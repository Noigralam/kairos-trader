"""
Backtest RSI threshold + re-entry cooldown sweep for SOLEUR.
Precomputes RSI/EMA once for speed, then sweeps parameter combos.

Usage:
    python backtest_rsi_cooldown.py [days]   # default: 365
"""
import sys
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from bot import config
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


def precompute(df: pd.DataFrame, rsi_period: int = 7, ema_period: int = 200):
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
    entry_time:  datetime
    exit_time:   datetime
    entry_price: float
    exit_price:  float
    pnl:         float
    fees:        float
    exit_reason: str


def run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr,
        start_balance: float,
        rsi_buy: int, rsi_sell: int, cooldown_candles: int) -> tuple[list[Trade], float]:

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
    cooldown_remaining   = 0
    timestamps = None  # lazy

    import numpy as np

    for i in range(WARMUP, len(close_arr)):
        price = float(close_arr[i])
        rsi   = float(rsi_arr[i])
        ema   = float(ema_arr[i])
        hi    = float(hi_arr[i])
        lo    = float(lo_arr[i])

        if cooldown_remaining > 0:
            cooldown_remaining -= 1

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
                trades.append(Trade(position._entry_time, i, position.entry_price,
                                    exit_price, pnl, buy_fee + sell_fee, "take_profit"))
                position = None
                cooldown_remaining = cooldown_candles
                continue

            if position.peak() > stop_level and lo <= stop_level:
                exit_price = stop_level
                buy_fee    = position.value_eur * fee_rate
                sell_fee   = position.amount * exit_price * fee_rate
                pnl        = calc_pnl(position, exit_price, buy_fee, sell_fee)
                balance   += position.amount * exit_price - sell_fee
                trades.append(Trade(position._entry_time, i, position.entry_price,
                                    exit_price, pnl, buy_fee + sell_fee, "trailing_stop"))
                position = None
                cooldown_remaining = cooldown_candles
                continue

        above_ema = price > ema

        # Entry
        if position is None and cooldown_remaining == 0 and rsi < rsi_buy and above_ema:
            max_size = balance / (1 + fee_rate)
            size     = min(balance * pos_pct, max_size)
            if size >= 1:
                buy_fee  = size * fee_rate
                position = Position(PAIR, price, size / price, size,
                                    price * (1 + tp_pct), price)
                position._entry_time = i
                balance -= size + buy_fee

        # DCA
        if position is not None and not position.dca_done:
            drop = (position.entry_price - price) / position.entry_price
            if drop >= dca_drop and rsi < dca_rsi:
                max_size  = balance / (1 + fee_rate)
                dca_value = min(balance * dca_pct, max_size)
                if dca_value >= 1:
                    buy_fee = dca_value * fee_rate
                    apply_dca(position, price, dca_value)
                    balance -= dca_value + buy_fee

        # Exit
        if position is not None and rsi > rsi_sell:
            if price >= position.entry_price * (1 + min_exit):
                buy_fee  = position.value_eur * fee_rate
                sell_fee = position.amount * price * fee_rate
                pnl      = calc_pnl(position, price, buy_fee, sell_fee)
                balance += position.amount * price - sell_fee
                trades.append(Trade(position._entry_time, i, position.entry_price,
                                    price, pnl, buy_fee + sell_fee, "signal"))
                position = None
                cooldown_remaining = cooldown_candles

    if position is not None:
        price    = float(close_arr[-1])
        buy_fee  = position.value_eur * fee_rate
        sell_fee = position.amount * price * fee_rate
        pnl      = calc_pnl(position, price, buy_fee, sell_fee)
        balance += position.amount * price - sell_fee
        trades.append(Trade(position._entry_time, len(close_arr) - 1, position.entry_price,
                            price, pnl, buy_fee + sell_fee, "end_of_data"))

    return trades, balance


def report_line(label: str, trades: list[Trade], end: float):
    if not trades:
        print(f"  {label:<36}  no trades")
        return
    pnl   = sum(t.pnl  for t in trades)
    fees  = sum(t.fees for t in trades)
    wins  = sum(1 for t in trades if t.pnl > 0)
    tax   = min(max(pnl, 0), 30000) * 0.30 + max(max(pnl, 0) - 30000, 0) * 0.34
    after = end - tax if pnl > 0 else end
    worst = min(t.pnl for t in trades)

    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    stop_c = reasons.get("trailing_stop", 0)
    sig_c  = reasons.get("signal", 0)
    tp_c   = reasons.get("take_profit", 0)

    print(f"  {label:<36}  n={len(trades):>3}  W/L={wins}/{len(trades)-wins}"
          f"  PnL={pnl:>+7.2f}  bal={after:>7.2f}"
          f"  worst={worst:>+6.2f}  TP×{tp_c} stop×{stop_c} sig×{sig_c}")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 365
    print(f"\nRSI threshold + cooldown sweep — {PAIR} — {days}d")
    print(f"pos={config.POSITION_SIZE_PCT*100:.0f}%  dca={config.DCA_SIZE_PCT*100:.0f}%"
          f"  floor={config.PROFIT_FLOOR_PCT*100:.0f}%  trail={config.TRAILING_STOP_PCT*100:.0f}%\n")

    df = fetch(days)
    rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = precompute(df)
    start = config.SIMULATION_BALANCE

    rsi_buys  = [25, 28, 30, 33]
    rsi_sells = [60, 65, 70]
    cooldowns = [0, 4, 8, 16]   # candles: 0=none, 4=1h, 8=2h, 16=4h

    results = []
    for rb in rsi_buys:
        for rs in rsi_sells:
            for cd in cooldowns:
                trades, end = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr,
                                  start, rb, rs, cd)
                pnl = sum(t.pnl for t in trades) if trades else 0
                tax = min(max(pnl, 0), 30000) * 0.30 + max(max(pnl, 0) - 30000, 0) * 0.34
                after = end - tax if pnl > 0 else end
                results.append((rb, rs, cd, trades, end, after))

    # Sort by after-tax balance descending
    results.sort(key=lambda x: x[5], reverse=True)

    print(f"  {'Scenario':<36}  {'n':>4}  {'W/L':<7}  {'PnL':>8}  {'bal':>8}  {'worst':>7}  exits")
    print(f"  {'─'*36}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*8}  {'─'*7}  {'─'*20}")

    current = ("buy<30", "sell>65", "cool=0")
    for rb, rs, cd, trades, end, after in results:
        cd_str = f"{cd*15}m" if cd > 0 else "none"
        marker = " ◄ current" if rb == 30 and rs == 65 and cd == 0 else ""
        label  = f"buy<{rb}  sell>{rs}  cool={cd_str}{marker}"
        report_line(label, trades, end)
