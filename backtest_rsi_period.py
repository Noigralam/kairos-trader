"""
Backtest RSI period (7 vs 14) sweep for SOLEUR.
Uses current optimized settings: buy<33, sell>70, floor=3%.

Usage:
    python backtest_rsi_period.py [days]   # default: 365
"""
import sys
from dataclasses import dataclass

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from bot import config
from bot.candles import get_df, initial_sync
from bot.risk import Position, apply_dca, update_peak, calc_pnl

INTERVAL = "15m"
WARMUP   = 210


def fetch(pair: str, days: int) -> pd.DataFrame:
    needed = days * 24 * 4 + WARMUP
    df = get_df(pair, INTERVAL, limit=needed)
    if len(df) >= needed * 0.9:
        print(f"  {pair}: {len(df):,} candles from local DB")
        return df
    print(f"  Syncing {days}d from Binance…", flush=True)
    initial_sync(pair, INTERVAL, days=days)
    df = get_df(pair, INTERVAL, limit=needed)
    print(f"  {pair}: {len(df):,} candles ready")
    return df


def precompute(df: pd.DataFrame, rsi_period: int, ema_period: int = 200):
    close = df["close"]
    ema   = close.ewm(span=ema_period, adjust=False).mean()
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    avg_l = loss.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    rsi   = 100 - (100 / (1 + avg_g / avg_l))
    return rsi.values, ema.values, close.values, df["high"].values, df["low"].values


@dataclass
class Trade:
    entry_price: float
    exit_price:  float
    pnl:         float
    fees:        float
    exit_reason: str


def run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr,
        start_balance: float, rsi_buy: int, rsi_sell: int, pair: str = "SOLEUR") -> tuple[list[Trade], float]:

    fee_rate  = config.BINANCE_FEE
    pos_pct   = config.POSITION_SIZE_PCT
    dca_drop  = config.DCA_DROP_PCT
    dca_pct   = config.DCA_SIZE_PCT
    tp_pct    = config.TAKE_PROFIT_PCT
    trail_pct = config.TRAILING_STOP_PCT
    floor_pct = config.PROFIT_FLOOR_PCT
    min_exit  = config.MIN_EXIT_PROFIT_PCT
    dca_rsi   = config.DCA_RSI_THRESHOLD

    balance  = start_balance
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
                exit_price = position.take_profit_price
                buy_fee    = position.value_eur * fee_rate
                sell_fee   = position.amount * exit_price * fee_rate
                pnl        = calc_pnl(position, exit_price, buy_fee, sell_fee)
                balance   += position.amount * exit_price - sell_fee
                trades.append(Trade(position.entry_price, exit_price, pnl,
                                    buy_fee + sell_fee, "take_profit"))
                position = None
                continue

            if position.peak() > stop_level and lo <= stop_level:
                exit_price = stop_level
                buy_fee    = position.value_eur * fee_rate
                sell_fee   = position.amount * exit_price * fee_rate
                pnl        = calc_pnl(position, exit_price, buy_fee, sell_fee)
                balance   += position.amount * exit_price - sell_fee
                trades.append(Trade(position.entry_price, exit_price, pnl,
                                    buy_fee + sell_fee, "trailing_stop"))
                position = None
                continue

        above_ema = price > ema

        if position is None and rsi < rsi_buy and above_ema:
            max_size = balance / (1 + fee_rate)
            size     = min(balance * pos_pct, max_size)
            if size >= 1:
                buy_fee  = size * fee_rate
                position = Position(pair, price, size / price, size,
                                    price * (1 + tp_pct), price)
                position._entry_time = i
                balance -= size + buy_fee

        if position is not None and not position.dca_done:
            drop = (position.entry_price - price) / position.entry_price
            if drop >= dca_drop and rsi < dca_rsi:
                max_size  = balance / (1 + fee_rate)
                dca_value = min(balance * dca_pct, max_size)
                if dca_value >= 1:
                    buy_fee = dca_value * fee_rate
                    apply_dca(position, price, dca_value)
                    balance -= dca_value + buy_fee

        if position is not None and rsi > rsi_sell:
            if price >= position.entry_price * (1 + min_exit):
                buy_fee  = position.value_eur * fee_rate
                sell_fee = position.amount * price * fee_rate
                pnl      = calc_pnl(position, price, buy_fee, sell_fee)
                balance += position.amount * price - sell_fee
                trades.append(Trade(position.entry_price, price, pnl,
                                    buy_fee + sell_fee, "signal"))
                position = None

    if position is not None:
        price    = float(close_arr[-1])
        buy_fee  = position.value_eur * fee_rate
        sell_fee = position.amount * price * fee_rate
        pnl      = calc_pnl(position, price, buy_fee, sell_fee)
        balance += position.amount * price - sell_fee
        trades.append(Trade(position.entry_price, price, pnl,
                            buy_fee + sell_fee, "end_of_data"))

    return trades, balance


