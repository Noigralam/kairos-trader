from dataclasses import dataclass, field
from . import config
from .risk import Position, create_position, check_stop_loss, calc_pnl
from .notifier import trade_alert, stop_loss_alert
from .db import log_trade

BINANCE_FEE = 0.001  # 0.1%


@dataclass
class SimState:
    balance: float = field(default_factory=lambda: config.SIMULATION_BALANCE)
    positions: dict = field(default_factory=dict)   # pair -> Position
    total_trades: int = 0
    total_pnl: float = 0.0


_state = SimState()


def get_state() -> SimState:
    return _state


def reset():
    global _state
    _state = SimState()


def open_position(pair: str, price: float):
    if pair in _state.positions:
        return
    min_size = config.SIMULATION_BALANCE * config.POSITION_SIZE_PCT
    if _state.balance < min_size:
        return

    pos = create_position(pair, price, _state.balance)
    fee = pos.value_eur * BINANCE_FEE
    _state.positions[pair] = pos
    _state.balance -= pos.value_eur + fee

    trade_alert("BUY", pair, price, pos.amount, pos.value_eur)
    log_trade(pair, "BUY", price, pos.amount, pos.value_eur, fee, mode="simulation")


def close_position(pair: str, price: float, reason: str = "signal"):
    if pair not in _state.positions:
        return

    pos = _state.positions.pop(pair)
    pnl = calc_pnl(pos, price)
    exit_value = pos.amount * price
    fee = exit_value * BINANCE_FEE
    _state.balance += exit_value - fee
    _state.total_trades += 1
    _state.total_pnl += pnl

    if reason == "stop_loss":
        stop_loss_alert(pair, price, abs(pnl))
    else:
        trade_alert("SELL", pair, price, pos.amount, exit_value, pnl)

    log_trade(pair, "SELL", price, pos.amount, exit_value, fee,
              mode="simulation", pnl=pnl, notes=reason)


def check_stops(prices: dict):
    for pair, pos in list(_state.positions.items()):
        if pair in prices and check_stop_loss(pos, prices[pair]):
            close_position(pair, prices[pair], reason="stop_loss")
