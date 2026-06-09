import json
import os
import threading
import time as _time
from dataclasses import dataclass, field
from . import config
from .spot_risk import Position, create_position, apply_dca, update_peak, check_trailing_stop, check_take_profit, calc_pnl
from .notifier import trade_alert, trailing_stop_alert, notify
from .db import log_trade, log_balance
from .spot_exchange import round_qty, get_min_notional, get_eur_balance, place_order

SPOT_FEE = config.SPOT_FEE
_check_lock = threading.Lock()

_SIM_STATE_PATH  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "spot_state_simulation.json")
_LIVE_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "spot_state_live.json")
STATE_PATH = _LIVE_STATE_PATH if config.SPOT_MODE == "live" else _SIM_STATE_PATH


@dataclass
class SimState:
    balance: float = field(default_factory=lambda: config.SPOT_SIMULATION_BALANCE)
    positions: dict = field(default_factory=dict)   # pair -> Position
    total_trades: int = 0
    total_pnl: float = 0.0
    total_fees: float = 0.0
    portfolio_peak: float = 0.0


_state = SimState()


def _save():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    data = {
        "mode": config.SPOT_MODE,
        "balance": _state.balance,
        "total_trades": _state.total_trades,
        "total_pnl": _state.total_pnl,
        "total_fees": _state.total_fees,
        "portfolio_peak": _state.portfolio_peak,
        "positions": {
            pair: {
                "pair": pos.pair,
                "entry_price": pos.entry_price,
                "amount": pos.amount,
                "value_eur": pos.value_eur,
                "take_profit_price": pos.take_profit_price,
                "highest_price": pos.highest_price,
                "dca_count": pos.dca_count,
                "opened_at": pos.opened_at,
            }
            for pair, pos in _state.positions.items()
        },
    }
    with open(STATE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _load():
    global _state
    if not os.path.exists(STATE_PATH):
        return
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
        _state.balance = data.get("balance", config.SPOT_SIMULATION_BALANCE)
        _state.total_trades = data.get("total_trades", 0)
        _state.total_pnl = data.get("total_pnl", 0.0)
        _state.total_fees = data.get("total_fees", 0.0)
        _state.portfolio_peak = data.get("portfolio_peak", 0.0)
        _state.positions = {}
        for pair, pos in data.get("positions", {}).items():
            pos.pop("stop_cooldown", None)  # removed field; silently discard so old state files still load
            if "dca_done" in pos:           # legacy field: map to dca_count
                pos["dca_count"] = 1 if pos.pop("dca_done") else 0
            p = Position(**pos)
            if p.highest_price == 0.0:
                p.highest_price = p.entry_price
            _state.positions[pair] = p
    except Exception:
        pass  # corrupt state file — start fresh


def init():
    if config.SPOT_MODE == "live":
        # In live mode, balance is always fetched from Binance — just load positions.
        _load()
        _state.balance = get_eur_balance()
        _save()
        notify(f"[LIVE] Started — Binance EUR balance: €{_state.balance:.2f}  open positions: {list(_state.positions.keys()) or 'none'}")
    else:
        _load()


def get_state() -> SimState:
    return _state


def reset():
    global _state
    _state = SimState()
    _save()


# ---------------------------------------------------------------------------
# Position management
# ---------------------------------------------------------------------------

def _portfolio_value(balance: float, prices: dict | None) -> float:
    """Cash balance plus open position market values. Uses value_eur as fallback when price unavailable."""
    pos_val = sum(
        (prices[p] * pos.amount) if prices and p in prices else pos.value_eur
        for p, pos in _state.positions.items()
    )
    return balance + pos_val


def open_position(pair: str, price: float, size_pct: float = None, prices: dict | None = None):
    if pair in _state.positions:
        return
    pct = size_pct if size_pct is not None else config.SPOT_POSITION_SIZE_PCT

    if config.SPOT_MODE == "live":
        balance = get_eur_balance()
        _state.balance = balance
    else:
        balance = _state.balance

    if config.SPOT_MAX_DRAWDOWN_PCT > 0 and _state.portfolio_peak > 0:
        portfolio_value = _portfolio_value(balance, prices or {pair: price})
        drawdown = (_state.portfolio_peak - portfolio_value) / _state.portfolio_peak
        if drawdown > config.SPOT_MAX_DRAWDOWN_PCT:
            notify(f"[SKIP] {pair} buy blocked — portfolio drawdown {drawdown*100:.1f}% exceeds limit {config.SPOT_MAX_DRAWDOWN_PCT*100:.0f}%", discord=False)
            return

    max_size = balance / (1 + SPOT_FEE)
    min_notional = get_min_notional(pair)
    size = min(balance * pct, max_size)
    # If the leftover after buying would be below Binance's minimum tradeable amount,
    # spend everything — a stranded dust balance can't be traded anyway.
    if balance - size < min_notional:
        size = min(balance, max_size)
    if size < min_notional:
        notify(f"[SKIP] {pair} buy signal — order size €{size:.2f} below minimum €{min_notional:.2f} (balance €{balance:.2f})")
        return

    if config.SPOT_MODE == "live":
        amount = round_qty(pair, size / price)
        order  = place_order(pair, "BUY", amount)
        fills  = order.get("fills", [])
        if fills:
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / sum(float(f["qty"]) for f in fills)
            amount    = sum(float(f["qty"]) for f in fills)
            buy_fee   = sum(float(f["commission"]) for f in fills)
        else:
            avg_price = price
            buy_fee   = amount * avg_price * SPOT_FEE
        value    = amount * avg_price
        tp_price = avg_price * (1 + config.take_profit_for(pair))
        log_trade(pair, "BUY", avg_price, amount, value, buy_fee, mode="live")
        pos      = Position(pair, avg_price, amount, value, tp_price, avg_price, opened_at=_time.time())
        _state.positions[pair] = pos
        _state.total_fees += buy_fee
        _state.balance = get_eur_balance()
        _save()
        trade_alert("BUY", pair, avg_price, amount, value, fee=buy_fee)
    else:
        amount   = size / price
        amount   = round_qty(pair, amount)
        value    = amount * price
        buy_fee  = value * SPOT_FEE
        tp_price = price * (1 + config.take_profit_for(pair))
        pos      = Position(pair, price, amount, value, tp_price, price, opened_at=_time.time())
        _state.positions[pair] = pos
        _state.balance -= value + buy_fee
        _state.total_fees += buy_fee
        _save()
        trade_alert("BUY", pair, price, pos.amount, pos.value_eur, fee=buy_fee)
        log_trade(pair, "BUY", price, pos.amount, pos.value_eur, buy_fee, mode="simulation")


def manual_add(pair: str, price: float, size_pct: float):
    """Manual buy — opens new position or merges into existing one."""
    if config.SPOT_MODE == "live":
        balance = get_eur_balance()
        _state.balance = balance
    else:
        balance = _state.balance

    max_size = balance / (1 + SPOT_FEE)
    size = min(balance * size_pct, max_size)
    if size < 1:
        return

    buy_fee = size * SPOT_FEE

    if config.SPOT_MODE == "live":
        amount = round_qty(pair, size / price)
        order  = place_order(pair, "BUY", amount)
        fills  = order.get("fills", [])
        if fills:
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / sum(float(f["qty"]) for f in fills)
            amount    = sum(float(f["qty"]) for f in fills)
            buy_fee   = sum(float(f["commission"]) for f in fills)
        else:
            avg_price = price
            buy_fee   = amount * avg_price * SPOT_FEE
        value = amount * avg_price
        if pair not in _state.positions:
            tp_price = avg_price * (1 + config.take_profit_for(pair))
            pos = Position(pair, avg_price, amount, value, tp_price, avg_price, opened_at=_time.time())
            _state.positions[pair] = pos
        else:
            apply_dca(_state.positions[pair], avg_price, value)
        _state.total_fees += buy_fee
        _state.balance = get_eur_balance()
        _save()
        trade_alert("BUY", pair, avg_price, amount, value, fee=buy_fee)
        log_trade(pair, "BUY", avg_price, amount, value, buy_fee, mode="live", notes="manual_add")
    else:
        if pair not in _state.positions:
            amount   = round_qty(pair, size / price)
            value    = amount * price
            buy_fee  = value * SPOT_FEE
            tp_price = price * (1 + config.take_profit_for(pair))
            pos      = Position(pair, price, amount, value, tp_price, price, opened_at=_time.time())
            _state.positions[pair] = pos
            _state.balance -= value + buy_fee
            _state.total_fees += buy_fee
            _save()
            trade_alert("BUY", pair, price, pos.amount, pos.value_eur, fee=buy_fee)
            log_trade(pair, "BUY", price, pos.amount, pos.value_eur, buy_fee, mode="simulation")
        else:
            pos = _state.positions[pair]
            bought = size / price
            apply_dca(pos, price, size, tp_pct=config.take_profit_for(pair))
            _state.balance -= size + buy_fee
            _state.total_fees += buy_fee
            _save()
            trade_alert("BUY", pair, price, bought, size, fee=buy_fee)
            log_trade(pair, "BUY", price, bought, size, buy_fee, mode="simulation", notes="manual_add")


def dca_position(pair: str, price: float, prices: dict | None = None):
    if pair not in _state.positions:
        return
    pos = _state.positions[pair]
    if pos.dca_count >= config.SPOT_DCA_MAX:
        return

    if config.SPOT_MODE == "live":
        balance = get_eur_balance()
        _state.balance = balance
    else:
        balance = _state.balance

    if config.SPOT_MAX_DRAWDOWN_PCT > 0 and _state.portfolio_peak > 0:
        portfolio_value = _portfolio_value(balance, prices or {pair: price})
        drawdown = (_state.portfolio_peak - portfolio_value) / _state.portfolio_peak
        if drawdown > config.SPOT_MAX_DRAWDOWN_PCT:
            notify(f"[SKIP] {pair} DCA blocked — portfolio drawdown {drawdown*100:.1f}% exceeds limit {config.SPOT_MAX_DRAWDOWN_PCT*100:.0f}%", discord=False)
            return

    max_size  = balance / (1 + SPOT_FEE)
    min_notional = get_min_notional(pair)
    is_last_dca = (pos.dca_count + 1 >= config.SPOT_DCA_MAX)
    dca_value = max_size if is_last_dca else min(balance * config.SPOT_DCA_SIZE_PCT, max_size)
    # if leftover after DCA would be below minimum tradeable, use all available balance
    if balance - dca_value < min_notional:
        dca_value = min(balance, max_size)
    if dca_value < min_notional:
        notify(f"[SKIP] {pair} DCA — order size €{dca_value:.2f} below minimum €{min_notional:.2f} (balance €{balance:.2f})")
        return

    if config.SPOT_MODE == "live":
        amount = round_qty(pair, dca_value / price)
        order  = place_order(pair, "BUY", amount)
        fills  = order.get("fills", [])
        if fills:
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / sum(float(f["qty"]) for f in fills)
            bought    = sum(float(f["qty"]) for f in fills)
            buy_fee   = sum(float(f["commission"]) for f in fills)
            dca_value = bought * avg_price
        else:
            avg_price = price
            bought    = amount
            buy_fee   = dca_value * SPOT_FEE
        log_trade(pair, "BUY", avg_price, bought, dca_value, buy_fee, mode="live", notes="dca")
        apply_dca(pos, avg_price, dca_value, tp_pct=config.take_profit_for(pair))
        _state.total_fees += buy_fee
        _state.balance = get_eur_balance()
        _save()
        trade_alert("DCA", pair, avg_price, bought, dca_value, fee=buy_fee)
    else:
        buy_fee = dca_value * SPOT_FEE
        bought  = dca_value / price
        apply_dca(pos, price, dca_value, tp_pct=config.take_profit_for(pair))
        _state.balance -= dca_value + buy_fee
        _state.total_fees += buy_fee
        _save()
        trade_alert("DCA", pair, price, bought, dca_value, fee=buy_fee)
        log_trade(pair, "BUY", price, bought, dca_value, buy_fee, mode="simulation", notes="dca")


def close_position(pair: str, price: float, reason: str = "signal"):
    if pair not in _state.positions:
        return

    pos = _state.positions.pop(pair)

    if config.SPOT_MODE == "live":
        order  = place_order(pair, "SELL", pos.amount)
        fills  = order.get("fills", [])
        if fills:
            avg_price  = sum(float(f["price"]) * float(f["qty"]) for f in fills) / sum(float(f["qty"]) for f in fills)
            sold_qty   = sum(float(f["qty"]) for f in fills)
            sell_fee   = sum(float(f["commission"]) for f in fills)
        else:
            avg_price = price
            sold_qty  = pos.amount
            sell_fee  = pos.amount * price * SPOT_FEE
        exit_value = sold_qty * avg_price
        buy_fee    = pos.value_eur * SPOT_FEE
        pnl        = calc_pnl(pos, avg_price, buy_fee=buy_fee, sell_fee=sell_fee)
        log_trade(pair, "SELL", avg_price, sold_qty, exit_value, sell_fee, mode="live", pnl=pnl, notes=reason)
        _state.total_trades += 1
        _state.total_pnl    += pnl
        _state.total_fees   += sell_fee
        _state.balance = get_eur_balance()
        _save()
        if reason == "trailing_stop":
            trailing_stop_alert(pair, avg_price, pnl)
        else:
            trade_alert("SELL", pair, avg_price, sold_qty, exit_value, pnl=pnl, fee=sell_fee)
    else:
        exit_value = pos.amount * price
        buy_fee    = pos.value_eur * SPOT_FEE
        sell_fee   = exit_value * SPOT_FEE
        pnl        = calc_pnl(pos, price, buy_fee=buy_fee, sell_fee=sell_fee)
        _state.balance      += exit_value - sell_fee
        _state.total_trades += 1
        _state.total_pnl    += pnl
        _state.total_fees   += sell_fee
        _save()
        if reason == "trailing_stop":
            trailing_stop_alert(pair, price, pnl)
        else:
            trade_alert("SELL", pair, price, pos.amount, exit_value, pnl=pnl, fee=sell_fee)
        log_trade(pair, "SELL", price, pos.amount, exit_value, sell_fee,
                  mode="simulation", pnl=pnl, notes=reason)


def check_stops(prices: dict):
    with _check_lock:
        now = _time.time()
        for pair, pos in list(_state.positions.items()):
            if pair not in prices:
                continue
            price = prices[pair]
            updated = update_peak(pos, price)
            if check_take_profit(pos, price):
                close_position(pair, price, reason="take_profit")
            elif check_trailing_stop(pos, price, floor_pct=config.profit_floor_for(pair), trail_pct=config.trailing_stop_for(pair)):
                close_position(pair, price, reason="trailing_stop")
            elif (config.time_stop_for(pair) > 0
                  and pos.opened_at > 0
                  and (now - pos.opened_at) / 86400 > config.time_stop_for(pair)
                  and pos.peak() <= pos.trailing_stop_level(floor_pct=config.profit_floor_for(pair), trail_pct=config.trailing_stop_for(pair))):
                age = (now - pos.opened_at) / 86400
                notify(f"[TIME STOP] {pair} — position held {age:.0f}d without reaching profit floor, closing at €{price:,.2f}")
                close_position(pair, price, reason="time_stop")
            elif updated:
                _save()

        # Update portfolio peak for drawdown guard
        if config.SPOT_MAX_DRAWDOWN_PCT > 0:
            portfolio_value = _state.balance + sum(
                prices[p] * pos.amount for p, pos in _state.positions.items() if p in prices
            )
            if portfolio_value > _state.portfolio_peak:
                _state.portfolio_peak = portfolio_value
                _save()


# ---------------------------------------------------------------------------
# Shadow simulation
# ---------------------------------------------------------------------------

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


class SpotShadowSimulator:
    """A lightweight simulation-only instance running a different strategy config.
    Receives the same candle ticks as the main engine but never touches the exchange.
    Each shadow has its own state file and balance."""

    def __init__(self, name: str, state_path: str, overrides: dict):
        self.name       = name
        self.state_path = state_path
        self.overrides  = overrides
        self.pairs: list[str] | None = overrides.get("pairs", None)
        self.interval: str = overrides.get("interval", config.SPOT_INTERVAL)
        self._lock      = threading.Lock()
        self.balance        = float(overrides.get("balance", config.SPOT_SIMULATION_BALANCE))
        self.positions: dict[str, Position] = {}
        self.total_trades   = 0
        self.total_pnl      = 0.0
        self.total_fees     = 0.0
        self.portfolio_peak = 0.0
        self.started_at: str | None = None
        self._load()
        if self.started_at is None:
            import datetime, zoneinfo
            self.started_at = datetime.datetime.now(tz=zoneinfo.ZoneInfo("Europe/Helsinki")).isoformat()
            self._save()

    # --- override helpers ---

    def _o(self, key: str, default):
        return self.overrides.get(key, default)

    def _trailing_stop_level(self, pos: Position) -> float:
        fee   = config.SPOT_FEE
        floor = self._o("floor_pct", config.profit_floor_for(pos.pair))
        trail = self._o("trail_pct", config.trailing_stop_for(pos.pair))
        return max(
            pos.entry_price * (1 + fee + floor) / (1 - fee),
            pos.peak() * (1 - trail),
        )

    # --- persistence ---

    def _save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        data = {
            "name": self.name,
            "started_at": self.started_at,
            "overrides": self.overrides,
            "balance": self.balance,
            "total_trades": self.total_trades,
            "total_pnl": self.total_pnl,
            "total_fees": self.total_fees,
            "portfolio_peak": self.portfolio_peak,
            "positions": {
                pair: {
                    "pair":               pos.pair,
                    "entry_price":        pos.entry_price,
                    "amount":             pos.amount,
                    "value_eur":          pos.value_eur,
                    "take_profit_price":  pos.take_profit_price,
                    "highest_price":      pos.highest_price,
                    "dca_count":          pos.dca_count,
                    "opened_at":          pos.opened_at,
                }
                for pair, pos in self.positions.items()
            },
        }
        with open(self.state_path, "w") as f:
            json.dump(data, f, indent=2)

    def _load(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            self.started_at     = data.get("started_at",     None)
            self.balance        = data.get("balance", float(self.overrides.get("balance", config.SPOT_SIMULATION_BALANCE)))
            self.total_trades   = data.get("total_trades",   0)
            self.total_pnl      = data.get("total_pnl",      0.0)
            self.total_fees     = data.get("total_fees",     0.0)
            self.portfolio_peak = data.get("portfolio_peak", 0.0)
            self.positions      = {}
            for pair, pos in data.get("positions", {}).items():
                pos.pop("stop_cooldown", None)
                if "dca_done" in pos:
                    pos["dca_count"] = 1 if pos.pop("dca_done") else 0
                p = Position(**pos)
                if p.highest_price == 0.0:
                    p.highest_price = p.entry_price
                self.positions[pair] = p
        except Exception:
            pass

    @property
    def _db_mode(self) -> str:
        return f"shadow_{self.name.lower()}"

    # --- internal trade ops (simulation only, no exchange) ---

    def _open(self, pair: str, price: float):
        if pair in self.positions:
            return
        tp_pct      = self._o("tp_pct",  config.take_profit_for(pair))
        max_sz      = self.balance / (1 + SPOT_FEE)
        no_dca      = self._o("dca_max", config.SPOT_DCA_MAX) == 0
        pos_pct_set = "pos_pct" in self.overrides
        pct         = self._o("pos_pct", config.SPOT_POSITION_SIZE_PCT)
        size        = max_sz if (no_dca and not pos_pct_set) else min(self.balance * pct, max_sz)
        if size < 1:
            return
        buy_fee = size * SPOT_FEE
        amount  = size / price
        pos     = Position(pair, price, amount, size, price * (1 + tp_pct), price, opened_at=_time.time())
        self.positions[pair]  = pos
        self.balance         -= size + buy_fee
        self.total_fees      += buy_fee
        log_trade(pair, "BUY", price, amount, size, buy_fee, mode=self._db_mode)

    def _dca(self, pair: str, price: float):
        pos = self.positions.get(pair)
        if pos is None:
            return
        dca_max = self._o("dca_max", config.SPOT_DCA_MAX)
        tp_pct  = self._o("tp_pct",  config.take_profit_for(pair))
        if pos.dca_count >= dca_max:
            return
        pct    = self._o("dca_pct", config.SPOT_DCA_SIZE_PCT)
        max_sz = self.balance / (1 + SPOT_FEE)
        is_last_dca = (pos.dca_count + 1 >= dca_max)
        size   = max_sz if is_last_dca else min(self.balance * pct, max_sz)
        if size < 1:
            return
        bought  = size / price
        buy_fee = size * SPOT_FEE
        apply_dca(pos, price, size)
        pos.take_profit_price = pos.entry_price * (1 + tp_pct)
        self.balance     -= size + buy_fee
        self.total_fees  += buy_fee
        log_trade(pair, "BUY", price, bought, size, buy_fee, mode=self._db_mode, notes="dca")

    def _close(self, pair: str, price: float, reason: str = "signal"):
        pos = self.positions.pop(pair, None)
        if pos is None:
            return
        exit_value  = pos.amount * price
        buy_fee     = pos.value_eur * SPOT_FEE
        sell_fee    = exit_value * SPOT_FEE
        pnl         = calc_pnl(pos, price, buy_fee=buy_fee, sell_fee=sell_fee)
        self.balance        += exit_value - sell_fee
        self.total_trades   += 1
        self.total_pnl      += pnl
        self.total_fees     += sell_fee
        log_trade(pair, "SELL", price, pos.amount, exit_value, sell_fee, mode=self._db_mode, pnl=pnl, notes=reason)

    # --- public API ---

    def tick(self, pair: str, prices: dict):
        """Process one candle for one pair. Called from the engine's main loop."""
        from .candles import get_df
        from .strategy import compute_signal, Signal

        price = prices.get(pair)
        if price is None:
            return

        with self._lock:
            # stops first (same logic as between-candle check)
            pos = self.positions.get(pair)
            if pos:
                update_peak(pos, price)
                stop = self._trailing_stop_level(pos)
                if price >= pos.take_profit_price > 0:
                    self._close(pair, price, "take_profit")
                    self._save()
                    return
                if pos.peak() > stop and price <= stop:
                    self._close(pair, price, "trailing_stop")
                    self._save()
                    return
                time_stop = self._o("time_stop_days", config.time_stop_for(pair))
                if time_stop > 0 and pos.opened_at > 0 and (_time.time() - pos.opened_at) / 86400 > time_stop:
                    self._close(pair, price, "time_stop")
                    self._save()
                    return

            df     = get_df(pair, self.interval)
            result = compute_signal(
                df,
                rsi_period   = self._o("rsi_period",   config.rsi_period_for(pair)),
                rsi_oversold = self._o("rsi_oversold",  config.rsi_oversold_for(pair)),
                rsi_overbought = self._o("rsi_overbought", config.rsi_overbought_for(pair)),
                ema_gap      = self._o("ema_gap",       config.ema_gap_for(pair)),
            )

            if result.signal == Signal.BUY and pair not in self.positions:
                self._open(pair, price)
            elif pair in self.positions:
                pos      = self.positions[pair]
                dca_drop = self._o("dca_drop", config.dca_drop_for(pair))
                dca_step = self._o("dca_step", config.SPOT_DCA_STEP_PCT)
                drop     = (pos.entry_price - price) / pos.entry_price
                next_drop = dca_drop + pos.dca_count * dca_step
                if pos.dca_count < self._o("dca_max", config.SPOT_DCA_MAX) and drop >= next_drop:
                    self._dca(pair, price)
                if result.signal == Signal.SELL:
                    min_exit = self._o("min_exit", config.min_exit_for(pair))
                    if price >= pos.entry_price * (1 + min_exit):
                        self._close(pair, price, "signal")

            # update portfolio peak and log balance history
            port_val = self.balance + sum(
                prices[p] * p2.amount for p, p2 in self.positions.items() if p in prices
            )
            if port_val > self.portfolio_peak:
                self.portfolio_peak = port_val
            log_balance(round(port_val, 2), self._db_mode)

            self._save()

    def check_stops(self, prices: dict):
        """Between-candle stop check — trailing stop and take-profit only."""
        with self._lock:
            changed = False
            for pair, pos in list(self.positions.items()):
                price = prices.get(pair)
                if price is None:
                    continue
                updated = update_peak(pos, price)
                stop    = self._trailing_stop_level(pos)
                if price >= pos.take_profit_price > 0:
                    self._close(pair, price, "take_profit")
                    changed = True
                elif pos.peak() > stop and price <= stop:
                    self._close(pair, price, "trailing_stop")
                    changed = True
                elif updated:
                    changed = True

            port_val = self.balance + sum(
                prices[p] * p2.amount for p, p2 in self.positions.items() if p in prices
            )
            if port_val > self.portfolio_peak:
                self.portfolio_peak = port_val
                changed = True

            if changed:
                self._save()

    def get_state(self) -> dict:
        return {
            "name":           self.name,
            "overrides":      self.overrides,
            "balance":        round(self.balance,        2),
            "total_trades":   self.total_trades,
            "total_pnl":      round(self.total_pnl,      2),
            "total_fees":     round(self.total_fees,     4),
            "portfolio_peak": round(self.portfolio_peak, 2),
            "positions": {
                pair: {
                    "entry_price":   pos.entry_price,
                    "amount":        pos.amount,
                    "value_eur":     round(pos.value_eur, 2),
                    "dca_count":     pos.dca_count,
                    "trailing_stop": round(self._trailing_stop_level(pos), 2),
                    "take_profit":   round(pos.take_profit_price, 2),
                }
                for pair, pos in self.positions.items()
            },
        }


# --- module-level shadow registry ---

_shadows: list[SpotShadowSimulator] = []


def init_shadows() -> list[SpotShadowSimulator]:
    global _shadows
    _shadows = []
    for name in config.get_shadow_profiles():
        overrides   = config.get_shadow_overrides(name)
        state_path  = os.path.join(_DATA_DIR, f"spot_state_shadow_{name.lower()}.json")
        _shadows.append(SpotShadowSimulator(name, state_path, overrides))
    if _shadows:
        names = ", ".join(s.name for s in _shadows)
        notify(f"[SHADOW] {len(_shadows)} shadow sim(s) loaded: {names}", discord=False)
    return _shadows


def get_shadows() -> list[SpotShadowSimulator]:
    return _shadows
