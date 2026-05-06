from dataclasses import dataclass
from . import config


@dataclass
class Position:
    pair: str
    entry_price: float
    amount: float
    value_eur: float
    stop_loss_price: float


def create_position(pair: str, entry_price: float, balance: float) -> Position:
    size = balance * config.POSITION_SIZE_PCT
    amount = size / entry_price
    stop_loss_price = entry_price * (1 - config.STOP_LOSS_PCT)
    return Position(pair, entry_price, amount, size, stop_loss_price)


def check_stop_loss(position: Position, current_price: float) -> bool:
    return current_price <= position.stop_loss_price


def calc_pnl(position: Position, exit_price: float) -> float:
    return (exit_price - position.entry_price) * position.amount
