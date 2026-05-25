"""
Compare 5m vs 15m candle interval for SOLEUR.
Tests RSI(14) buy<30/sell>65 on both intervals, plus RSI(7) on 5m as a tuned variant.

Usage:
    python backtest_interval.py [days]   # default: 365 180
"""
import sys
from dataclasses import dataclass

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from bot import config
from bot.candles import get_df, initial_sync
from bot.risk import Position, apply_dca, update_peak, calc_pnl

WARMUP = 210   # candles needed for EMA200 to stabilise


def fetch(pair: str, interval: str, days: int) -> pd.DataFrame:
    candles_per_day = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48, "1h": 24}[interval]
    needed = days * candles_per_day + WARMUP
    df = get_df(pair, interval, limit=needed)
    if len(df) >= needed * 0.9:
        print(f"  {pair}/{interval}: {len(df):,} candles from local DB")
        return df
    print(f"  Syncing {pair}/{interval} {days}d from Binance…", flush=True)
    initial_sync(pair, interval, days=days)
    df = get_df(pair, interval, limit=needed)
    print(f"  {pair}/{interval}: {len(df):,} candles ready")
    return df


def precompute(df: pd.DataFrame, rsi_period: int):
    close = df["close"]
    ema   = close.ewm(span=200, adjust=False).mean()
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    avg_l = loss.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    rsi   = 100 - (100 / (1 + avg_g / avg_l))
    return rsi.values, ema.values, close.values, df["high"].values, df["low"].values


@dataclass
class Trade:
    pnl:         float
    fees:        float
    exit_reason: str


def run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr,
        start_balance: float, rsi_buy: int, rsi_sell: int,
        pair: str = "SOLEUR") -> tuple[list[Trade], float]:

    fee_rate  = config.BINANCE_FEE
    pos_pct   = config.POSITION_SIZE_PCT
    dca_drop  = config.DCA_DROP_PCT
    dca_pct   = config.DCA_SIZE_PCT
    tp_pct    = config.TAKE_PROFIT_PCT
    trail_pct = config.TRAILING_STOP_PCT
    floor_pct = config.PROFIT_FLOOR_PCT
    min_exit  = config.MIN_EXIT_PROFIT_PCT
    dca_rsi   = config.DCA_RSI_THRESHOLD

    balance   = start_balance
    position: Position | None = None
    trades: list[Trade] = []

    for i in range(WARMUP, len(close_arr)):
        price = float(close_arr[i])
        rsi   = float(rsi_arr[i])
        ema   = float(ema_arr[i])
        hi    = float(hi_arr[i])
        lo    = float(lo_arr[i])

        if position is not None:
            update_peak(position, hi)
            stop_level = max(
                position.entry_price * (1 + fee_rate + floor_pct) / (1 - fee_rate),
                position.peak() * (1 - trail_pct),
            )

            if hi >= position.take_profit_price:
                ep = position.take_profit_price
                bf = position.value_eur * fee_rate
                sf = position.amount * ep * fee_rate
                balance += position.amount * ep - sf
                trades.append(Trade(calc_pnl(position, ep, bf, sf), bf + sf, "take_profit"))
                position = None
                continue

            if position.peak() > stop_level and lo <= stop_level:
                ep = stop_level
                bf = position.value_eur * fee_rate
                sf = position.amount * ep * fee_rate
                balance += position.amount * ep - sf
                trades.append(Trade(calc_pnl(position, ep, bf, sf), bf + sf, "trailing_stop"))
                position = None
                continue

        if position is None and rsi < rsi_buy and price > ema:
            max_size = balance / (1 + fee_rate)
            size = min(balance * pos_pct, max_size)
            if size >= 1:
                position = Position(pair, price, size / price, size,
                                    price * (1 + tp_pct), price)
                balance -= size + size * fee_rate

        if position is not None and not position.dca_done:
            drop = (position.entry_price - price) / position.entry_price
            if drop >= dca_drop and rsi < dca_rsi:
                max_size  = balance / (1 + fee_rate)
                dca_value = min(balance * dca_pct, max_size)
                if dca_value >= 1:
                    apply_dca(position, price, dca_value)
                    balance -= dca_value + dca_value * fee_rate

        if position is not None and rsi > rsi_sell:
            if price >= position.entry_price * (1 + min_exit):
                bf = position.value_eur * fee_rate
                sf = position.amount * price * fee_rate
                balance += position.amount * price - sf
                trades.append(Trade(calc_pnl(position, price, bf, sf), bf + sf, "signal"))
                position = None

    if position is not None:
        price = float(close_arr[-1])
        bf = position.value_eur * fee_rate
        sf = position.amount * price * fee_rate
        balance += position.amount * price - sf
        trades.append(Trade(calc_pnl(position, price, bf, sf), bf + sf, "end_of_data"))

    return trades, balance


