import json
import os
import time as _time
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

_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "futures_state.json"
)


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
                "dca_done":          p.dca_done,
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
            _state.positions[sym] = FuturesPosition(**d)
    except Exception:
        pass  # corrupt state — start fresh


def init():
    if config.FUTURES_MODE == "live":
        _load()
        _state.balance = get_usdt_balance()
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
    if pos.dca_done or pos.side != "LONG":
        return

    if config.FUTURES_MODE == "live":
        balance = get_usdt_balance()
        _state.balance = balance
    else:
        balance = _state.balance

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
            close_long(symbol, pos.liquidation_price(), reason="liquidation")

        elif check_take_profit(pos, price):
            close_long(symbol, price, reason="take_profit")

        elif check_trailing_stop(pos, price):
            close_long(symbol, price, reason="trailing_stop")

        elif updated:
            _save()

    # Update portfolio peak for drawdown guard
    if config.FUTURES_MAX_DRAWDOWN_PCT > 0:
        pos_pnl = sum(
            p.unrealized_pnl(prices[s])
            for s, p in _state.positions.items() if s in prices
        )
        portfolio_value = _state.balance + sum(p.margin for p in _state.positions.values()) + pos_pnl
        if portfolio_value > _state.portfolio_peak:
            _state.portfolio_peak = portfolio_value
            _save()
