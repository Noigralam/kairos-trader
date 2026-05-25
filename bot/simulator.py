import json
import os
from dataclasses import dataclass, field
from . import config
from .risk import Position, create_position, apply_dca, update_peak, check_trailing_stop, check_take_profit, calc_pnl
from .notifier import trade_alert, trailing_stop_alert, notify
from .db import log_trade
from .exchange import round_qty, get_min_notional, get_eur_balance, place_order

BINANCE_FEE = config.BINANCE_FEE

_SIM_STATE_PATH  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "state.json")
_LIVE_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "state_live.json")
STATE_PATH = _LIVE_STATE_PATH if config.MODE == "live" else _SIM_STATE_PATH


@dataclass
class SimState:
    balance: float = field(default_factory=lambda: config.SIMULATION_BALANCE)
    positions: dict = field(default_factory=dict)   # pair -> Position
    total_trades: int = 0
    total_pnl: float = 0.0
    total_fees: float = 0.0


_state = SimState()


def _save():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    data = {
        "mode": config.MODE,
        "balance": _state.balance,
        "total_trades": _state.total_trades,
        "total_pnl": _state.total_pnl,
        "total_fees": _state.total_fees,
        "positions": {
            pair: {
                "pair": pos.pair,
                "entry_price": pos.entry_price,
                "amount": pos.amount,
                "value_eur": pos.value_eur,
                "take_profit_price": pos.take_profit_price,
                "highest_price": pos.highest_price,
                "dca_done": pos.dca_done,
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
        _state.balance = data.get("balance", config.SIMULATION_BALANCE)
        _state.total_trades = data.get("total_trades", 0)
        _state.total_pnl = data.get("total_pnl", 0.0)
        _state.total_fees = data.get("total_fees", 0.0)
        _state.positions = {}
        for pair, pos in data.get("positions", {}).items():
            pos.pop("stop_cooldown", None)
            p = Position(**pos)
            if p.highest_price == 0.0:
                p.highest_price = p.entry_price
            _state.positions[pair] = p
    except Exception:
        pass  # corrupt state file — start fresh


def init():
    if config.MODE == "live":
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

def open_position(pair: str, price: float, size_pct: float = None):
    if pair in _state.positions:
        return
    pct = size_pct if size_pct is not None else config.POSITION_SIZE_PCT

    if config.MODE == "live":
        balance = get_eur_balance()
        _state.balance = balance
    else:
        balance = _state.balance

    max_size = balance / (1 + BINANCE_FEE)
    size = min(balance * pct, max_size)
    min_notional = get_min_notional(pair)
    if size < min_notional:
        notify(f"[SKIP] {pair} buy signal — order size €{size:.2f} below minimum €{min_notional:.2f} (balance €{balance:.2f})")
        return

    if config.MODE == "live":
        amount = round_qty(pair, size / price)
        order  = place_order(pair, "BUY", amount)
        fills  = order.get("fills", [])
        if fills:
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / sum(float(f["qty"]) for f in fills)
            amount    = sum(float(f["qty"]) for f in fills)
            buy_fee   = sum(float(f["commission"]) for f in fills)
        else:
            avg_price = price
            buy_fee   = amount * avg_price * BINANCE_FEE
        value    = amount * avg_price
        tp_price = avg_price * (1 + config.TAKE_PROFIT_PCT)
        pos      = Position(pair, avg_price, amount, value, tp_price, avg_price)
        _state.positions[pair] = pos
        _state.total_fees += buy_fee
        _state.balance = get_eur_balance()
        _save()
        trade_alert("BUY", pair, avg_price, amount, value, fee=buy_fee)
        log_trade(pair, "BUY", avg_price, amount, value, buy_fee, mode="live")
    else:
        amount   = size / price
        amount   = round_qty(pair, amount)
        value    = amount * price
        buy_fee  = value * BINANCE_FEE
        tp_price = price * (1 + config.TAKE_PROFIT_PCT)
        pos      = Position(pair, price, amount, value, tp_price, price)
        _state.positions[pair] = pos
        _state.balance -= value + buy_fee
        _state.total_fees += buy_fee
        _save()
        trade_alert("BUY", pair, price, pos.amount, pos.value_eur, fee=buy_fee)
        log_trade(pair, "BUY", price, pos.amount, pos.value_eur, buy_fee, mode="simulation")


def manual_add(pair: str, price: float, size_pct: float):
    """Manual buy — opens new position or merges into existing one."""
    if config.MODE == "live":
        balance = get_eur_balance()
        _state.balance = balance
    else:
        balance = _state.balance

    max_size = balance / (1 + BINANCE_FEE)
    size = min(balance * size_pct, max_size)
    if size < 1:
        return

    buy_fee = size * BINANCE_FEE

    if config.MODE == "live":
        amount = round_qty(pair, size / price)
        order  = place_order(pair, "BUY", amount)
        fills  = order.get("fills", [])
        if fills:
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / sum(float(f["qty"]) for f in fills)
            amount    = sum(float(f["qty"]) for f in fills)
            buy_fee   = sum(float(f["commission"]) for f in fills)
        else:
            avg_price = price
            buy_fee   = amount * avg_price * BINANCE_FEE
        value = amount * avg_price
        if pair not in _state.positions:
            tp_price = avg_price * (1 + config.TAKE_PROFIT_PCT)
            pos = Position(pair, avg_price, amount, value, tp_price, avg_price)
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
            buy_fee  = value * BINANCE_FEE
            tp_price = price * (1 + config.TAKE_PROFIT_PCT)
            pos      = Position(pair, price, amount, value, tp_price, price)
            _state.positions[pair] = pos
            _state.balance -= value + buy_fee
            _state.total_fees += buy_fee
            _save()
            trade_alert("BUY", pair, price, pos.amount, pos.value_eur, fee=buy_fee)
            log_trade(pair, "BUY", price, pos.amount, pos.value_eur, buy_fee, mode="simulation")
        else:
            pos = _state.positions[pair]
            bought = size / price
            apply_dca(pos, price, size)
            _state.balance -= size + buy_fee
            _state.total_fees += buy_fee
            _save()
            trade_alert("BUY", pair, price, bought, size, fee=buy_fee)
            log_trade(pair, "BUY", price, bought, size, buy_fee, mode="simulation", notes="manual_add")


def dca_position(pair: str, price: float):
    if pair not in _state.positions:
        return
    pos = _state.positions[pair]
    if pos.dca_done:
        return

    if config.MODE == "live":
        balance = get_eur_balance()
        _state.balance = balance
    else:
        balance = _state.balance

    max_size  = balance / (1 + BINANCE_FEE)
    dca_value = min(balance * config.DCA_SIZE_PCT, max_size)
    min_notional = get_min_notional(pair)
    if dca_value < min_notional:
        notify(f"[SKIP] {pair} DCA — order size €{dca_value:.2f} below minimum €{min_notional:.2f} (balance €{balance:.2f})")
        return

    if config.MODE == "live":
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
            buy_fee   = dca_value * BINANCE_FEE
        apply_dca(pos, avg_price, dca_value)
        _state.total_fees += buy_fee
        _state.balance = get_eur_balance()
        _save()
        trade_alert("DCA", pair, avg_price, bought, dca_value, fee=buy_fee)
        log_trade(pair, "DCA", avg_price, bought, dca_value, buy_fee, mode="live")
    else:
        buy_fee = dca_value * BINANCE_FEE
        bought  = dca_value / price
        apply_dca(pos, price, dca_value)
        _state.balance -= dca_value + buy_fee
        _state.total_fees += buy_fee
        _save()
        trade_alert("DCA", pair, price, bought, dca_value, fee=buy_fee)
        log_trade(pair, "DCA", price, bought, dca_value, buy_fee, mode="simulation")


def close_position(pair: str, price: float, reason: str = "signal"):
    if pair not in _state.positions:
        return

    pos = _state.positions.pop(pair)

    if config.MODE == "live":
        order  = place_order(pair, "SELL", pos.amount)
        fills  = order.get("fills", [])
        if fills:
            avg_price  = sum(float(f["price"]) * float(f["qty"]) for f in fills) / sum(float(f["qty"]) for f in fills)
            sold_qty   = sum(float(f["qty"]) for f in fills)
            sell_fee   = sum(float(f["commission"]) for f in fills)
        else:
            avg_price = price
            sold_qty  = pos.amount
            sell_fee  = pos.amount * price * BINANCE_FEE
        exit_value = sold_qty * avg_price
        buy_fee    = pos.value_eur * BINANCE_FEE
        pnl        = calc_pnl(pos, avg_price, buy_fee=buy_fee, sell_fee=sell_fee)
        _state.total_trades += 1
        _state.total_pnl    += pnl
        _state.total_fees   += sell_fee
        _state.balance = get_eur_balance()
        _save()
        if reason == "trailing_stop":
            trailing_stop_alert(pair, avg_price, pnl)
        else:
            trade_alert("SELL", pair, avg_price, sold_qty, exit_value, pnl=pnl, fee=sell_fee)
        log_trade(pair, "SELL", avg_price, sold_qty, exit_value, sell_fee, mode="live", pnl=pnl, notes=reason)
    else:
        exit_value = pos.amount * price
        buy_fee    = pos.value_eur * BINANCE_FEE
        sell_fee   = exit_value * BINANCE_FEE
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
    for pair, pos in list(_state.positions.items()):
        if pair not in prices:
            continue
        price = prices[pair]
        updated = update_peak(pos, price)
        if check_take_profit(pos, price):
            close_position(pair, price, reason="take_profit")
        elif check_trailing_stop(pos, price):
            close_position(pair, price, reason="trailing_stop")
        elif updated:
            _save()
