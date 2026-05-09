import json
import os
from dataclasses import dataclass, field
from . import config
from .risk import Position, create_position, update_peak, check_trailing_stop, check_take_profit, calc_pnl
from .notifier import trade_alert, trailing_stop_alert
from .db import log_trade

BINANCE_FEE = config.BINANCE_FEE
STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "state.json")


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
            p = Position(**pos)
            if p.highest_price == 0.0:
                p.highest_price = p.entry_price
            _state.positions[pair] = p
    except Exception:
        pass  # corrupt state file — start fresh


def init():
    _load()


def get_state() -> SimState:
    return _state


def reset():
    global _state
    _state = SimState()
    _save()


def open_position(pair: str, price: float):
    if pair in _state.positions:
        return
    min_size = config.SIMULATION_BALANCE * config.POSITION_SIZE_PCT
    if _state.balance < min_size:
        return

    pos = create_position(pair, price, _state.balance)
    buy_fee = pos.value_eur * BINANCE_FEE
    _state.positions[pair] = pos
    _state.balance -= pos.value_eur + buy_fee
    _state.total_fees += buy_fee
    _save()

    trade_alert("BUY", pair, price, pos.amount, pos.value_eur, fee=buy_fee)
    log_trade(pair, "BUY", price, pos.amount, pos.value_eur, buy_fee, mode="simulation")


def close_position(pair: str, price: float, reason: str = "signal"):
    if pair not in _state.positions:
        return

    pos = _state.positions.pop(pair)
    exit_value = pos.amount * price
    buy_fee = pos.value_eur * BINANCE_FEE
    sell_fee = exit_value * BINANCE_FEE
    pnl = calc_pnl(pos, price, buy_fee=buy_fee, sell_fee=sell_fee)
    _state.balance += exit_value - sell_fee
    _state.total_trades += 1
    _state.total_pnl += pnl
    _state.total_fees += sell_fee
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