def report(label: str, trades: list[Trade], start: float, end: float):
    if not trades:
        print(f"  {label:<30}  no trades")
        return
    pnl   = sum(t.pnl  for t in trades)
    fees  = sum(t.fees for t in trades)
    wins  = sum(1 for t in trades if t.pnl > 0)
    tax   = min(max(pnl, 0), 30000) * 0.30 + max(max(pnl, 0) - 30000, 0) * 0.34
    after = end - tax if pnl > 0 else end
    worst = min(t.pnl for t in trades)
    best  = max(t.pnl for t in trades)
    avg   = pnl / len(trades)

    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    stop_c = reasons.get("trailing_stop", 0)
    sig_c  = reasons.get("signal", 0)
    tp_c   = reasons.get("take_profit", 0)

    print(f"  {label:<30}  n={len(trades):>3}  W/L={wins}/{len(trades)-wins}"
          f"  PnL={pnl:>+7.2f}  bal={after:>7.2f}"
          f"  avg={avg:>+5.2f}  worst={worst:>+6.2f}  best={best:>+5.2f}"
          f"  fees={fees:.2f}  TP×{tp_c} stop×{stop_c} sig×{sig_c}")


if __name__ == "__main__":
    args = sys.argv[1:]
    pair = next((a.upper() for a in args if not a.isdigit()), "SOLEUR")
    days_list = [int(a) for a in args if a.isdigit()] or [365, 180]

    # focused comparison: current (RSI14 buy<30/sell<65) vs old (RSI7 buy<33/sell>70)
    scenarios = [
        (14, 30, 65,  "RSI(14) buy<30 sell>65  ◄ current"),
        (7,  33, 70,  "RSI(7)  buy<33 sell>70  (old)"),
        (7,  30, 65,  "RSI(7)  buy<30 sell>65  (rsi7 baseline)"),
    ]

    print(f"\nRSI comparison — {pair}")
    print(f"floor={config.PROFIT_FLOOR_PCT*100:.0f}%  trail={config.TRAILING_STOP_PCT*100:.0f}%"
          f"  pos={config.POSITION_SIZE_PCT*100:.0f}%  dca={config.DCA_SIZE_PCT*100:.0f}%\n")

    for days in days_list:
        print(f"{'━'*100}")
        print(f"  {days}d window")
        print(f"{'━'*100}")
        print(f"  {'Scenario':<38}  {'n':>4}  {'W/L':<7}  {'PnL':>8}  {'bal':>8}"
              f"  {'avg':>6}  {'worst':>7}  {'best':>6}  {'fees':>5}  exits")
        print(f"  {'─'*38}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*8}"
              f"  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*20}")

        df = fetch(pair, days)
        start = config.SIMULATION_BALANCE

        for rsi_period, rsi_buy, rsi_sell, label in scenarios:
            rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = precompute(df, rsi_period)
            trades, end = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr,
                              start, rsi_buy, rsi_sell, pair)
            report(label, trades, start, end)
        print()
