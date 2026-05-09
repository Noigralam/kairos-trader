from dataclasses import dataclass, field
from . import config


@dataclass
class Position:
    pair: str
    entry_price: float
    amount: float
    value_eur: float
    take_profit_price: float = 0.0
    highest_price: float = 0.0  # tracks peak for trailing stop; 0.0 means use entry_price

    def peak(self) -> float:
        return self.highest_price if self.highest_price > 0 else self.entry_price

    def trailing_stop_level(self) -> float:
        # floor guarantees at least 1% net profit after both fees
        fee_plus_profit_floor = self.entry_price * (1 + config.BINANCE_FEE + 0.01) / (1 - config.BINANCE_FEE)
        return max(fee_plus_profit_floor, self.peak() * (1 - config.TRAILING_STOP_PCT))


def create_position(pair: str, entry_price: float, balance: float) -> Position:
    size = balance * config.POSITION_SIZE_PCT
    amount = size / entry_price
    take_profit_price = entry_price * (1 + config.TAKE_PROFIT_PCT)
    return Position(pair, entry_price, amount, size, take_profit_price, entry_price)


def update_peak(position: Position, current_price: float) -> bool:
    """Update highest_price if price moved up. Returns True if updated."""
    if current_price > position.peak():
        position.highest_price = current_price
        return True
    return False


def check_trailing_stop(position: Position, current_price: float) -> bool:
    # only triggers once peak has risen above the floor (i.e. price has moved in our favour)
    return position.peak() > position.trailing_stop_level() and current_price <= position.trailing_stop_level()


def check_take_profit(position: Position, current_price: float) -> bool:
    return position.take_profit_price > 0 and current_price >= position.take_profit_price


def calc_pnl(position: Position, exit_price: float, buy_fee: float = 0.0, sell_fee: float = 0.0) -> float:
    return (exit_price - position.entry_price) * position.amount - buy_fee - sell_fee
