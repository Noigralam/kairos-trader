"""
Full parameter sweep for ETHEUR — mirrors all SOLEUR sweeps.
ETHEUR uses RSI(7), current settings: sell>75, TP=5%, trail=5%, floor=3%, buy<30, DCA drop=1%.

Usage:
    python backtest_sweep_eth.py [days]   # default: 365 180
"""
import sys
from dataclasses import dataclass

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from bot import config
from bot.candles import get_df, initial_sync
from bot.risk import Position, apply_dca, update_peak, calc_pnl

PAIR     = "ETHEUR"
INTERVAL = "15m"
WARMUP   = 210


def fetch(days: int) -> pd.DataFrame:
    needed = days * 24 * 4 + WARMUP
    df = get_df(PAIR, INTERVAL, limit=needed)
    if len(df) >= needed * 0.9:
        print(f"  {PAIR}/{INTERVAL}: {len(df):,} candles from local DB")
        return df
    print(f"  Syncing {PAIR}/{INTERVAL} {days}d from Binance…", flush=True)
    initial_sync(PAIR, INTERVAL, days=days)
    df = get_df(PAIR, INTERVAL, limit=needed)
    print(f"  {PAIR}/{INTERVAL}: {len(df):,} candles ready")
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
    dca_used:    bool = False


