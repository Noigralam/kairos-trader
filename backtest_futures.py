"""
Futures backtest tool. Simulates isolated-margin USDT-M perpetual longs.

Usage:
    python backtest_futures.py [days]               # baseline with current config
    python backtest_futures.py sweep trail [days]   # trailing stop %
    python backtest_futures.py sweep tp    [days]   # take-profit %
    python backtest_futures.py sweep pos   [days]   # position size %
    python backtest_futures.py sweep dca   [days]   # DCA drop % + size %
    python backtest_futures.py sweep floor [days]   # profit floor %
    python backtest_futures.py sweep lev   [days]   # leverage
    python backtest_futures.py sweep all   [days]   # all sweeps

Days can be one or multiple values: python backtest_futures.py sweep trail 365 180 90
"""
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from bot import config
from bot.candles import get_df, initial_sync

PAIRS   = ["ETHUSDT", "SOLUSDT"]
INTERVAL = "15m"
WARMUP   = 210
CANDLES_PER_DAY = 96          # 15m candles
FUNDING_INTERVAL = 32         # 8h / 15m = 32 candles per funding settlement
FUNDING_RATE     = 0.0001     # 0.01% per 8h — conservative estimate for longs


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def fetch(pair: str, days: int) -> pd.DataFrame:
    needed = days * CANDLES_PER_DAY + WARMUP
    df = get_df(pair, INTERVAL, limit=needed)
    if len(df) < needed * 0.9:
        print(f"  Syncing {pair}/15m {days}d…", flush=True)
        initial_sync(pair, INTERVAL, days=days)
        df = get_df(pair, INTERVAL, limit=needed)
    return df


def precompute(df: pd.DataFrame, rsi_period: int, ema_span: int = 200):
    close = df["close"]
    ema   = close.ewm(span=ema_span, adjust=False).mean()
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    avg_l = loss.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    rsi   = 100 - (100 / (1 + avg_g / avg_l))
    return rsi.values, ema.values, close.values, df["high"].values, df["low"].values


# ---------------------------------------------------------------------------
# Core simulator
# ---------------------------------------------------------------------------

@dataclass
class FTrade:
    pnl:         float
    fees:        float
    funding:     float
    exit_reason: str
    dca_used:    bool = False


