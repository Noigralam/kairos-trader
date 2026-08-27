import json
import logging
import os
import threading
import time as _time

log = logging.getLogger("cryptobot")
from dataclasses import dataclass, field
from . import config
from .futures_risk import (
    FuturesPosition, apply_dca, update_peak,
    check_take_profit, check_trailing_stop, check_liquidation, calc_pnl,
)
from .futures_exchange import (
    get_usdt_balance, place_order, close_position as exchange_close,
    set_leverage, set_margin_type, get_mark_price, get_funding_rate, round_qty,
)
from .notifier import notify, trade_alert, trailing_stop_alert
from .db import log_trade

FEE = config.FUTURES_FEE
_check_lock = threading.Lock()

_SIM_STATE_PATH  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "futures_state_simulation.json")
_LIVE_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "futures_state_live.json")
_STATE_PATH = _LIVE_STATE_PATH if config.FUTURES_MODE == "live" else _SIM_STATE_PATH


@dataclass
class FuturesSimState:
    balance:        float = field(default_factory=lambda: config.FUTURES_SIMULATION_BALANCE)
    positions:      dict  = field(default_factory=dict)   # symbol -> FuturesPosition
    total_trades:   int   = 0
    total_pnl:      float = 0.0
    total_fees:     float = 0.0
    total_funding:  float = 0.0
    portfolio_peak: float = 0.0


