from dataclasses import dataclass, field
from . import config


@dataclass
class FuturesPosition:
    symbol:            str
    side:              str    # "LONG" | "SHORT"
    entry_price:       float
    amount:            float  # contract quantity (base asset units)
    margin:            float  # USDT margin posted = notional / leverage
    leverage:          int
    take_profit_price: float = 0.0
    highest_price:     float = 0.0   # long: tracks peak; short: tracks trough
    dca_done:          bool  = False
    funding_paid:      float = 0.0   # cumulative funding cost in USDT (positive = paid out)
    opened_at:         float = 0.0   # unix timestamp

    @property
    def notional(self) -> float:
        return self.entry_price * self.amount

    def peak(self) -> float:
        return self.highest_price if self.highest_price > 0 else self.entry_price

    def trailing_stop_level(self) -> float:
        # floor: minimum exit price that covers both entry+exit fees and the profit floor.
        # Dividing by (1-fee) converts net-needed to gross exit price (sell fee is a deduction, not addition).
        fee_plus_floor = self.entry_price * (1 + config.FUTURES_FEE + config.FUTURES_PROFIT_FLOOR_PCT) / (1 - config.FUTURES_FEE)
        if self.side == "LONG":
            return max(fee_plus_floor, self.peak() * (1 - config.FUTURES_TRAILING_STOP_PCT))
        else:  # SHORT: symmetric ceiling below entry
            ceiling = self.entry_price * (1 - config.FUTURES_FEE - config.FUTURES_PROFIT_FLOOR_PCT) / (1 + config.FUTURES_FEE)
            return min(ceiling, self.peak() * (1 + config.FUTURES_TRAILING_STOP_PCT))

    def liquidation_price(self) -> float:
        """
        Approximate isolated-margin liquidation price.
        Long:  entry * (1 - 1/leverage + maintenance_margin_rate)
        Short: entry * (1 + 1/leverage - maintenance_margin_rate)
        Uses 0.5% maintenance margin rate (conservative for most symbols).
        """
        mmr = 0.005  # 0.5% is conservative but correct for most Tier-1 notionals on ETH/SOL
        if self.side == "LONG":
            return self.entry_price * (1 - 1 / self.leverage + mmr)
        else:
            return self.entry_price * (1 + 1 / self.leverage - mmr)

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "LONG":
            return (current_price - self.entry_price) * self.amount
        else:
            return (self.entry_price - current_price) * self.amount

    def pnl_pct(self, current_price: float) -> float:
        """PnL as % of margin posted (i.e. leveraged return)."""
        return self.unrealized_pnl(current_price) / self.margin if self.margin else 0.0


def apply_dca(pos: FuturesPosition, dca_price: float, dca_margin: float) -> None:
    """
    Merge a second tranche into an existing position.
    dca_margin — additional USDT margin being posted.
    """
    dca_amount   = (dca_margin * pos.leverage) / dca_price
    total_amount = pos.amount + dca_amount
    total_margin = pos.margin + dca_margin
    # weighted average entry
    pos.entry_price        = (pos.entry_price * pos.amount + dca_price * dca_amount) / total_amount
    pos.amount             = total_amount
    pos.margin             = total_margin
    pos.take_profit_price  = pos.entry_price * (1 + config.FUTURES_TAKE_PROFIT_PCT) if pos.side == "LONG" \
                             else pos.entry_price * (1 - config.FUTURES_TAKE_PROFIT_PCT)
    pos.highest_price      = dca_price
    pos.dca_done           = True


def update_peak(pos: FuturesPosition, current_price: float) -> bool:
    """Update highest_price for trailing stop tracking. Returns True if updated."""
    if pos.side == "LONG" and current_price > pos.peak():
        pos.highest_price = current_price
        return True
    if pos.side == "SHORT" and (pos.highest_price == 0 or current_price < pos.highest_price):
        pos.highest_price = current_price
        return True
    return False


def check_take_profit(pos: FuturesPosition, current_price: float) -> bool:
    if pos.take_profit_price <= 0:
        return False
    if pos.side == "LONG":
        return current_price >= pos.take_profit_price
    else:
        return current_price <= pos.take_profit_price


def check_trailing_stop(pos: FuturesPosition, current_price: float) -> bool:
    stop = pos.trailing_stop_level()
    if pos.side == "LONG":
        return pos.peak() > stop and current_price <= stop
    else:
        return pos.peak() < stop and current_price >= stop


def check_liquidation(pos: FuturesPosition, current_price: float) -> bool:
    liq = pos.liquidation_price()
    if pos.side == "LONG":
        return current_price <= liq
    else:
        return current_price >= liq


def calc_pnl(pos: FuturesPosition, exit_price: float,
             entry_fee: float = 0.0, exit_fee: float = 0.0) -> float:
    """Realised PnL in USDT after fees and funding costs."""
    raw = (exit_price - pos.entry_price) * pos.amount if pos.side == "LONG" \
          else (pos.entry_price - exit_price) * pos.amount
    return raw - entry_fee - exit_fee - pos.funding_paid