def run_pair(
    pair: str, df: pd.DataFrame, start_balance: float,
    leverage:   int   = None,
    pos_pct:    float = None,
    dca_pct:    float = None,
    dca_drop:   float = None,
    tp_pct:     float = None,
    trail_pct:  float = None,
    floor_pct:  float = None,
    rsi_buy:    int   = None,
    rsi_sell:   int   = None,
    rsi_period: int   = None,
    ema_gap:    float = None,
    enable_dca: bool  = True,
    funding_rate: float = FUNDING_RATE,
) -> tuple[list[FTrade], float]:

    fee_rate   = config.FUTURES_FEE
    leverage   = leverage   if leverage   is not None else config.FUTURES_LEVERAGE
    pos_pct    = pos_pct    if pos_pct    is not None else config.FUTURES_POSITION_SIZE_PCT
    dca_pct    = dca_pct    if dca_pct    is not None else config.FUTURES_DCA_SIZE_PCT
    dca_drop   = dca_drop   if dca_drop   is not None else config.FUTURES_DCA_DROP_PCT
    tp_pct     = tp_pct     if tp_pct     is not None else config.FUTURES_TAKE_PROFIT_PCT
    trail_pct  = trail_pct  if trail_pct  is not None else config.FUTURES_TRAILING_STOP_PCT
    floor_pct  = floor_pct  if floor_pct  is not None else config.FUTURES_PROFIT_FLOOR_PCT
    rsi_buy    = rsi_buy    if rsi_buy    is not None else config.futures_rsi_oversold_for(pair)
    rsi_sell   = rsi_sell   if rsi_sell   is not None else config.futures_rsi_overbought_for(pair)
    rsi_period = rsi_period if rsi_period is not None else config.futures_rsi_period_for(pair)
    ema_gap    = ema_gap    if ema_gap    is not None else config.futures_ema_gap_for(pair)

    mmr = 0.005   # maintenance margin rate (0.5% — conservative for ETH/SOL)

    rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = precompute(df, rsi_period)

    balance      = start_balance
    trades: list[FTrade] = []

    # Position state
    pos_entry:   float = 0.0
    pos_amount:  float = 0.0
    pos_margin:  float = 0.0
    pos_peak:    float = 0.0
    pos_dca:     bool  = False
    pos_total_funding: float = 0.0
    pos_entry_fee:     float = 0.0
    entry_candle: int  = 0
    in_pos:       bool = False

    def liq_price(entry: float) -> float:
        return entry * (1 - 1 / leverage + mmr)

    def trail_stop(entry: float, peak: float) -> float:
        floor = entry * (1 + fee_rate + floor_pct) / (1 - fee_rate)
        return max(floor, peak * (1 - trail_pct))

    def tp_price(entry: float) -> float:
        return entry * (1 + tp_pct)

    for i in range(WARMUP, len(close_arr)):
        price = float(close_arr[i])
        hi    = float(hi_arr[i])
        lo    = float(lo_arr[i])
        rsi   = float(rsi_arr[i])
        ema   = float(ema_arr[i])

        if in_pos:
            # Update peak for trailing stop
            if hi > pos_peak:
                pos_peak = hi

            # Apply funding every funding_interval candles
            if (i - entry_candle) % FUNDING_INTERVAL == 0 and i > entry_candle:
                cost = pos_entry * pos_amount * funding_rate
                pos_total_funding += cost

            liq = liq_price(pos_entry)
            tp  = tp_price(pos_entry)
            ts  = trail_stop(pos_entry, pos_peak)

            # Liquidation (check low first — worst case)
            if lo <= liq:
                exit_fee = liq * pos_amount * fee_rate
                pnl = (liq - pos_entry) * pos_amount - pos_entry_fee - exit_fee - pos_total_funding
                balance += max(0.0, pos_margin + pnl)   # isolated — floored at 0
                trades.append(FTrade(pnl, pos_entry_fee + exit_fee, pos_total_funding, "liquidation", pos_dca))
                in_pos = False
                continue

            # Take profit (check high)
            if hi >= tp:
                exit_fee = tp * pos_amount * fee_rate
                pnl = (tp - pos_entry) * pos_amount - pos_entry_fee - exit_fee - pos_total_funding
                balance += pos_margin + pnl
                trades.append(FTrade(pnl, pos_entry_fee + exit_fee, pos_total_funding, "take_profit", pos_dca))
                in_pos = False
                continue

            # Trailing stop (only once peak is above the stop level)
            if pos_peak > ts and lo <= ts:
                ep = ts
                exit_fee = ep * pos_amount * fee_rate
                pnl = (ep - pos_entry) * pos_amount - pos_entry_fee - exit_fee - pos_total_funding
                balance += pos_margin + pnl
                trades.append(FTrade(pnl, pos_entry_fee + exit_fee, pos_total_funding, "trailing_stop", pos_dca))
                in_pos = False
                continue

            # DCA — one tranche only
            if enable_dca and not pos_dca:
                drop = (pos_entry - price) / pos_entry
                if drop >= dca_drop:
                    dca_margin  = min(balance * dca_pct, balance)
                    if dca_margin >= 1:
                        dca_notional = dca_margin * leverage
                        dca_amount   = dca_notional / price
                        dca_fee      = dca_notional * fee_rate
                        total_amount = pos_amount + dca_amount
                        # weighted avg entry
                        pos_entry    = (pos_entry * pos_amount + price * dca_amount) / total_amount
                        pos_amount   = total_amount
                        pos_margin  += dca_margin
                        pos_peak     = price
                        pos_dca      = True
                        pos_entry_fee += dca_fee
                        balance      -= dca_margin + dca_fee

        else:
            # Entry: RSI oversold + price above EMA
            if (rsi < rsi_buy and price > ema * (1 + ema_gap)):
                margin   = balance * pos_pct
                if margin < 1:
                    continue
                notional = margin * leverage
                amount   = notional / price
                entry_fee = notional * fee_rate

                pos_entry    = price
                pos_amount   = amount
                pos_margin   = margin
                pos_peak     = price
                pos_dca      = False
                pos_total_funding = 0.0
                pos_entry_fee     = entry_fee
                entry_candle      = i
                in_pos            = True
                balance          -= margin + entry_fee

    # Mark open position to last close price
    if in_pos:
        ep = float(close_arr[-1])
        exit_fee = ep * pos_amount * fee_rate
        pnl = (ep - pos_entry) * pos_amount - pos_entry_fee - exit_fee - pos_total_funding
        balance += pos_margin + pnl
        trades.append(FTrade(pnl, pos_entry_fee + exit_fee, pos_total_funding, "end_of_data", pos_dca))

    return trades, balance


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summarise(label: str, trades: list[FTrade], start: float):
    realized = [t for t in trades if t.exit_reason != "end_of_data"]
    open_eod = len(trades) - len(realized)
    if not realized:
        print(f"  {label:<46}  no closed trades")
        return
    pnl      = sum(t.pnl     for t in realized)
    fees     = sum(t.fees    for t in realized)
    funding  = sum(t.funding for t in realized)
    wins     = sum(1 for t in realized if t.pnl > 0)
    liq      = sum(1 for t in realized if t.exit_reason == "liquidation")
    n        = len(realized)
    avg      = pnl / n
    worst    = min(t.pnl for t in realized)
    best     = max(t.pnl for t in realized)
    eod      = f"+{open_eod}" if open_eod else "  "
    reasons  = {}
    for t in realized:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    exits = "  ".join(f"{k}×{v}" for k, v in reasons.items())
    liq_flag = f"  ⚠ {liq}×LIQ" if liq else ""

    print(f"  {label:<46}  n={n:>3}{eod}  W/L={wins}/{n-wins}"
          f"  PnL={pnl:>+7.2f}  avg={avg:>+5.2f}  worst={worst:>+6.2f}  best={best:>+5.2f}"
          f"  fees={fees:.2f}  fund={funding:.2f}  [{exits}]{liq_flag}")


