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
    dca_count: int = 0
    opened_at: float = 0.0      # unix timestamp of entry (0 = unknown)
    partial_closed: bool = False  # True after partial TP sell; remainder runs on tighter trail

    def peak(self) -> float:
        return self.highest_price if self.highest_price > 0 else self.entry_price

    def trailing_stop_level(self, floor_pct: float | None = None, trail_pct: float | None = None) -> float:
        # Minimum exit price that clears both buy+sell fees AND the profit floor.
        # Dividing by (1 - fee) converts from "net proceeds needed" to "gross exit price"
        # because the sell fee is taken from proceeds, not added on top.
        _floor = floor_pct if floor_pct is not None else config.SPOT_PROFIT_FLOOR_PCT
        _trail = trail_pct if trail_pct is not None else config.SPOT_TRAILING_STOP_PCT
        fee_plus_profit_floor = self.entry_price * (1 + config.SPOT_FEE + _floor) / (1 - config.SPOT_FEE)
        return max(fee_plus_profit_floor, self.peak() * (1 - _trail))


def apply_dca(position: Position, dca_price: float, dca_value_eur: float, tp_pct: float | None = None) -> None:
    """Merge a second tranche, updating weighted average entry price."""
    dca_amount = dca_value_eur / dca_price
    total_value = position.value_eur + dca_value_eur
    total_amount = position.amount + dca_amount
    position.entry_price = total_value / total_amount
    position.amount = total_amount
    position.value_eur = total_value
    _tp = tp_pct if tp_pct is not None else config.SPOT_TAKE_PROFIT_PCT
    position.take_profit_price = position.entry_price * (1 + _tp)
    position.highest_price = dca_price  # reset peak from new blended entry; old peak is irrelevant after cost basis shifts
    position.dca_count += 1



def update_peak(position: Position, current_price: float) -> bool:
    """Update highest_price if price moved up. Returns True if updated."""
    if current_price > position.peak():
        position.highest_price = current_price
        return True
    return False


def check_trailing_stop(position: Position, current_price: float, floor_pct: float | None = None, trail_pct: float | None = None) -> bool:
    stop = position.trailing_stop_level(floor_pct, trail_pct)
    return position.peak() > stop and current_price <= stop


def check_take_profit(position: Position, current_price: float) -> bool:
    return position.take_profit_price > 0 and current_price >= position.take_profit_price

def check_hard_stop(position: Position, current_price: float, hard_stop_pct: float) -> bool:
    return hard_stop_pct > 0 and current_price <= position.entry_price * (1 - hard_stop_pct)


def calc_pnl(position: Position, exit_price: float, buy_fee: float = 0.0, sell_fee: float = 0.0) -> float:
    return (exit_price - position.entry_price) * position.amount - buy_fee - sell_fee
