"""
Backtest DCA RSI threshold comparison for SOLEUR.

Tests free DCA (decoupled from EMA200) vs old DCA (signal-gated)
across different RSI thresholds.

Usage:
    python backtest_dca_rsi.py [days]   # default: 365
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
    print(f"  {PAIR}: syncing {days}d from Binance…", flush=True)
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


def run(df: pd.DataFrame, start_balance: float, dca_rsi_threshold: float | None) -> tuple[list[Trade], float]:
    """
    dca_rsi_threshold=None  → no DCA at all
    dca_rsi_threshold=-1    → old behaviour (DCA only when signal==BUY, i.e. EMA200 + RSI<30)
    dca_rsi_threshold=N     → free DCA when drop>=DCA_DROP_PCT and RSI<N
    """
    fee_rate  = config.BINANCE_FEE
    pos_pct   = config.POSITION_SIZE_PCT
    dca_drop  = config.DCA_DROP_PCT
    dca_pct   = config.DCA_SIZE_PCT
    tp_pct    = config.TAKE_PROFIT_PCT
    trail_pct = config.TRAILING_STOP_PCT
    min_exit  = config.MIN_EXIT_PROFIT_PCT

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
                position.entry_price * (1 + fee_rate + 0.01) / (1 - fee_rate),
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

        # Entry
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

        # DCA
        if position is not None and not position.dca_done:
            drop = (position.entry_price - price) / position.entry_price
            if drop >= dca_drop:
                fire = False
                if dca_rsi_threshold == -1:
                    fire = result.signal == Signal.BUY  # old: EMA200 + RSI<30
                elif dca_rsi_threshold is not None:
                    fire = result.rsi < dca_rsi_threshold  # free: RSI floor only
                if fire:
                    max_size  = balance / (1 + fee_rate)
                    dca_value = min(balance * dca_pct, max_size)
                    if dca_value >= 1:
                        buy_fee = dca_value * fee_rate
                        apply_dca(position, price, dca_value)
                        balance -= dca_value + buy_fee

        # Exit
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


def report(label: str, trades: list[Trade], start: float, end: float):
    if not trades:
        print(f"  {label:<28}  no trades")
        return
    pnl   = sum(t.pnl  for t in trades)
    fees  = sum(t.fees for t in trades)
    wins  = sum(1 for t in trades if t.pnl > 0)
    dca_count = sum(1 for t in trades if t.entry_price != t.entry_price)  # placeholder
    tax   = min(max(pnl, 0), 30000) * 0.30 + max(max(pnl, 0) - 30000, 0) * 0.34
    after = end - tax if pnl > 0 else end

    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    reason_str = "  ".join(f"{k}×{v}" for k, v in reasons.items())

    print(f"  {label:<28}  trades={len(trades):>3}  W/L={wins}/{len(trades)-wins}"
          f"  PnL={pnl:>+7.2f}  after-tax={after:>7.2f}  fees={fees:.3f}"
          f"  bal={end:>7.2f}  [{reason_str}]")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 365
    print(f"\nDCA RSI threshold backtest — {PAIR} — {days}d — interval={INTERVAL}")
    print(f"Settings: pos={config.POSITION_SIZE_PCT*100:.0f}%  dca={config.DCA_SIZE_PCT*100:.0f}%"
          f"  drop={config.DCA_DROP_PCT*100:.0f}%  TP={config.TAKE_PROFIT_PCT*100:.0f}%"
          f"  trail={config.TRAILING_STOP_PCT*100:.0f}%\n")

    df = fetch(days)
    start = config.SIMULATION_BALANCE

    scenarios = [
        ("No DCA",              None),
        ("Old DCA (EMA+RSI<30)", -1),
        ("Free DCA RSI<30",     30),
        ("Free DCA RSI<35",     35),
        ("Free DCA RSI<38",     38),
        ("Free DCA RSI<42",     42),
        ("Free DCA RSI<45",     45),
    ]

    print(f"  {'Scenario':<28}  {'trades':>6}  {'W/L':<7}  {'PnL':>8}  {'after-tax':>10}  {'fees':>7}  {'balance':>8}  exits")
    print(f"  {'─'*28}  {'─'*6}  {'─'*7}  {'─'*8}  {'─'*10}  {'─'*7}  {'─'*8}  {'─'*30}")

    for label, threshold in scenarios:
        trades, end = run(df, start, threshold)
        report(label, trades, start, end)