def _header(days: int, title: str):
    print(f"\n{'━'*135}")
    print(f"  {days}d — {title}  "
          f"(start=${config.FUTURES_SIMULATION_BALANCE:.0f}  lev={config.FUTURES_LEVERAGE}x"
          f"  fee={config.FUTURES_FEE*100:.3f}%  funding≈{FUNDING_RATE*100:.3f}%/8h)")
    print(f"{'━'*135}")
    print(f"  {'Scenario':<46}  {'n':>3}      {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
          f"  {'worst':>7}  {'best':>6}  fees   funding  exits")
    print(f"  {'─'*46}  {'─'*3}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*7}")


def _mark_current(scenarios: list[dict], **current):
    for s in scenarios:
        match = all(
            abs(s.get(k, float("nan")) - v) < 1e-9 if isinstance(v, float) else s.get(k) == v
            for k, v in current.items()
        )
        if match:
            s["label"] += "  ◄ current"


def _run_sweep(days_list: list[int], title: str, scenarios: list[dict]):
    start = config.FUTURES_SIMULATION_BALANCE
    for days in days_list:
        _header(days, title)
        for pair in PAIRS:
            df = fetch(pair, days)
            print(f"\n  ── {pair} ──")
            for s in scenarios:
                kwargs = {k: v for k, v in s.items() if k != "label"}
                trades, _ = run_pair(pair, df, start, **kwargs)
                summarise(s["label"], trades, start)
        print()


