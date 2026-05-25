"""
Backtest profit floor % sweep for SOLEUR.

Usage:
    python backtest_floor.py [days]   # default: 365
"""
import sys
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from bot import config
from bot.strategy import compute_signal, Signal
from bot.candles import get_df, initial_sync
from bot.risk import Position, apply_dca, update_peak, calc_pnl

INTERVAL = "15m"
WARMUP   = 210
PAIR     = "SOLEUR"


def fetch(days: int) -> pd.DataFrame:
    needed = days * 24 * 4 + WARMUP
    df = get_df(PAIR, INTERVAL, limit=needed)
    if len(df) >= needed * 0.9:
        print(f"  {PAIR}: {len(df):,} candles from local DB")
        return df
    print(f"  Syncing {days}d from Binance…", flush=True)
    initial_sync(PAIR, INTERVAL, days=days)
    df = get_df(PAIR, INTERVAL, limit=needed)
    print(f"  {PAIR}: {len(df):,} candles ready")
    return df


@dataclass
class Trade:
    entry_time:  datetime
    exit_time:   datetime
    entry_price: float
    exit_price:  float
    pnl:         float
    fees:        float
    exit_reason: str


def run(df: pd.DataFrame, start_balance: float, profit_floor: float) -> tuple[list[Trade], float]:
    fee_rate  = config.BINANCE_FEE
    pos_pct   = config.POSITION_SIZE_PCT
    dca_drop  = config.DCA_DROP_PCT
    dca_pct   = config.DCA_SIZE_PCT
    tp_pct    = config.TAKE_PROFIT_PCT
    trail_pct = config.TRAILING_STOP_PCT
    min_exit  = config.MIN_EXIT_PROFIT_PCT
    dca_rsi   = config.DCA_RSI_THRESHOLD

    balance  = start_balance
    position: Position | None = None
    trades: list[Trade] = []

    for i in range(WARMUP, len(df)):
        window = df.iloc[: i + 1]
        row    = df.iloc[i]
        price  = float(row["close"])
        ts     = row["open_time"].to_pydatetime()

        if position is not None:
            hi = float(row["high"])
            lo = float(row["low"])
            update_peak(position, hi)

            stop_level = max(
                position.entry_price * (1 + fee_rate + profit_floor) / (1 - fee_rate),
                position.peak() * (1 - trail_pct),
            )

            if hi >= position.take_profit_price:
                exit_price = position.take_profit_price
                buy_fee    = position.value_eur * fee_rate
                sell_fee   = position.amount * exit_price * fee_rate
                pnl        = calc_pnl(position, exit_price, buy_fee, sell_fee)
                balance   += position.amount * exit_price - sell_fee
                trades.append(Trade(position._entry_time, ts, position.entry_price,
                                    exit_price, pnl, buy_fee + sell_fee, "take_profit"))
                position = None
                continue

            if position.peak() > stop_level and lo <= stop_level:
                exit_price = stop_level
                buy_fee    = position.value_eur * fee_rate
                sell_fee   = position.amount * exit_price * fee_rate
                pnl        = calc_pnl(position, exit_price, buy_fee, sell_fee)
                balance   += position.amount * exit_price - sell_fee
                trades.append(Trade(position._entry_time, ts, position.entry_price,
                                    exit_price, pnl, buy_fee + sell_fee, "trailing_stop"))
                position = None
                continue

        result = compute_signal(window)

        if result.signal == Signal.BUY and position is None:
            max_size = balance / (1 + fee_rate)
            size     = min(balance * pos_pct, max_size)
            if size < 1:
                continue
            buy_fee  = size * fee_rate
            position = Position(PAIR, price, size / price, size,
                                price * (1 + tp_pct), price)
            position._entry_time = ts
            balance -= size + buy_fee

        if position is not None and not position.dca_done:
            drop = (position.entry_price - price) / position.entry_price
            if drop >= dca_drop and result.rsi < dca_rsi:
                max_size  = balance / (1 + fee_rate)
                dca_value = min(balance * dca_pct, max_size)
                if dca_value >= 1:
                    buy_fee = dca_value * fee_rate
                    apply_dca(position, price, dca_value)
                    balance -= dca_value + buy_fee

        if result.signal == Signal.SELL and position is not None:
            if price >= position.entry_price * (1 + min_exit):
                buy_fee  = position.value_eur * fee_rate
                sell_fee = position.amount * price * fee_rate
                pnl      = calc_pnl(position, price, buy_fee, sell_fee)
                balance += position.amount * price - sell_fee
                trades.append(Trade(position._entry_time, ts, position.entry_price,
                                    price, pnl, buy_fee + sell_fee, "signal"))
                position = None

    if position is not None:
        price    = float(df.iloc[-1]["close"])
        buy_fee  = position.value_eur * fee_rate
        sell_fee = position.amount * price * fee_rate
        pnl      = calc_pnl(position, price, buy_fee, sell_fee)
        balance += position.amount * price - sell_fee
        trades.append(Trade(position._entry_time, df.iloc[-1]["open_time"].to_pydatetime(),
                            position.entry_price, price, pnl,
                            buy_fee + sell_fee, "end_of_data"))

    return trades, balance


def report_line(label: str, trades: list[Trade], start: float, end: float):
    if not trades:
        print(f"  {label:<22}  no trades")
        return
    pnl   = sum(t.pnl  for t in trades)
    fees  = sum(t.fees for t in trades)
    wins  = sum(1 for t in trades if t.pnl > 0)
    tax   = min(max(pnl, 0), 30000) * 0.30 + max(max(pnl, 0) - 30000, 0) * 0.34
    after = end - tax if pnl > 0 else end
    worst = min(t.pnl for t in trades)
    best  = max(t.pnl for t in trades)

    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    tp_count   = reasons.get("take_profit", 0)
    stop_count = reasons.get("trailing_stop", 0)
    sig_count  = reasons.get("signal", 0)

    avg_pnl = pnl / len(trades)

    print(f"  {label:<22}  n={len(trades):>3}  W/L={wins}/{len(trades)-wins}"
          f"  PnL={pnl:>+7.2f}  after-tax={after:>7.2f}"
          f"  avg={avg_pnl:>+5.2f}  worst={worst:>+6.2f}  best={best:>+6.2f}"
          f"  TP×{tp_count} stop×{stop_count} sig×{sig_count}")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 365
    print(f"\nProfit floor sweep — {PAIR} — {days}d")
    print(f"pos={config.POSITION_SIZE_PCT*100:.0f}%  dca={config.DCA_SIZE_PCT*100:.0f}%"
          f"  drop={config.DCA_DROP_PCT*100:.0f}%  trail={config.TRAILING_STOP_PCT*100:.0f}%"
          f"  TP={config.TAKE_PROFIT_PCT*100:.0f}%\n")

    df = fetch(days)
    start = config.SIMULATION_BALANCE

    floors = [0.0, 0.002, 0.005, 0.008, 0.010, 0.015, 0.020, 0.030, 0.050]

    print(f"  {'Scenario':<22}  {'n':>4}  {'W/L':<7}  {'PnL':>8}  {'after-tax':>10}"
          f"  {'avg':>6}  {'worst':>7}  {'best':>7}  exits")
    print(f"  {'─'*22}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*10}"
          f"  {'─'*6}  {'─'*7}  {'─'*7}  {'─'*20}")

    for floor in floors:
        label = f"floor={floor*100:.1f}%"
        trades, end = run(df, start, floor)
        report_line(label, trades, start, end)
