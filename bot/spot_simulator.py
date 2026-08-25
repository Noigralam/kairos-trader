import json
import logging
import os
import threading
import time as _time
from dataclasses import dataclass, field
from . import config
from .spot_risk import Position, apply_dca, update_peak, check_trailing_stop, check_take_profit, calc_pnl
from .notifier import trade_alert, trailing_stop_alert, notify

log = logging.getLogger("cryptobot")
from .db import log_trade, log_balance, clear_shadow_trades
from .spot_exchange import round_qty, get_min_notional, get_eur_balance, get_free_balance, place_order

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
    stop_cooldowns: dict = field(default_factory=dict)  # pair -> epoch expiry


_state = SimState()


def _save():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    now = _time.time()
    data = {
        "mode": config.SPOT_MODE,
        "balance": _state.balance,
        "total_trades": _state.total_trades,
        "total_pnl": _state.total_pnl,
        "total_fees": _state.total_fees,
        "portfolio_peak": _state.portfolio_peak,
        "stop_cooldowns": {p: t for p, t in _state.stop_cooldowns.items() if t > now},
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
                "partial_closed": pos.partial_closed,
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
        now = _time.time()
        _state.stop_cooldowns = {p: t for p, t in data.get("stop_cooldowns", {}).items() if t > now}
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
        try:
            _state.balance = get_eur_balance()
            _save()
            notify(f"[LIVE] Started — Binance EUR balance: €{_state.balance:.2f}  open positions: {list(_state.positions.keys()) or 'none'}")
        except Exception as e:
            log.warning(f"[LIVE] Binance balance fetch failed at startup ({e}) — using last saved balance €{_state.balance:.2f}")
    else:
        _load()


def get_state() -> SimState:
    return _state


def sync_position_from_binance(pair: str) -> dict:
    """
    Reconcile the tracked position amount for `pair` against the actual free
    balance reported by Binance.  Updates in-memory state and persists to disk.
    Returns a summary dict with old/new amounts and the delta.
    Only valid in live mode.
    """
    if config.SPOT_MODE != "live":
        return {"error": "only valid in live mode"}
    if pair not in _state.positions:
        return {"error": f"no open position for {pair}"}

    asset = pair.replace("EUR", "").replace("USDT", "")
    actual = get_free_balance(asset)
    pos    = _state.positions[pair]
    old_amount = pos.amount

    if actual <= 0:
        return {"error": f"Binance reports 0 free {asset}"}

    delta = actual - old_amount
    # Recalculate cost basis so entry_price stays proportional
    pos.value_eur = pos.value_eur * (actual / old_amount) if old_amount else pos.value_eur
    pos.amount    = actual
    _save()

    log.info(f"[SYNC] {pair} position amount updated: {old_amount:.6f} → {actual:.6f} ({delta:+.6f} {asset})")
    return {"pair": pair, "asset": asset, "old": old_amount, "new": actual, "delta": delta}


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
    cooldown_until = _state.stop_cooldowns.get(pair, 0)
    if cooldown_until > _time.time():
        remaining = (cooldown_until - _time.time()) / 60
        notify(f"[SKIP] {pair} buy blocked — stop cooldown active ({remaining:.0f} min remaining)", discord=False)
        return
    no_dca = config.dca_max_for(pair) == 0
    pct = size_pct if size_pct is not None else (1.0 if no_dca else config.SPOT_POSITION_SIZE_PCT)

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
    if pos.dca_count >= config.dca_max_for(pair):
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
    is_last_dca = (pos.dca_count + 1 >= config.dca_max_for(pair))
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


_INTERVAL_SECONDS = config.INTERVAL_SECONDS


def _set_stop_cooldown(pair: str):
    candles = config.stop_cooldown_for(pair)
    if candles <= 0:
        return
    secs = candles * _INTERVAL_SECONDS.get(config.SPOT_INTERVAL, 900)
    _state.stop_cooldowns[pair] = _time.time() + secs
    notify(f"[COOLDOWN] {pair} — re-entry blocked for {candles} candles ({secs//60:.0f} min)", discord=False)


def close_position(pair: str, price: float, reason: str = "signal"):
    if pair not in _state.positions:
        return

    pos = _state.positions.pop(pair)

    if config.SPOT_MODE == "live":
        try:
            base_asset  = pair.replace("EUR", "").replace("USDT", "").replace("USDC", "")
            free        = get_free_balance(base_asset)
            sell_amount = min(pos.amount, free)
            order = place_order(pair, "SELL", sell_amount)
        except Exception:
            _state.positions[pair] = pos  # restore — order never reached exchange
            raise
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
        if reason == "trailing_stop":
            _set_stop_cooldown(pair)
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
        if reason == "trailing_stop":
            _set_stop_cooldown(pair)
        _save()
        if reason == "trailing_stop":
            trailing_stop_alert(pair, price, pnl)
        else:
            trade_alert("SELL", pair, price, pos.amount, exit_value, pnl=pnl, fee=sell_fee)
        log_trade(pair, "SELL", price, pos.amount, exit_value, sell_fee,
                  mode="simulation", pnl=pnl, notes=reason)


def partial_close_position(pair: str, price: float):
    """Sell a configured fraction of position at TP; leave remainder running on tighter trail."""
    if pair not in _state.positions:
        return
    pos = _state.positions[pair]
    pct = config.partial_close_for(pair)
    if pct <= 0:
        return

    sell_amt = round_qty(pair, pos.amount * pct) if config.SPOT_MODE == "live" else pos.amount * pct

    if config.SPOT_MODE == "live":
        base_asset = pair.replace("EUR", "").replace("USDT", "").replace("USDC", "")
        free       = get_free_balance(base_asset)
        sell_amt   = min(sell_amt, round_qty(pair, free * pct))
        order = place_order(pair, "SELL", sell_amt)
        fills = order.get("fills", [])
        if fills:
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / sum(float(f["qty"]) for f in fills)
            sell_amt  = sum(float(f["qty"]) for f in fills)
            sell_fee  = sum(float(f["commission"]) for f in fills)
        else:
            avg_price = price
            sell_fee  = sell_amt * price * SPOT_FEE
        exit_value = sell_amt * avg_price
        buy_fee    = pos.value_eur * (sell_amt / pos.amount) * SPOT_FEE
        pnl        = (avg_price - pos.entry_price) * sell_amt - buy_fee - sell_fee
        log_trade(pair, "SELL", avg_price, sell_amt, exit_value, sell_fee, mode="live", pnl=pnl, notes="partial_close")
        _state.total_pnl  += pnl
        _state.total_fees += sell_fee
        _state.balance = get_eur_balance()
    else:
        avg_price  = price
        exit_value = sell_amt * price
        sell_fee   = exit_value * SPOT_FEE
        buy_fee    = pos.value_eur * (sell_amt / pos.amount) * SPOT_FEE
        pnl        = (price - pos.entry_price) * sell_amt - buy_fee - sell_fee
        _state.balance    += exit_value - sell_fee
        _state.total_pnl  += pnl
        _state.total_fees += sell_fee
        log_trade(pair, "SELL", price, sell_amt, exit_value, sell_fee, mode="simulation", pnl=pnl, notes="partial_close")

    # update position: reduce amount, disable TP, activate tighter trail
    value_frac = sell_amt / pos.amount
    pos.amount        -= sell_amt
    pos.value_eur     -= pos.value_eur * value_frac
    pos.take_profit_price = 0.0
    pos.partial_closed    = True
    _save()
    trade_alert("SELL", pair, avg_price, sell_amt, exit_value, pnl=pnl, fee=sell_fee)


def check_stops(prices: dict):
    with _check_lock:
        now = _time.time()
        for pair, pos in list(_state.positions.items()):
            if pair not in prices:
                continue
            price = prices[pair]
            updated = update_peak(pos, price)
            floor = 0.0 if pos.partial_closed else config.profit_floor_for(pair)
            trail = config.partial_close_trail_for(pair) if pos.partial_closed else config.trailing_stop_for(pair)
            if check_take_profit(pos, price) and not pos.partial_closed:
                if config.partial_close_for(pair) > 0:
                    partial_close_position(pair, price)
                else:
                    close_position(pair, price, reason="take_profit")
            elif check_trailing_stop(pos, price, floor_pct=floor, trail_pct=trail):
                close_position(pair, price, reason="trailing_stop")
            elif (config.time_stop_for(pair) > 0
                  and pos.opened_at > 0
                  and (now - pos.opened_at) / 86400 > config.time_stop_for(pair)
                  and pos.peak() <= pos.trailing_stop_level(floor_pct=floor, trail_pct=trail)):
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


def _config_fingerprint(overrides: dict) -> str:
    """Hash of strategy-relevant overrides; excludes spot_balance so balance changes don't trigger a reset."""
    import hashlib
    skip = {"spot_balance"}
    relevant = {k: v for k, v in overrides.items() if k not in skip}
    blob = json.dumps(relevant, sort_keys=True)
    return hashlib.md5(blob.encode()).hexdigest()


def _live_spot_balance() -> float:
    try:
        with open(_LIVE_STATE_PATH) as f:
            return float(json.load(f).get("balance", config.SPOT_SHADOW_DEFAULT_BALANCE))
    except Exception:
        return config.SPOT_SHADOW_DEFAULT_BALANCE


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
        self.starting_balance = float(overrides.get("spot_balance", _live_spot_balance()))
        self.balance        = self.starting_balance
        self.positions: dict[str, Position] = {}
        self.total_trades   = 0
        self.total_pnl      = 0.0
        self.total_fees     = 0.0
        self.portfolio_peak = 0.0
        self.started_at: str | None = None
        self.live_baseline_pct: float | None = None
        self._stop_cooldowns: dict[str, float] = {}
        self._reentry_drop_prices: dict[str, float] = {}
        self._fingerprint = _config_fingerprint(overrides)
        self._load()
        if self.started_at is None:
            import datetime, zoneinfo
            self.started_at = datetime.datetime.now(tz=zoneinfo.ZoneInfo("Europe/Helsinki")).isoformat()
            self._save()

    # --- override helpers ---

    def _o(self, key: str, default):
        return self.overrides.get(key, default)

    def set_live_baseline(self, pct: float):
        if self.live_baseline_pct is None:
            self.live_baseline_pct = pct
            self._save()

    def _trailing_stop_level(self, pos: Position) -> float:
        fee = config.SPOT_FEE
        if pos.partial_closed:
            floor = 0.0
            trail = config.partial_close_trail_for(pos.pair)
        else:
            floor = self._o("spot_floor_pct", config.profit_floor_for(pos.pair))
            trail = self._o("spot_trail_pct", config.trailing_stop_for(pos.pair))
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
            "config_fingerprint": self._fingerprint,
            "starting_balance":  self.starting_balance,
            "live_baseline_pct": self.live_baseline_pct,
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
                    "partial_closed":     pos.partial_closed,
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
            saved_fp = data.get("config_fingerprint")
            if saved_fp is not None and saved_fp != self._fingerprint:
                log.info(f"[SHADOW] {self.name}: config changed — resetting trades and state")
                clear_shadow_trades(self._db_mode)
                os.remove(self.state_path)
                self.starting_balance = _live_spot_balance()
                self.balance = self.starting_balance
                return
            self.started_at        = data.get("started_at",        None)
            self.starting_balance  = data.get("starting_balance",  self.starting_balance)
            self.live_baseline_pct = data.get("live_baseline_pct", None)
            self.balance           = data.get("balance",           self.starting_balance)
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
        cooldown_until = self._stop_cooldowns.get(pair, 0)
        if cooldown_until > _time.time():
            return
        reentry_threshold = self._reentry_drop_prices.get(pair)
        if reentry_threshold is not None:
            if price > reentry_threshold:
                return
            del self._reentry_drop_prices[pair]
        fng_max = self._o("spot_fng_max", None)
        if fng_max is not None:
            from .notifier import get_fng
            fng_val, _ = get_fng()
            if fng_val is not None and fng_val > fng_max:
                return
        tp_pct      = self._o("spot_tp_pct",  config.take_profit_for(pair))
        max_sz      = self.balance / (1 + SPOT_FEE)
        no_dca      = self._o("spot_dca_max", config.dca_max_for(pair)) == 0
        pos_pct_set = "pos_pct" in self.overrides
        pct         = self._o("spot_pos_pct", config.SPOT_POSITION_SIZE_PCT)
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
        dca_max = self._o("spot_dca_max", config.dca_max_for(pair))
        tp_pct  = self._o("spot_tp_pct",  config.take_profit_for(pair))
        if pos.dca_count >= dca_max:
            return
        pct    = self._o("spot_dca_pct", config.SPOT_DCA_SIZE_PCT)
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

    def _partial_close(self, pair: str, price: float):
        pos = self.positions.get(pair)
        if pos is None:
            return
        pct      = self._o("spot_partial_close_pct", config.partial_close_for(pair))
        sell_amt = pos.amount * pct
        exit_val = sell_amt * price
        sell_fee = exit_val * SPOT_FEE
        buy_fee  = pos.value_eur * pct * SPOT_FEE
        pnl      = (price - pos.entry_price) * sell_amt - buy_fee - sell_fee
        self.balance    += exit_val - sell_fee
        self.total_pnl  += pnl
        self.total_fees += sell_fee
        log_trade(pair, "SELL", price, sell_amt, exit_val, sell_fee, mode=self._db_mode, pnl=pnl, notes="partial_close")
        value_frac = sell_amt / pos.amount
        pos.amount           -= sell_amt
        pos.value_eur        -= pos.value_eur * value_frac
        pos.take_profit_price = 0.0
        pos.partial_closed    = True

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
        if reason == "trailing_stop":
            candles = self._o("spot_stop_cooldown", config.stop_cooldown_for(pair))
            if candles > 0:
                secs = candles * _INTERVAL_SECONDS.get(self.interval, 900)
                self._stop_cooldowns[pair] = _time.time() + secs
            drop_pct = self._o("spot_reentry_drop_pct", 0.0)
            if drop_pct > 0:
                self._reentry_drop_prices[pair] = price * (1 - drop_pct)
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
                if price >= pos.take_profit_price > 0 and not pos.partial_closed:
                    pct = self._o("spot_partial_close_pct", config.partial_close_for(pair))
                    if pct > 0:
                        self._partial_close(pair, price)
                    else:
                        self._close(pair, price, "take_profit")
                    self._save()
                    return
                if pos.peak() > stop and price <= stop:
                    self._close(pair, price, "trailing_stop")
                    self._save()
                    return
                time_stop = self._o("spot_time_stop_days", config.time_stop_for(pair))
                if time_stop > 0 and pos.opened_at > 0 and (_time.time() - pos.opened_at) / 86400 > time_stop:
                    self._close(pair, price, "time_stop")
                    self._save()
                    return

            df     = get_df(pair, self.interval)
            result = compute_signal(
                df,
                rsi_period     = self._o("spot_rsi_period",     config.rsi_period_for(pair)),
                rsi_oversold   = self._o("spot_rsi_oversold",   config.rsi_oversold_for(pair)),
                rsi_overbought = self._o("spot_rsi_overbought", config.rsi_overbought_for(pair)),
                ema_gap        = self._o("spot_ema_gap",        config.ema_gap_for(pair)),
                vol_period     = self._o("spot_vol_period",     config.vol_period_for(pair)),
                vol_mult       = self._o("spot_vol_mult",       config.vol_mult_for(pair)),
            )

            if result.signal == Signal.BUY and pair not in self.positions:
                self._open(pair, price)
            elif pair in self.positions:
                pos      = self.positions[pair]
                dca_drop = self._o("spot_dca_drop", config.dca_drop_for(pair))
                dca_step = self._o("spot_dca_step", config.SPOT_DCA_STEP_PCT)
                drop     = (pos.entry_price - price) / pos.entry_price
                next_drop = dca_drop + pos.dca_count * dca_step
                if pos.dca_count < self._o("spot_dca_max", config.dca_max_for(pair)) and drop >= next_drop:
                    self._dca(pair, price)
                if result.signal == Signal.SELL:
                    min_exit = self._o("spot_min_exit", config.min_exit_for(pair))
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
                if price >= pos.take_profit_price > 0 and not pos.partial_closed:
                    pct = self._o("spot_partial_close_pct", config.partial_close_for(pair))
                    if pct > 0:
                        self._partial_close(pair, price)
                    else:
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


class GridShadowSimulator:
    """Grid trading shadow: maintains a ladder of fixed buy/sell levels around a reference price.
    Each slot is funded with starting_balance / grid_levels EUR. Sells before buys on each candle."""

    def __init__(self, name: str, state_path: str, overrides: dict):
        self.name       = name
        self.state_path = state_path
        self.overrides  = overrides
        self.pairs: list[str] | None = overrides.get("pairs", None)
        self.interval: str = overrides.get("interval", config.SPOT_INTERVAL)
        self._lock      = threading.Lock()

        starting = float(overrides.get("spot_balance", _live_spot_balance()))
        self.starting_balance = starting
        self.balance        = starting
        self.total_trades   = 0
        self.total_pnl      = 0.0
        self.total_fees     = 0.0
        self.portfolio_peak = 0.0
        self.started_at: str | None = None
        self.live_baseline_pct: float | None = None

        self._spacing  = float(overrides.get("spot_grid_spacing", 0.015))
        self._n_levels = int(overrides.get("spot_grid_levels", 8))
        self._slot_eur = self.starting_balance / self._n_levels

        # grid state: list of slot dicts (serialised to JSON)
        self._grid_center: float | None = None
        self._slots: list[dict] = []
        self._highest_price: float = 0.0

        self._fingerprint = _config_fingerprint(overrides)
        self._load()
        if self.started_at is None:
            import datetime, zoneinfo
            self.started_at = datetime.datetime.now(tz=zoneinfo.ZoneInfo("Europe/Helsinki")).isoformat()
            self._save()

    # --- compatibility helpers (app.py calls these on all shadows) ---

    def _o(self, key: str, default):
        if key == "spot_dca_max":
            return self._n_levels
        return self.overrides.get(key, default)

    def set_live_baseline(self, pct: float):
        if self.live_baseline_pct is None:
            self.live_baseline_pct = pct
            self._save()

    def _trailing_stop_level(self, pos) -> float:
        return 0.0  # grid exits at fixed sell targets, no trailing stop

    @property
    def _db_mode(self) -> str:
        return f"shadow_{self.name.lower()}"

    @property
    def positions(self) -> dict:
        """Aggregate all filled slots into one Position so dashboard/stop-check loop works."""
        filled = [s for s in self._slots if s["filled"]]
        if not filled:
            return {}
        pair         = (self.pairs or config.SPOT_TRADING_PAIRS)[0]
        total_amount = sum(s["amount"] for s in filled)
        total_cost   = sum(s["cost_eur"] for s in filled)
        avg_entry    = total_cost / total_amount if total_amount > 0 else 0.0
        pos = Position(
            pair             = pair,
            entry_price      = avg_entry,
            amount           = total_amount,
            value_eur        = total_cost,
            take_profit_price= 0.0,
            highest_price    = self._highest_price or avg_entry,
            dca_count        = len(filled),
            opened_at        = _time.time(),
            partial_closed   = False,
        )
        return {pair: pos}

    # --- grid management ---

    def _init_grid(self, center: float):
        self._grid_center = center
        self._slots = []
        for i in range(self._n_levels):
            buy   = round(center * (1 - (i + 1) * self._spacing), 8)
            sell  = round(buy  * (1 + self._spacing), 8)
            self._slots.append({"level": i, "buy_price": buy, "sell_price": sell,
                                 "amount": 0.0, "cost_eur": 0.0, "filled": False})

    def _buy_slot(self, slot: dict, price: float) -> bool:
        if self.balance < self._slot_eur * (1 + SPOT_FEE):
            return False
        fee    = self._slot_eur * SPOT_FEE
        amount = self._slot_eur / price
        slot["filled"]   = True
        slot["amount"]   = amount
        slot["cost_eur"] = self._slot_eur
        self.balance    -= self._slot_eur + fee
        self.total_fees += fee
        if price > self._highest_price:
            self._highest_price = price
        pair = (self.pairs or config.SPOT_TRADING_PAIRS)[0]
        log_trade(pair, "BUY", price, amount, self._slot_eur, fee,
                  mode=self._db_mode, notes=f"grid_level_{slot['level']}")
        return True

    def _sell_slot(self, slot: dict, price: float):
        pair      = (self.pairs or config.SPOT_TRADING_PAIRS)[0]
        sell_val  = slot["amount"] * price
        sell_fee  = sell_val  * SPOT_FEE
        buy_fee   = slot["cost_eur"] * SPOT_FEE
        pnl       = sell_val - slot["cost_eur"] - buy_fee - sell_fee
        self.balance        += sell_val - sell_fee
        self.total_trades   += 1
        self.total_pnl      += pnl
        self.total_fees     += sell_fee
        log_trade(pair, "SELL", price, slot["amount"], sell_val, sell_fee,
                  mode=self._db_mode, pnl=pnl, notes=f"grid_level_{slot['level']}")
        slot["filled"]   = False
        slot["amount"]   = 0.0
        slot["cost_eur"] = 0.0

    # --- main loop interface ---

    def tick(self, pair: str, prices: dict):
        from .candles import get_df
        price = prices.get(pair)
        if price is None:
            return

        with self._lock:
            # First tick: initialise grid around current price and skip trading
            if self._grid_center is None:
                self._init_grid(price)
                self._save()
                return

            df = get_df(pair, self.interval)
            if df.empty:
                return
            last = df.iloc[-1]
            high = float(last["high"])
            low  = float(last["low"])
            if high > self._highest_price:
                self._highest_price = high

            # Sells first (price rallied → close profitable slots before opening new ones)
            for slot in self._slots:
                if slot["filled"] and high >= slot["sell_price"]:
                    self._sell_slot(slot, slot["sell_price"])

            # Then buys (price dropped → open new slots)
            for slot in self._slots:
                if not slot["filled"] and low <= slot["buy_price"]:
                    self._buy_slot(slot, slot["buy_price"])

            # Shift grid up if all slots are empty and price rallied well above the top level
            if not any(s["filled"] for s in self._slots):
                top_buy = self._slots[0]["buy_price"]
                if price > top_buy * (1 + self._spacing * 2):
                    self._init_grid(price)

            filled   = [s for s in self._slots if s["filled"]]
            port_val = self.balance + sum(s["amount"] * price for s in filled)
            if port_val > self.portfolio_peak:
                self.portfolio_peak = port_val
            log_balance(round(port_val, 2), self._db_mode)
            self._save()

    def check_stops(self, prices: dict):
        """Between-candle: fire any sell targets hit by current price."""
        with self._lock:
            pair  = (self.pairs or config.SPOT_TRADING_PAIRS)[0]
            price = prices.get(pair)
            if price is None:
                return
            changed = False
            for slot in self._slots:
                if slot["filled"] and price >= slot["sell_price"]:
                    self._sell_slot(slot, slot["sell_price"])
                    changed = True
            if price > self._highest_price:
                self._highest_price = price
                changed = True
            if changed:
                filled   = [s for s in self._slots if s["filled"]]
                port_val = self.balance + sum(s["amount"] * price for s in filled)
                if port_val > self.portfolio_peak:
                    self.portfolio_peak = port_val
                self._save()

    # --- persistence ---

    def _save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump({
                "name":             self.name,
                "started_at":       self.started_at,
                "overrides":        self.overrides,
                "config_fingerprint": self._fingerprint,
                "starting_balance":  self.starting_balance,
                "live_baseline_pct": self.live_baseline_pct,
                "balance":          self.balance,
                "total_trades":     self.total_trades,
                "total_pnl":        self.total_pnl,
                "total_fees":       self.total_fees,
                "portfolio_peak":   self.portfolio_peak,
                "grid_center":      self._grid_center,
                "highest_price":  self._highest_price,
                "slots":          self._slots,
            }, f, indent=2)

    def _load(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            if data.get("config_fingerprint") != self._fingerprint:
                log.info(f"[SHADOW] {self.name}: config changed — resetting trades and state")
                clear_shadow_trades(self._db_mode)
                os.remove(self.state_path)
                self.starting_balance = _live_spot_balance()
                self.balance = self.starting_balance
                self._slot_eur = self.starting_balance / self._n_levels
                return
            self.started_at        = data.get("started_at",        None)
            self.starting_balance  = data.get("starting_balance",  self.starting_balance)
            self.live_baseline_pct = data.get("live_baseline_pct", None)
            self.balance           = data.get("balance",           self.starting_balance)
            self.total_trades   = data.get("total_trades",   0)
            self.total_pnl      = data.get("total_pnl",      0.0)
            self.total_fees     = data.get("total_fees",     0.0)
            self.portfolio_peak = data.get("portfolio_peak", 0.0)
            self._grid_center   = data.get("grid_center",    None)
            self._highest_price = data.get("highest_price",  0.0)
            self._slots         = data.get("slots",          [])
        except Exception:
            pass

    def get_state(self) -> dict:
        return {
            "name":           self.name,
            "overrides":      self.overrides,
            "balance":        round(self.balance,        2),
            "total_trades":   self.total_trades,
            "total_pnl":      round(self.total_pnl,      2),
            "total_fees":     round(self.total_fees,     4),
            "portfolio_peak": round(self.portfolio_peak, 2),
            "positions":      {},
        }


# --- module-level shadow registry ---

_shadows: list[SpotShadowSimulator] = []


def init_shadows() -> list[SpotShadowSimulator]:
    global _shadows
    _shadows = []
    for name in config.get_shadow_profiles():
        overrides  = config.get_shadow_overrides(name)
        state_path = os.path.join(_DATA_DIR, f"spot_state_shadow_{name.lower()}.json")
        if overrides.get("type") == "grid":
            _shadows.append(GridShadowSimulator(name, state_path, overrides))
        else:
            _shadows.append(SpotShadowSimulator(name, state_path, overrides))
    if _shadows:
        names = ", ".join(s.name for s in _shadows)
        notify(f"[SHADOW] {len(_shadows)} shadow sim(s) loaded: {names}", discord=False)
    return _shadows


def get_shadows() -> list[SpotShadowSimulator]:
    return _shadows