# ---------------------------------------------------------------------------
# Sweep modes
# ---------------------------------------------------------------------------

def sweep_trail(days_list: list[int]):
    scenarios = [
        dict(trail_pct=0.02, label="trail=2%"),
        dict(trail_pct=0.03, label="trail=3%"),
        dict(trail_pct=0.04, label="trail=4%"),
        dict(trail_pct=0.05, label="trail=5%"),
        dict(trail_pct=0.06, label="trail=6%"),
        dict(trail_pct=0.07, label="trail=7%"),
        dict(trail_pct=0.08, label="trail=8%"),
    ]
    _mark_current(scenarios, trail_pct=config.FUTURES_TRAILING_STOP_PCT)
    _run_sweep(days_list, "Trailing stop sweep", scenarios)


def sweep_tp(days_list: list[int]):
    scenarios = [
        dict(tp_pct=0.03, label="TP=3%"),
        dict(tp_pct=0.04, label="TP=4%"),
        dict(tp_pct=0.05, label="TP=5%"),
        dict(tp_pct=0.06, label="TP=6%"),
        dict(tp_pct=0.07, label="TP=7%"),
        dict(tp_pct=0.08, label="TP=8%"),
        dict(tp_pct=0.10, label="TP=10%"),
        dict(tp_pct=0.12, label="TP=12%"),
    ]
    _mark_current(scenarios, tp_pct=config.FUTURES_TAKE_PROFIT_PCT)
    _run_sweep(days_list, "Take-profit sweep", scenarios)


def sweep_pos(days_list: list[int]):
    scenarios = [
        dict(pos_pct=0.25, label="pos=25%"),
        dict(pos_pct=0.33, label="pos=33%"),
        dict(pos_pct=0.50, label="pos=50%"),
        dict(pos_pct=0.60, label="pos=60%"),
        dict(pos_pct=0.75, label="pos=75%"),
        dict(pos_pct=1.00, label="pos=100% (all-in)"),
    ]
    _mark_current(scenarios, pos_pct=config.FUTURES_POSITION_SIZE_PCT)
    _run_sweep(days_list, "Position size sweep (% of balance per trade)", scenarios)


def sweep_dca(days_list: list[int]):
    start = config.FUTURES_SIMULATION_BALANCE
    for days in days_list:
        _header(days, "DCA parameter sweep")
        for pair in PAIRS:
            df = fetch(pair, days)
            print(f"\n  ── {pair} ──")
            trades, _ = run_pair(pair, df, start, enable_dca=False)
            summarise("no DCA  (baseline)", trades, start)
            for drop in [0.005, 0.01, 0.015, 0.02, 0.03]:
                for dca_pct in [0.50, 0.75]:
                    cur = (abs(drop - config.FUTURES_DCA_DROP_PCT) < 1e-9 and
                           abs(dca_pct - config.FUTURES_DCA_SIZE_PCT) < 1e-9)
                    label = (f"drop={drop*100:.1f}%  dca_size={dca_pct*100:.0f}%"
                             + ("  ◄ current" if cur else ""))
                    trades, _ = run_pair(pair, df, start, dca_drop=drop, dca_pct=dca_pct)
                    summarise(label, trades, start)
        print()


def sweep_floor(days_list: list[int]):
    scenarios = [
        dict(floor_pct=0.01, label="floor=1%"),
        dict(floor_pct=0.02, label="floor=2%"),
        dict(floor_pct=0.03, label="floor=3%"),
        dict(floor_pct=0.04, label="floor=4%"),
        dict(floor_pct=0.05, label="floor=5%"),
    ]
    _mark_current(scenarios, floor_pct=config.FUTURES_PROFIT_FLOOR_PCT)
    _run_sweep(days_list, "Profit floor sweep (trailing stop rises once above this)", scenarios)


