"""
Sweep DCA parameters for SOLEUR on 15m with current settings
(sell>75, TP=5%, trail=5%, floor=3%, RSI14 buy<30).

Sweeps: drop%, RSI threshold, and size% independently, plus a no-DCA baseline.

Usage:
    python backtest_dca2.py [days]   # default: 365 180
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
        print(f"  {pair}/{INTERVAL}: {len(df):,} candles from local DB")
        return df
    print(f"  Syncing {pair}/{INTERVAL} {days}d from Binance…", flush=True)
    initial_sync(pair, INTERVAL, days=days)
    df = get_df(pair, INTERVAL, limit=needed)
    print(f"  {pair}/{INTERVAL}: {len(df):,} candles ready")
    return df


def precompute(df: pd.DataFrame):
    close = df["close"]
    ema   = close.ewm(span=200, adjust=False).mean()
    rsi_p = config.rsi_period_for("SOLEUR")
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=rsi_p - 1, min_periods=rsi_p).mean()
    avg_l = loss.ewm(com=rsi_p - 1, min_periods=rsi_p).mean()
    rsi   = 100 - (100 / (1 + avg_g / avg_l))
    return rsi.values, ema.values, close.values, df["high"].values, df["low"].values


@dataclass
class Trade:
    pnl:         float
    fees:        float
    exit_reason: str
    dca_used:    bool


def run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr,
        start_balance: float,
        dca_drop: float, dca_rsi: int, dca_pct: float,
        enable_dca: bool = True) -> tuple[list[Trade], float]:

    fee_rate  = config.BINANCE_FEE
    pos_pct   = config.POSITION_SIZE_PCT
    tp_pct    = config.TAKE_PROFIT_PCT
    trail_pct = config.TRAILING_STOP_PCT
    floor_pct = config.PROFIT_FLOOR_PCT
    min_exit  = config.MIN_EXIT_PROFIT_PCT
    rsi_buy   = config.RSI_OVERSOLD
    rsi_sell  = config.RSI_OVERBOUGHT
    pair      = "SOLEUR"

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
            take_profit_price = position.entry_price * (1 + tp_pct)

            if hi >= take_profit_price:
                ep = take_profit_price
                bf = position.value_eur * fee_rate
                sf = position.amount * ep * fee_rate
                balance += position.amount * ep - sf
                trades.append(Trade(calc_pnl(position, ep, bf, sf), bf + sf, "take_profit", position.dca_done))
                position = None
                continue

            if position.peak() > stop_level and lo <= stop_level:
                ep = stop_level
                bf = position.value_eur * fee_rate
                sf = position.amount * ep * fee_rate
                balance += position.amount * ep - sf
                trades.append(Trade(calc_pnl(position, ep, bf, sf), bf + sf, "trailing_stop", position.dca_done))
                position = None
                continue

        if position is None and rsi < rsi_buy and price > ema:
            max_size = balance / (1 + fee_rate)
            size = min(balance * pos_pct, max_size)
            if size >= 1:
                position = Position(pair, price, size / price, size,
                                    price * (1 + tp_pct), price)
                balance -= size + size * fee_rate

        if enable_dca and position is not None and not position.dca_done:
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
                trades.append(Trade(calc_pnl(position, price, bf, sf), bf + sf, "signal", position.dca_done))
                position = None

    if position is not None:
        price = float(close_arr[-1])
        bf = position.value_eur * fee_rate
        sf = position.amount * price * fee_rate
        balance += position.amount * price - sf
        trades.append(Trade(calc_pnl(position, price, bf, sf), bf + sf, "end_of_data", position.dca_done))

    return trades, balance


def report(label: str, trades: list[Trade]):
    realized = [t for t in trades if t.exit_reason != "end_of_data"]
    open_eod  = len(trades) - len(realized)
    if not realized:
        print(f"  {label:<42}  no closed trades")
        return
    pnl    = sum(t.pnl  for t in realized)
    fees   = sum(t.fees for t in realized)
    wins   = sum(1 for t in realized if t.pnl > 0)
    losses = sum(1 for t in realized if t.pnl <= 0)
    n      = len(realized)
    worst  = min(t.pnl for t in realized)
    best   = max(t.pnl for t in realized)
    avg    = pnl / n
    eod    = f"+{open_eod}" if open_eod else "  "
    dca_n  = sum(1 for t in realized if t.dca_used)
    reasons = {}
    for t in realized:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    tp_c   = reasons.get("take_profit", 0)
    stop_c = reasons.get("trailing_stop", 0)
    sig_c  = reasons.get("signal", 0)
    print(f"  {label:<42}  n={n:>3}{eod}  W/L={wins}/{losses}"
          f"  PnL={pnl:>+7.2f}  avg={avg:>+5.2f}  worst={worst:>+6.2f}  best={best:>+5.2f}"
          f"  fees={fees:.2f}  dca×{dca_n}  [TP×{tp_c} stop×{stop_c} sig×{sig_c}]")


if __name__ == "__main__":
    args      = [a for a in sys.argv[1:] if a.isdigit()]
    days_list = [int(a) for a in args] or [365, 180]
    pair      = "SOLEUR"
    start     = config.SIMULATION_BALANCE

    # current values
    cur_drop = config.DCA_DROP_PCT        # 0.02
    cur_rsi  = config.DCA_RSI_THRESHOLD   # 38
    cur_pct  = config.DCA_SIZE_PCT        # 0.75

    print(f"\nDCA parameter sweep — SOLEUR 15m RSI({config.rsi_period_for('SOLEUR')})")
    print(f"floor={config.PROFIT_FLOOR_PCT*100:.0f}%  trail={config.TRAILING_STOP_PCT*100:.0f}%"
          f"  sell>{config.RSI_OVERBOUGHT}  buy<{config.RSI_OVERSOLD}  pos={config.POSITION_SIZE_PCT*100:.0f}%\n")

    for days in days_list:
        print(f"{'━'*115}")
        print(f"  {days}d window")
        print(f"{'━'*115}")

        df = fetch(pair, days)
        rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = precompute(df)

        def hdr():
            print(f"  {'Scenario':<42}  {'n':>4}       {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
                  f"  {'worst':>7}  {'best':>6}  fees   dca  exits")
            print(f"  {'─'*42}  {'─'*4}  {'─'*5}  {'─'*7}  {'─'*8}  {'─'*6}"
                  f"  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*5}  {'─'*20}")

        # --- No DCA baseline ---
        print(f"\n  No DCA baseline")
        hdr()
        t, _ = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr, start,
                   cur_drop, cur_rsi, cur_pct, enable_dca=False)
        report("no DCA", t)

        # --- Drop % sweep ---
        print(f"\n  Drop % sweep  (RSI<{cur_rsi}  size={cur_pct*100:.0f}%)")
        hdr()
        for drop, lbl in [(0.01,"drop=1%"), (0.02,"drop=2%  ◄ current"),
                          (0.03,"drop=3%"), (0.04,"drop=4%"), (0.05,"drop=5%")]:
            t, _ = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr, start,
                       drop, cur_rsi, cur_pct)
            report(lbl, t)

        # --- RSI threshold sweep ---
        print(f"\n  RSI threshold sweep  (drop={cur_drop*100:.0f}%  size={cur_pct*100:.0f}%)")
        hdr()
        for rsi, lbl in [(30,"dca_rsi<30"), (33,"dca_rsi<33"), (35,"dca_rsi<35"),
                         (38,"dca_rsi<38  ◄ current"), (42,"dca_rsi<42"), (45,"dca_rsi<45")]:
            t, _ = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr, start,
                       cur_drop, rsi, cur_pct)
            report(lbl, t)

        # --- Size % sweep ---
        print(f"\n  Size % sweep  (drop={cur_drop*100:.0f}%  RSI<{cur_rsi})")
        hdr()
        for pct, lbl in [(0.25,"dca_size=25%"), (0.50,"dca_size=50%"),
                         (0.75,"dca_size=75%  ◄ current"), (1.00,"dca_size=100%")]:
            t, _ = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr, start,
                       cur_drop, cur_rsi, pct)
            report(lbl, t)

        print()