def report(label: str, trades: list[Trade], start: float):
    realized = [t for t in trades if t.exit_reason != "end_of_data"]
    open_eod  = len(trades) - len(realized)

    if not realized:
        print(f"  {label:<42}  no closed trades")
        return

    pnl   = sum(t.pnl  for t in realized)
    fees  = sum(t.fees for t in realized)
    wins  = sum(1 for t in realized if t.pnl > 0)
    n     = len(realized)
    worst = min(t.pnl for t in realized)
    best  = max(t.pnl for t in realized)
    avg   = pnl / n if n else 0
    eod_str = f"+{open_eod}" if open_eod else "  "

    reasons = {}
    for t in realized:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    tp_c   = reasons.get("take_profit", 0)
    stop_c = reasons.get("trailing_stop", 0)
    sig_c  = reasons.get("signal", 0)

    print(f"  {label:<42}  n={n:>3}{eod_str}  W/L={wins}/{n-wins}"
          f"  PnL={pnl:>+7.2f}  avg={avg:>+5.2f}  worst={worst:>+6.2f}  best={best:>+5.2f}"
          f"  fees={fees:.2f}  [TP×{tp_c} stop×{stop_c} sig×{sig_c}]")


if __name__ == "__main__":
    args      = [a for a in sys.argv[1:] if a.isdigit()]
    days_list = [int(a) for a in args] or [365, 180]
    pair      = "SOLEUR"
    start     = config.SIMULATION_BALANCE

    scenarios = [
        ("15m", 14, 30, 65, "15m  RSI(14) buy<30 sell>65  ◄ current"),
        ("3m",  14, 30, 65, "3m   RSI(14) buy<30 sell>65"),
        ("3m",  7,  30, 65, "3m   RSI(7)  buy<30 sell>65"),
        ("3m",  7,  25, 70, "3m   RSI(7)  buy<25 sell>70  (tighter)"),
        ("3m",  14, 25, 70, "3m   RSI(14) buy<25 sell>70  (tighter)"),
    ]

    print(f"\n3m vs 15m candle interval — {pair}")
    print(f"floor={config.PROFIT_FLOOR_PCT*100:.0f}%  trail={config.TRAILING_STOP_PCT*100:.0f}%"
          f"  pos={config.POSITION_SIZE_PCT*100:.0f}%  dca={config.DCA_SIZE_PCT*100:.0f}%\n")

    for days in days_list:
        print(f"{'━'*110}")
        print(f"  {days}d window")
        print(f"{'━'*110}")

        # pre-fetch all needed dataframes (one per unique interval)
        dfs: dict[str, pd.DataFrame] = {}
        for interval, *_ in scenarios:
            if interval not in dfs:
                dfs[interval] = fetch(pair, interval, days)
        print()

        print(f"  {'Scenario':<42}  {'n':>4}       {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
              f"  {'worst':>7}  {'best':>6}  fees   exits")
        print(f"  {'─'*42}  {'─'*4}  {'─'*5}  {'─'*7}  {'─'*8}  {'─'*6}"
              f"  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*22}")

        for interval, rsi_p, rsi_buy, rsi_sell, label in scenarios:
            df = dfs[interval]
            rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = precompute(df, rsi_p)
            trades, end = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr,
                              start, rsi_buy, rsi_sell, pair)
            report(label, trades, start)
        print()