def sweep_lev(days_list: list[int]):
    scenarios = [
        dict(leverage=1,  label="1x  (no leverage)"),
        dict(leverage=2,  label="2x"),
        dict(leverage=3,  label="3x"),
        dict(leverage=5,  label="5x"),
        dict(leverage=10, label="10x"),
    ]
    _mark_current(scenarios, leverage=config.FUTURES_LEVERAGE)
    _run_sweep(days_list, "Leverage sweep", scenarios)


def sweep_rsi(days_list: list[int]):
    scenarios = [
        dict(rsi_buy=25, rsi_sell=70, label="buy<25  sell>70"),
        dict(rsi_buy=25, rsi_sell=75, label="buy<25  sell>75"),
        dict(rsi_buy=30, rsi_sell=70, label="buy<30  sell>70"),
        dict(rsi_buy=30, rsi_sell=75, label="buy<30  sell>75"),
        dict(rsi_buy=30, rsi_sell=80, label="buy<30  sell>80"),
        dict(rsi_buy=35, rsi_sell=70, label="buy<35  sell>70"),
        dict(rsi_buy=35, rsi_sell=75, label="buy<35  sell>75"),
        dict(rsi_buy=35, rsi_sell=80, label="buy<35  sell>80"),
    ]
    for s in scenarios:
        pair = PAIRS[0]
        if (abs(s["rsi_buy"] - config.futures_rsi_oversold_for(pair)) < 1e-9 and
                abs(s["rsi_sell"] - config.futures_rsi_overbought_for(pair)) < 1e-9):
            s["label"] += "  ◄ current"
    _run_sweep(days_list, "RSI thresholds sweep (buy oversold / sell overbought)", scenarios)


# ---------------------------------------------------------------------------
# Baseline run
# ---------------------------------------------------------------------------

def run_baseline(days_list: list[int]):
    start = config.FUTURES_SIMULATION_BALANCE
    print(f"\nFutures baseline — current config")
    print(f"  pairs={PAIRS}  lev={config.FUTURES_LEVERAGE}x  "
          f"pos={config.FUTURES_POSITION_SIZE_PCT*100:.0f}%  "
          f"TP={config.FUTURES_TAKE_PROFIT_PCT*100:.0f}%  "
          f"trail={config.FUTURES_TRAILING_STOP_PCT*100:.0f}%  "
          f"floor={config.FUTURES_PROFIT_FLOOR_PCT*100:.0f}%  "
          f"DCA drop={config.FUTURES_DCA_DROP_PCT*100:.1f}%  "
          f"RSI buy<{config.futures_rsi_oversold_for(PAIRS[0])}  "
          f"sell>{config.futures_rsi_overbought_for(PAIRS[0])}")
    for days in days_list:
        _header(days, "current config")
        for pair in PAIRS:
            df = fetch(pair, days)
            print(f"\n  ── {pair} ──")
            trades, final = run_pair(pair, df, start)
            summarise(f"current config", trades, start)
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args     = sys.argv[1:]
    day_args = [int(a) for a in args if a.isdigit()]
    str_args = [a for a in args if not a.isdigit()]

    days_list = day_args or [730, 365]

    if str_args and str_args[0] == "sweep":
        mode = str_args[1].lower() if len(str_args) > 1 else "all"
        sweeps = {
            "trail": sweep_trail,
            "tp":    sweep_tp,
            "pos":   sweep_pos,
            "dca":   sweep_dca,
            "floor": sweep_floor,
            "lev":   sweep_lev,
            "rsi":   sweep_rsi,
        }
        if mode == "all":
            for fn in sweeps.values():
                fn(days_list)
        elif mode in sweeps:
            sweeps[mode](days_list)
        else:
            print(f"Unknown sweep '{mode}'. Options: {', '.join(sweeps)}, all")
            sys.exit(1)
    else:
        run_baseline(days_list)