_state = FuturesSimState()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _save():
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    data = {
        "mode":           config.FUTURES_MODE,
        "balance":        _state.balance,
        "total_trades":   _state.total_trades,
        "total_pnl":      _state.total_pnl,
        "total_fees":     _state.total_fees,
        "total_funding":  _state.total_funding,
        "portfolio_peak": _state.portfolio_peak,
        "positions": {
            sym: {
                "symbol":            p.symbol,
                "side":              p.side,
                "entry_price":       p.entry_price,
                "amount":            p.amount,
                "margin":            p.margin,
                "leverage":          p.leverage,
                "take_profit_price": p.take_profit_price,
                "highest_price":     p.highest_price,
                "dca_count":         p.dca_count,
                "funding_paid":      p.funding_paid,
                "opened_at":         p.opened_at,
            }
            for sym, p in _state.positions.items()
        },
    }
    with open(_STATE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _load():
    global _state
    if not os.path.exists(_STATE_PATH):
        return
    try:
        with open(_STATE_PATH) as f:
            data = json.load(f)
        _state.balance        = data.get("balance", config.FUTURES_SIMULATION_BALANCE)
        _state.total_trades   = data.get("total_trades", 0)
        _state.total_pnl      = data.get("total_pnl", 0.0)
        _state.total_fees     = data.get("total_fees", 0.0)
        _state.total_funding  = data.get("total_funding", 0.0)
        _state.portfolio_peak = data.get("portfolio_peak", 0.0)
        _state.positions = {}
        for sym, d in data.get("positions", {}).items():
            if "dca_done" in d:
                d["dca_count"] = 1 if d.pop("dca_done") else 0
            _state.positions[sym] = FuturesPosition(**d)
    except Exception:
        pass  # corrupt state — start fresh


def init():
    if config.FUTURES_MODE == "live":
        _load()
        try:
            _state.balance = get_usdt_balance()
        except Exception as e:
            log.warning(f"[FUTURES] Binance balance fetch failed at startup ({e}) — using last saved balance ${_state.balance:.2f}")
        # Ensure leverage and margin type are set for each pair
        for sym in config.FUTURES_TRADING_PAIRS:
            try:
                set_margin_type(sym, config.FUTURES_MARGIN_TYPE)
                set_leverage(sym, config.FUTURES_LEVERAGE)
            except Exception as e:
                notify(f"[FUTURES INIT] {sym} setup warning: {e}", discord=False)
        _save()
        notify(
            f"[FUTURES] Started — USDT balance: ${_state.balance:.2f}  "
            f"leverage: {config.FUTURES_LEVERAGE}x  "
            f"open positions: {list(_state.positions.keys()) or 'none'}"
        )
    else:
        _load()
        _save()  # anchor starting balance on first run; no-op if file already exists with same state


def get_state() -> FuturesSimState:
    return _state


def reset():
    global _state
    _state = FuturesSimState()
    _save()


# ---------------------------------------------------------------------------
# Position management
# ---------------------------------------------------------------------------

def open_long(symbol: str, price: float, size_pct: float = None):
    if symbol in _state.positions:
        return

    pct = size_pct if size_pct is not None else config.FUTURES_POSITION_SIZE_PCT

    if config.FUTURES_MODE == "live":
        balance = get_usdt_balance()
        _state.balance = balance
    else:
        balance = _state.balance

    if config.FUTURES_MAX_DRAWDOWN_PCT > 0 and _state.portfolio_peak > 0:
        drawdown = (_state.portfolio_peak - balance) / _state.portfolio_peak
        if drawdown > config.FUTURES_MAX_DRAWDOWN_PCT:
            notify(
                f"[FUTURES SKIP] {symbol} buy blocked — drawdown "
                f"{drawdown*100:.1f}% exceeds {config.FUTURES_MAX_DRAWDOWN_PCT*100:.0f}%",
                discord=False,
            )
            return

    margin    = balance * pct
    notional  = margin * config.FUTURES_LEVERAGE
    amount    = round_qty(symbol, notional / price)
    if amount <= 0 or margin < 1:
        notify(f"[FUTURES SKIP] {symbol} — margin too small (${margin:.2f})", discord=False)
        return

    entry_fee = notional * FEE
    tp_price  = price * (1 + config.FUTURES_TAKE_PROFIT_PCT)

    if config.FUTURES_MODE == "live":
        order     = place_order(symbol, "BUY", amount)
        fills     = order.get("fills", [])
        avg_price = (sum(float(f["price"]) * float(f["qty"]) for f in fills)
                     / sum(float(f["qty"]) for f in fills)) if fills else price
        amount    = sum(float(f["qty"]) for f in fills) if fills else amount
        entry_fee = sum(float(f["commission"]) for f in fills) if fills else entry_fee
        notional  = avg_price * amount
        margin    = notional / config.FUTURES_LEVERAGE
        tp_price  = avg_price * (1 + config.FUTURES_TAKE_PROFIT_PCT)
        price     = avg_price

    pos = FuturesPosition(
        symbol=symbol, side="LONG",
        entry_price=price, amount=amount, margin=margin,
        leverage=config.FUTURES_LEVERAGE,
        take_profit_price=tp_price, highest_price=price,
        opened_at=_time.time(),
    )
    _state.positions[symbol] = pos
    _state.balance   -= margin + entry_fee
    _state.total_fees += entry_fee
    _save()

    notify(
        f"[FUTURES BUY] {symbol} LONG  {amount} @ ${price:,.4f}  "
        f"margin ${margin:.2f}  notional ${notional:.2f}  "
        f"liq ${pos.liquidation_price():,.2f}  fee ${entry_fee:.4f}"
    )
    log_trade(symbol, "BUY", price, amount, notional, entry_fee,
              mode=f"futures_{config.FUTURES_MODE}",
              notes=f"lev={config.FUTURES_LEVERAGE}x")


def dca_long(symbol: str, price: float):
    if symbol not in _state.positions:
        return
    pos = _state.positions[symbol]
    if pos.dca_count > 0 or pos.side != "LONG":
        return

    if config.FUTURES_MODE == "live":
        balance = get_usdt_balance()
        _state.balance = balance
    else:
        balance = _state.balance

    if config.FUTURES_MAX_DRAWDOWN_PCT > 0 and _state.portfolio_peak > 0:
        drawdown = (_state.portfolio_peak - balance) / _state.portfolio_peak
        if drawdown > config.FUTURES_MAX_DRAWDOWN_PCT:
            notify(f"[FUTURES SKIP] {symbol} DCA blocked — drawdown {drawdown*100:.1f}% exceeds {config.FUTURES_MAX_DRAWDOWN_PCT*100:.0f}%", discord=False)
            return

    dca_margin  = min(balance * config.FUTURES_DCA_SIZE_PCT, balance)
    if dca_margin < 1:
        return
    dca_notional = dca_margin * config.FUTURES_LEVERAGE
    dca_amount   = round_qty(symbol, dca_notional / price)
    entry_fee    = dca_notional * FEE

    if config.FUTURES_MODE == "live":
        order    = place_order(symbol, "BUY", dca_amount)
        fills    = order.get("fills", [])
        price    = (sum(float(f["price"]) * float(f["qty"]) for f in fills)
                    / sum(float(f["qty"]) for f in fills)) if fills else price
        dca_amount   = sum(float(f["qty"]) for f in fills) if fills else dca_amount
        entry_fee    = sum(float(f["commission"]) for f in fills) if fills else entry_fee
        dca_notional = price * dca_amount
        dca_margin   = dca_notional / config.FUTURES_LEVERAGE

    apply_dca(pos, price, dca_margin)
    _state.balance    -= dca_margin + entry_fee
    _state.total_fees += entry_fee
    _save()

    notify(
        f"[FUTURES DCA] {symbol}  +{dca_amount} @ ${price:,.4f}  "
        f"new avg ${pos.entry_price:,.4f}  liq ${pos.liquidation_price():,.2f}"
    )
    log_trade(symbol, "BUY", price, dca_amount, dca_notional, entry_fee,
              mode=f"futures_{config.FUTURES_MODE}", notes="dca")


def close_long(symbol: str, price: float, reason: str = "signal"):
    if symbol not in _state.positions:
        return
    pos = _state.positions.pop(symbol)

    notional  = price * pos.amount
    exit_fee  = notional * FEE
    entry_fee = pos.entry_price * pos.amount * FEE
    pnl       = calc_pnl(pos, price, entry_fee=entry_fee, exit_fee=exit_fee)

    if config.FUTURES_MODE == "live":
        exchange_close(symbol)
        _state.balance = get_usdt_balance()
    else:
        returned_margin = pos.margin + pnl
        # Isolated margin: max loss is the posted margin; liquidation should have fired first,
        # but floor at 0 as a safety net so a bad pnl never drives balance negative.
        _state.balance += max(returned_margin, 0)

    _state.total_trades  += 1
    _state.total_pnl     += pnl
    _state.total_fees    += exit_fee
    _state.total_funding += pos.funding_paid
    _save()

    pnl_pct = pnl / pos.margin * 100 if pos.margin else 0
    notify(
        f"[FUTURES SELL] {symbol} LONG closed @ ${price:,.4f}  "
        f"PnL ${pnl:+.4f} ({pnl_pct:+.1f}%)  reason={reason}  fee ${exit_fee:.4f}"
    )
    log_trade(symbol, "SELL", price, pos.amount, notional, exit_fee,
              mode=f"futures_{config.FUTURES_MODE}", pnl=round(pnl, 4), notes=reason)


# ---------------------------------------------------------------------------
# Stop / funding checks (called every tick)
# ---------------------------------------------------------------------------

def apply_funding(symbol: str, rate: float):
    """
    Deduct (or credit) funding from an open long position.
    Positive rate = long pays short. Negative = long receives.
    """
    if symbol not in _state.positions:
        return
    pos     = _state.positions[symbol]
    # Funding is charged on notional (entry_price × qty), not on margin.
    # Using margin × rate would undercount by a factor of leverage.
    cost    = pos.entry_price * pos.amount * rate
    pos.funding_paid  += cost
    _state.total_funding += cost
    if abs(cost) > 0.001:
        notify(
            f"[FUTURES FUNDING] {symbol}  rate={rate*100:.4f}%  "
            f"cost ${cost:+.4f}  total paid ${pos.funding_paid:.4f}",
            discord=False,
        )
    _save()


def check_stops(prices: dict):
    with _check_lock:
        now = _time.time()
        for symbol, pos in list(_state.positions.items()):
            if symbol not in prices:
                continue
            price = prices[symbol]
            updated = update_peak(pos, price)

            if check_liquidation(pos, price):
                notify(
                    f"[FUTURES LIQUIDATION] {symbol} — price ${price:,.4f} hit "
                    f"liquidation ${pos.liquidation_price():,.2f}. Closing."
                )
                close_long(symbol, price, reason="liquidation")

            elif check_take_profit(pos, price):
                close_long(symbol, price, reason="take_profit")

            elif check_trailing_stop(pos, price):
                close_long(symbol, price, reason="trailing_stop")

            elif updated:
                _save()

        # Update portfolio peak for drawdown guard (only positions with prices to avoid overstating value on partial fetch)
        if config.FUTURES_MAX_DRAWDOWN_PCT > 0:
            pos_pnl = sum(
                p.unrealized_pnl(prices[s])
                for s, p in _state.positions.items() if s in prices
            )
            portfolio_value = _state.balance + sum(
                p.margin for s, p in _state.positions.items() if s in prices
            ) + pos_pnl
            if portfolio_value > _state.portfolio_peak:
                _state.portfolio_peak = portfolio_value
                _save()


# ---------------------------------------------------------------------------
# Futures shadow simulators
# ---------------------------------------------------------------------------

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def _live_futures_balance() -> float:
    try:
        with open(_LIVE_STATE_PATH) as f:
            return float(json.load(f).get("balance", config.FUTURES_SHADOW_DEFAULT_BALANCE))
    except Exception:
        return config.FUTURES_SHADOW_DEFAULT_BALANCE


class FuturesShadowSimulator:
    """Lightweight futures simulation with configurable overrides.
    Receives the same candle ticks as the main engine; never touches the exchange."""

    def __init__(self, name: str, state_path: str, overrides: dict):
        self.name       = name
        self.state_path = state_path
        self.overrides  = overrides
        self.symbols: list[str] = overrides.get("symbols", list(config.FUTURES_TRADING_PAIRS))
        self._lock      = threading.Lock()

        starting = float(overrides.get("futures_balance", _live_futures_balance()))
        self._starting      = starting
        self.balance        = starting
        self.positions: dict[str, FuturesPosition] = {}
        self.total_trades   = 0
        self.total_pnl      = 0.0
        self.total_fees     = 0.0
        self.total_funding  = 0.0
        self.portfolio_peak = 0.0
        self.started_at: str | None = None
        self.main_baseline_pct: float | None = None
        self._load()
        if self.started_at is None:
            import datetime, zoneinfo
            self.started_at = datetime.datetime.now(tz=zoneinfo.ZoneInfo("Europe/Helsinki")).isoformat()
            self._save()

    def _o(self, key, default):
        return self.overrides.get(key, default)

    def set_main_baseline(self, pct: float):
        if self.main_baseline_pct is None:
            self.main_baseline_pct = pct
            self._save()

    @property
    def _db_mode(self) -> str:
        return f"futures_shadow_{self.name.lower()}"

    # --- position ops ---

    def _open(self, symbol: str, price: float):
        if symbol in self.positions:
            return
        leverage = self._o("futures_leverage",           config.FUTURES_LEVERAGE)
        pct      = self._o("futures_position_size_pct", config.FUTURES_POSITION_SIZE_PCT)
        margin   = self.balance * pct
        notional = margin * leverage
        amount   = round_qty(symbol, notional / price)
        if amount <= 0 or margin < 1:
            return
        fee      = notional * FEE
        tp_pct    = self._o("futures_take_profit_pct",    config.FUTURES_TAKE_PROFIT_PCT)
        trail_pct = self._o("futures_trailing_stop_pct",  0.0)
        floor_pct = self._o("futures_profit_floor_pct",   0.0)
        pos = FuturesPosition(
            symbol=symbol, side="LONG",
            entry_price=price, amount=amount, margin=margin,
            leverage=leverage,
            take_profit_price=price * (1 + tp_pct),
            highest_price=price,
            opened_at=_time.time(),
            trail_pct=trail_pct,
            floor_pct=floor_pct,
        )
        self.positions[symbol]  = pos
        self.balance           -= margin + fee
        self.total_fees        += fee
        log_trade(symbol, "BUY", price, amount, notional, fee,
                  mode=self._db_mode, notes=f"lev={leverage}x")

    def _dca(self, symbol: str, price: float):
        pos = self.positions.get(symbol)
        if pos is None or pos.dca_count > 0:
            return
        margin   = min(self.balance * self._o("futures_dca_size_pct", config.FUTURES_DCA_SIZE_PCT), self.balance)
        if margin < 1:
            return
        leverage = self._o("futures_leverage", config.FUTURES_LEVERAGE)
        notional = margin * leverage
        amount   = round_qty(symbol, notional / price)
        fee      = notional * FEE
        apply_dca(pos, price, margin)
        self.balance    -= margin + fee
        self.total_fees += fee
        log_trade(symbol, "BUY", price, amount, notional, fee,
                  mode=self._db_mode, notes="dca")

    def _close(self, symbol: str, price: float, reason: str = "signal"):
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return
        notional  = price * pos.amount
        exit_fee  = notional * FEE
        entry_fee = pos.entry_price * pos.amount * FEE
        pnl       = calc_pnl(pos, price, entry_fee=entry_fee, exit_fee=exit_fee)
        self.balance        += max(pos.margin + pnl, 0)
        self.total_trades   += 1
        self.total_pnl      += pnl
        self.total_fees     += exit_fee
        self.total_funding  += pos.funding_paid
        log_trade(symbol, "SELL", price, pos.amount, notional, exit_fee,
                  mode=self._db_mode, pnl=round(pnl, 4), notes=reason)

    # --- engine interface ---

    def tick(self, symbol: str, price: float, df):
        from .strategy import compute_signal, Signal
        with self._lock:
            pos = self.positions.get(symbol)
            if pos:
                update_peak(pos, price)
                if check_liquidation(pos, price):
                    self._close(symbol, price, "liquidation")
                elif check_take_profit(pos, price):
                    self._close(symbol, price, "take_profit")
                elif check_trailing_stop(pos, price):
                    self._close(symbol, price, "trailing_stop")
                else:
                    drop = (pos.entry_price - price) / pos.entry_price
                    if pos.dca_count == 0 and drop >= self._o("futures_dca_drop_pct", config.FUTURES_DCA_DROP_PCT):
                        self._dca(symbol, price)
            else:
                result = compute_signal(
                    df,
                    rsi_period     = self._o("futures_rsi_period",     config.futures_rsi_period_for(symbol)),
                    rsi_oversold   = self._o("futures_rsi_oversold",   config.futures_rsi_oversold_for(symbol)),
                    rsi_overbought = self._o("futures_rsi_overbought", config.futures_rsi_overbought_for(symbol)),
                    ema_gap        = self._o("futures_ema_gap_pct",    config.futures_ema_gap_for(symbol)),
                )
                if result.signal == Signal.BUY:
                    max_funding = self._o("futures_max_funding_rate", config.FUTURES_MAX_FUNDING_RATE)
                    if max_funding > 0:
                        try:
                            rate = get_funding_rate(symbol)
                            if rate > max_funding:
                                return
                        except Exception:
                            pass
                    self._open(symbol, price)
            self._save()

    def apply_funding(self, symbol: str, rate: float):
        if symbol not in self.positions:
            return
        pos  = self.positions[symbol]
        cost = pos.entry_price * pos.amount * rate
        pos.funding_paid    += cost
        self.total_funding  += cost
        self._save()

    def check_stops(self, prices: dict):
        with self._lock:
            changed = False
            for symbol, pos in list(self.positions.items()):
                price = prices.get(symbol)
                if price is None:
                    continue
                update_peak(pos, price)
                if check_liquidation(pos, price):
                    self._close(symbol, price, "liquidation"); changed = True
                elif check_take_profit(pos, price):
                    self._close(symbol, price, "take_profit"); changed = True
                elif check_trailing_stop(pos, price):
                    self._close(symbol, price, "trailing_stop"); changed = True
            if changed:
                self._save()

    def log_snapshot(self, prices: dict):
        from .db import log_balance
        open_pnl = sum(p.unrealized_pnl(prices[s]) for s, p in self.positions.items() if s in prices)
        port_val = self.balance + open_pnl
        if port_val > self.portfolio_peak:
            self.portfolio_peak = port_val
        log_balance(round(port_val, 2), self._db_mode)

    # --- persistence ---

    def _save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump({
                "name":             self.name,
                "started_at":       self.started_at,
                "overrides":        self.overrides,
                "starting_balance":   self._starting,
                "main_baseline_pct": self.main_baseline_pct,
                "balance":          self.balance,
                "total_trades":   self.total_trades,
                "total_pnl":      self.total_pnl,
                "total_fees":     self.total_fees,
                "total_funding":  self.total_funding,
                "portfolio_peak": self.portfolio_peak,
                "positions": {
                    sym: {
                        "symbol":            p.symbol,
                        "side":              p.side,
                        "entry_price":       p.entry_price,
                        "amount":            p.amount,
                        "margin":            p.margin,
                        "leverage":          p.leverage,
                        "take_profit_price": p.take_profit_price,
                        "highest_price":     p.highest_price,
                        "dca_count":         p.dca_count,
                        "funding_paid":      p.funding_paid,
                        "opened_at":         p.opened_at,
                        "trail_pct":         p.trail_pct,
                        "floor_pct":         p.floor_pct,
                    }
                    for sym, p in self.positions.items()
                },
            }, f, indent=2)

    def _load(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            self.started_at        = data.get("started_at",        None)
            self._starting         = data.get("starting_balance",  self._starting)
            self.main_baseline_pct = data.get("main_baseline_pct", None)
            self.balance           = data.get("balance",           self._starting)
            self.total_trades   = data.get("total_trades",   0)
            self.total_pnl      = data.get("total_pnl",      0.0)
            self.total_fees     = data.get("total_fees",     0.0)
            self.total_funding  = data.get("total_funding",  0.0)
            self.portfolio_peak = data.get("portfolio_peak", 0.0)
            self.positions = {}
            for sym, d in data.get("positions", {}).items():
                d.setdefault("trail_pct", 0.0)
                d.setdefault("floor_pct", 0.0)
                if "dca_done" in d:
                    d["dca_count"] = 1 if d.pop("dca_done") else 0
                self.positions[sym] = FuturesPosition(**d)
        except Exception:
            pass


_futures_shadows: list[FuturesShadowSimulator] = []
_futures_shadows_initialized: bool = False


def init_futures_shadows() -> list[FuturesShadowSimulator]:
    global _futures_shadows, _futures_shadows_initialized
    _futures_shadows_initialized = True
    _futures_shadows = []
    for name in config.get_futures_shadow_profiles():
        overrides  = config.get_futures_shadow_overrides(name)
        state_path = os.path.join(_DATA_DIR, f"futures_state_shadow_{name.lower()}.json")
        _futures_shadows.append(FuturesShadowSimulator(name, state_path, overrides))
    if _futures_shadows:
        names = ", ".join(s.name for s in _futures_shadows)
        notify(f"[FUTURES SHADOW] {len(_futures_shadows)} shadow(s) loaded: {names}", discord=False)
    return _futures_shadows


def get_futures_shadows() -> list[FuturesShadowSimulator]:
    if not _futures_shadows_initialized:
        init_futures_shadows()
    return _futures_shadows