def run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr,
        start_balance: float,
        rsi_buy:   int   = None,
        rsi_sell:  int   = None,
        tp_pct:    float = None,
        trail_pct: float = None,
        floor_pct: float = None,
        dca_drop:  float = None,
        dca_rsi:   int   = None,
        dca_pct:   float = None,
        enable_dca: bool = True) -> tuple[list[Trade], float]:

    fee_rate  = config.BINANCE_FEE
    pos_pct   = config.POSITION_SIZE_PCT
    min_exit  = config.MIN_EXIT_PROFIT_PCT

    rsi_buy   = rsi_buy   if rsi_buy   is not None else config.RSI_OVERSOLD
    rsi_sell  = rsi_sell  if rsi_sell  is not None else config.RSI_OVERBOUGHT
    tp_pct    = tp_pct    if tp_pct    is not None else config.TAKE_PROFIT_PCT
    trail_pct = trail_pct if trail_pct is not None else config.TRAILING_STOP_PCT
    floor_pct = floor_pct if floor_pct is not None else config.PROFIT_FLOOR_PCT
    dca_drop  = dca_drop  if dca_drop  is not None else config.DCA_DROP_PCT
    dca_rsi   = dca_rsi   if dca_rsi   is not None else config.DCA_RSI_THRESHOLD
    dca_pct   = dca_pct   if dca_pct   is not None else config.DCA_SIZE_PCT

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
                position = Position(PAIR, price, size / price, size,
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


def report(label: str, trades: list[Trade], show_dca: bool = False):
    realized = [t for t in trades if t.exit_reason != "end_of_data"]
    open_eod  = len(trades) - len(realized)
    if not realized:
        print(f"  {label:<44}  no closed trades")
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
    reasons = {}
    for t in realized:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    tp_c   = reasons.get("take_profit", 0)
    stop_c = reasons.get("trailing_stop", 0)
    sig_c  = reasons.get("signal", 0)
    dca_n  = sum(1 for t in realized if t.dca_used)
    dca_str = f"  dca×{dca_n}" if show_dca else ""
    print(f"  {label:<44}  n={n:>3}{eod}  W/L={wins}/{losses}"
          f"  PnL={pnl:>+7.2f}  avg={avg:>+5.2f}  worst={worst:>+6.2f}  best={best:>+5.2f}"
          f"  fees={fees:.2f}{dca_str}  [TP×{tp_c} stop×{stop_c} sig×{sig_c}]")


def hdr(w=44):
    print(f"  {'Scenario':<{w}}  {'n':>4}       {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
          f"  {'worst':>7}  {'best':>6}  fees   exits")
    print(f"  {'─'*w}  {'─'*4}  {'─'*5}  {'─'*7}  {'─'*8}  {'─'*6}"
          f"  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*22}")


if __name__ == "__main__":
    args      = [a for a in sys.argv[1:] if a.isdigit()]
    days_list = [int(a) for a in args] or [365, 180]
    start     = config.SIMULATION_BALANCE
    rsi_p     = config.rsi_period_for(PAIR)

    print(f"\n{'='*110}")
    print(f"  Full parameter sweep — {PAIR} 15m RSI({rsi_p})")
    print(f"  Baseline: buy<{config.RSI_OVERSOLD}  sell>{config.RSI_OVERBOUGHT}"
          f"  floor={config.PROFIT_FLOOR_PCT*100:.0f}%  trail={config.TRAILING_STOP_PCT*100:.0f}%"
          f"  TP={config.TAKE_PROFIT_PCT*100:.0f}%  pos={config.POSITION_SIZE_PCT*100:.0f}%"
          f"  dca_drop={config.DCA_DROP_PCT*100:.0f}%")
    print(f"{'='*110}")

    for days in days_list:
        print(f"\n{'━'*110}")
        print(f"  {days}d window")
        print(f"{'━'*110}")

        df = fetch(days)
        rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = precompute(df, rsi_p)

        # ── Sell RSI + TP sweep ──────────────────────────────────────────
        print(f"\n  Sell RSI & take-profit sweep")
        hdr()
        for rsi_sell, tp, lbl in [
            (65, 0.10, "sell>65  TP=10%  (old)"),
            (65, 0.05, "sell>65  TP=5%"),
            (70, 0.05, "sell>70  TP=5%"),
            (75, 0.05, "sell>75  TP=5%  ◄ current"),
            (80, 0.05, "sell>80  TP=5%"),
            (75, 0.07, "sell>75  TP=7%"),
            (75, 0.10, "sell>75  TP=10%"),
        ]:
            t, _ = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr, start, rsi_sell=rsi_sell, tp_pct=tp)
            report(lbl, t)

        # ── Profit floor sweep ───────────────────────────────────────────
        print(f"\n  Profit floor sweep")
        hdr()
        for floor, lbl in [
            (0.01, "floor=1%"),
            (0.02, "floor=2%"),
            (0.03, "floor=3%  ◄ current"),
            (0.04, "floor=4%"),
            (0.05, "floor=5%"),
            (0.06, "floor=6%"),
        ]:
            t, _ = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr, start, floor_pct=floor)
            report(lbl, t)

        # ── Trailing stop sweep ──────────────────────────────────────────
        print(f"\n  Trailing stop sweep")
        hdr()
        for trail, lbl in [
            (0.02, "trail=2%"),
            (0.03, "trail=3%"),
            (0.04, "trail=4%"),
            (0.05, "trail=5%  ◄ current"),
            (0.06, "trail=6%"),
            (0.07, "trail=7%"),
            (0.08, "trail=8%"),
        ]:
            t, _ = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr, start, trail_pct=trail)
            report(lbl, t)

        # ── Buy RSI sweep ────────────────────────────────────────────────
        print(f"\n  Buy RSI threshold sweep")
        hdr()
        for rsi_buy, lbl in [
            (25, "buy<25"),
            (28, "buy<28"),
            (30, "buy<30  ◄ current"),
            (32, "buy<32"),
            (33, "buy<33"),
            (35, "buy<35"),
            (38, "buy<38"),
        ]:
            t, _ = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr, start, rsi_buy=rsi_buy)
            report(lbl, t)

        # ── DCA sweeps ───────────────────────────────────────────────────
        print(f"\n  DCA — no DCA baseline")
        hdr()
        t, _ = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr, start, enable_dca=False)
        report("no DCA", t, show_dca=True)

        print(f"\n  DCA — drop % sweep  (RSI<{config.DCA_RSI_THRESHOLD}  size={config.DCA_SIZE_PCT*100:.0f}%)")
        hdr()
        for drop, lbl in [
            (0.01, "dca_drop=1%  ◄ current"),
            (0.02, "dca_drop=2%"),
            (0.03, "dca_drop=3%"),
            (0.04, "dca_drop=4%"),
            (0.05, "dca_drop=5%"),
        ]:
            t, _ = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr, start, dca_drop=drop)
            report(lbl, t, show_dca=True)

        print(f"\n  DCA — size % sweep  (drop={config.DCA_DROP_PCT*100:.0f}%  RSI<{config.DCA_RSI_THRESHOLD})")
        hdr()
        for pct, lbl in [
            (0.25, "dca_size=25%"),
            (0.50, "dca_size=50%"),
            (0.75, "dca_size=75%  ◄ current"),
            (1.00, "dca_size=100%"),
        ]:
            t, _ = run(rsi_arr, ema_arr, close_arr, hi_arr, lo_arr, start, dca_pct=pct)
            report(lbl, t, show_dca=True)

        print()
