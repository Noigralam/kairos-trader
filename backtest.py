"""
Unified backtest tool.

Usage:
    python backtest.py [days]               # pair comparison: solo vs combined
    python backtest.py sweep exit  [days]   # sell RSI + take-profit sweep
    python backtest.py sweep floor [days]   # profit floor sweep
    python backtest.py sweep trail [days]   # trailing stop sweep
    python backtest.py sweep buyrsi [days]  # buy RSI threshold sweep
    python backtest.py sweep dca   [days]   # DCA parameters sweep
    python backtest.py sweep ema   [days]   # EMA trend filter period sweep
    python backtest.py sweep all   [days]   # run every sweep
    python backtest.py topup [start] [monthly] [days]  # monthly top-up simulation

Days can be one or multiple values: python backtest.py sweep exit 365 180 90
"""
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from bot import config
from bot.candles import get_df, initial_sync
from bot.spot_risk import Position, apply_dca, update_peak, calc_pnl

INTERVAL = "15m"
WARMUP   = 210
PAIRS    = ["ETHEUR", "SOLEUR"]
CANDLES_PER_DAY = {"1m": 1440, "5m": 288, "15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def fetch(pair: str, days: int, interval: str = INTERVAL) -> pd.DataFrame:
    cpd    = CANDLES_PER_DAY.get(interval, 96)
    needed = days * cpd + WARMUP
    df = get_df(pair, interval, limit=needed)
    if len(df) >= needed * 0.9:
        return df
    print(f"  Syncing {pair}/{interval} {days}d…", flush=True)
    initial_sync(pair, interval, days=days)
    return get_df(pair, interval, limit=needed)


def fetch_daily_ema(pair: str, days: int, ema_span: int = 200) -> pd.Series:
    """Daily EMA series indexed by open_time (UTC ms). Syncs 1d candles if needed."""
    needed = days + ema_span + 10
    df = get_df(pair, "1d", limit=needed)
    if len(df) < needed * 0.8:
        print(f"  Syncing {pair}/1d…", flush=True)
        initial_sync(pair, "1d", days=needed)
        df = get_df(pair, "1d", limit=needed)
    ema = df["close"].ewm(span=ema_span, adjust=False).mean()
    return df[["open_time"]].assign(daily_ema=ema.values).set_index("open_time")["daily_ema"]


def fetch_htf_rsi(pair: str, days: int, interval: str, rsi_period: int) -> pd.Series:
    """RSI series on a higher timeframe, indexed by open_time."""
    df = fetch(pair, days, interval=interval)
    close = df["close"]
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    avg_l = loss.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    rsi   = 100 - (100 / (1 + avg_g / avg_l))
    return df[["open_time"]].assign(rsi=rsi.values).set_index("open_time")["rsi"]


def align_htf(df_base: pd.DataFrame, htf_series: pd.Series, col: str = "value") -> np.ndarray:
    """Forward-fill a higher-timeframe series onto the base (lower-tf) candle index."""
    htf_df = htf_series.reset_index()
    htf_df.columns = ["open_time", col]
    merged = pd.merge_asof(
        df_base[["open_time"]].sort_values("open_time"),
        htf_df.sort_values("open_time"),
        on="open_time",
    )
    return merged[col].values


def align_daily_ema(df_15m: pd.DataFrame, daily_ema: pd.Series) -> np.ndarray:
    """For each 15m candle return the most recent daily EMA value (forward-filled)."""
    daily_df = daily_ema.reset_index()
    daily_df.columns = ["open_time", "daily_ema"]
    merged = pd.merge_asof(
        df_15m[["open_time"]].sort_values("open_time"),
        daily_df.sort_values("open_time"),
        on="open_time",
    )
    return merged["daily_ema"].values


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


def _bullish_divergence(close_arr, rsi_arr, i: int, lookback: int, rsi_oversold: int) -> bool:
    """
    Bullish RSI divergence: price makes a lower low while RSI makes a higher low
    vs the most recent prior oversold candle within `lookback` bars.
    Requires at least 5 candles separation to avoid same-dip comparisons.
    """
    search_start = max(0, i - lookback)
    prev_j = None
    for j in range(i - 5, search_start - 1, -1):
        if rsi_arr[j] < rsi_oversold:
            prev_j = j
            break
    if prev_j is None:
        return False
    return close_arr[i] < close_arr[prev_j] and rsi_arr[i] > rsi_arr[prev_j]


# ---------------------------------------------------------------------------
# Core simulator
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    pnl:         float
    fees:        float
    exit_reason: str
    dca_used:    bool = False


def run_pair(
    pair: str, df: pd.DataFrame, start_balance: float,
    pos_pct:    float = None,
    dca_pct:    float = None,
    dca_drop:   float = None,
    enable_dca: bool  = True,
    tp_pct:     float = None,
    trail_pct:  float = None,
    floor_pct:  float = None,
    rsi_buy:    int   = None,
    rsi_sell:   int   = None,
    rsi_period: int   = None,
    ema_span:   int   = 200,
    min_exit:     float = None,
    ema_filter:   bool  = True,
    hard_stop:       float = None,  # e.g. 0.20 = cut loss at -20% from entry
    ema_gap:         float = 0.0,   # require price > ema * (1 + ema_gap) to buy
    time_stop_days:  float = 0,     # close if no profit floor reached within N days (0 = off)
    max_drawdown:    float = 0,     # pause buys if portfolio drops >X% from peak (0 = off)
    strong_gap:      float = None,       # price > ema*(1+strong_gap) → use pos_pct; below → use weak_pct
    weak_pct:        float = None,       # reduced size for borderline entries (between ema_gap and strong_gap)
    max_dca:         int   = 1,          # max number of DCA tranches (1 = current behaviour)
    dca_step:        float = None,       # additional drop % per DCA level; defaults to dca_drop
    daily_ema_arr:    np.ndarray = None,  # daily EMA200 aligned to df index; None = disabled
    htf_rsi_arr:      np.ndarray = None,  # higher-TF RSI aligned to df index; None = disabled
    htf_rsi_threshold: float     = 40,   # HTF RSI must be below this to allow a buy
    interval:         str = INTERVAL,    # candle interval (used for time_stop_days calc)
    vol_period:       int   = 0,         # 0 = disabled; N = require volume > vol_mult * N-period average
    vol_mult:         float = 1.0,       # volume must exceed this multiple of the rolling average
    div_lookback:     int   = 0,         # 0 = disabled; N = require bullish RSI divergence within N bars
    stop_cooldown:    int   = 0,         # 0 = disabled; N = block re-entry for N candles after trailing stop
) -> tuple[list[Trade], float]:

    fee_rate   = config.SPOT_FEE
    pos_pct    = pos_pct    if pos_pct    is not None else config.SPOT_POSITION_SIZE_PCT
    dca_drop   = dca_drop   if dca_drop   is not None else config.SPOT_DCA_DROP_PCT
    dca_pct    = dca_pct    if dca_pct    is not None else config.SPOT_DCA_SIZE_PCT
    tp_pct     = tp_pct     if tp_pct     is not None else config.SPOT_TAKE_PROFIT_PCT
    trail_pct  = trail_pct  if trail_pct  is not None else config.SPOT_TRAILING_STOP_PCT
    floor_pct  = floor_pct  if floor_pct  is not None else config.SPOT_PROFIT_FLOOR_PCT
    rsi_buy    = rsi_buy    if rsi_buy    is not None else config.SPOT_RSI_OVERSOLD
    rsi_sell   = rsi_sell   if rsi_sell   is not None else config.rsi_overbought_for(pair)
    rsi_period = rsi_period if rsi_period is not None else config.rsi_period_for(pair)
    min_exit   = min_exit   if min_exit   is not None else config.SPOT_MIN_EXIT_PROFIT_PCT

    rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = precompute(df, rsi_period, ema_span)

    vol_arr    = df["volume"].values
    vol_ma_arr = (df["volume"].rolling(vol_period).mean().values
                  if vol_period > 0 else None)

    balance        = start_balance
    portfolio_peak = start_balance
    position: Position | None = None
    entry_candle:  int = 0
    dca_count:     int = 0
    cooldown_until: int = 0
    trades: list[Trade] = []
    _dca_step = dca_step if dca_step is not None else dca_drop

    candles_per_day = CANDLES_PER_DAY.get(interval, 96)

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
                trades.append(Trade(calc_pnl(position, ep, bf, sf), bf + sf, "take_profit", position.dca_count > 0))
                position = None
                continue

            if position.peak() > stop_level and lo <= stop_level:
                ep = stop_level
                bf = position.value_eur * fee_rate
                sf = position.amount * ep * fee_rate
                balance += position.amount * ep - sf
                trades.append(Trade(calc_pnl(position, ep, bf, sf), bf + sf, "trailing_stop", position.dca_count > 0))
                position = None
                if stop_cooldown > 0:
                    cooldown_until = i + stop_cooldown
                continue

            if hard_stop is not None and lo <= position.entry_price * (1 - hard_stop):
                ep = position.entry_price * (1 - hard_stop)
                bf = position.value_eur * fee_rate
                sf = position.amount * ep * fee_rate
                balance += position.amount * ep - sf
                trades.append(Trade(calc_pnl(position, ep, bf, sf), bf + sf, "hard_stop", position.dca_count > 0))
                position = None
                continue

            if (time_stop_days > 0
                    and (i - entry_candle) > time_stop_days * candles_per_day
                    and position.peak() <= stop_level):
                bf = position.value_eur * fee_rate
                sf = position.amount * price * fee_rate
                balance += position.amount * price - sf
                trades.append(Trade(calc_pnl(position, price, bf, sf), bf + sf, "time_stop", position.dca_count > 0))
                position = None
                continue

        # update portfolio peak (cash only when no position, cash+pos otherwise)
        pos_val = position.amount * price if position is not None else 0.0
        portfolio_peak = max(portfolio_peak, balance + pos_val)

        daily_ema_val = float(daily_ema_arr[i]) if daily_ema_arr is not None else None
        htf_rsi_val   = float(htf_rsi_arr[i])  if htf_rsi_arr  is not None else None

        vol_ok = (vol_ma_arr is None
                  or np.isnan(vol_ma_arr[i])
                  or vol_arr[i] > vol_ma_arr[i] * vol_mult)

        div_ok = (div_lookback == 0
                  or _bullish_divergence(close_arr, rsi_arr, i, div_lookback, rsi_buy))

        if position is None and i >= cooldown_until and rsi < rsi_buy and vol_ok and div_ok \
                and (not ema_filter or price > ema * (1 + ema_gap)) \
                and (daily_ema_val is None or np.isnan(daily_ema_val) or price > daily_ema_val) \
                and (htf_rsi_val   is None or np.isnan(htf_rsi_val)  or htf_rsi_val < htf_rsi_threshold):
            if max_drawdown > 0 and portfolio_peak > 0:
                drawdown = (portfolio_peak - balance) / portfolio_peak
                if drawdown > max_drawdown:
                    continue
            effective_pct = pos_pct
            if strong_gap is not None and weak_pct is not None and price < ema * (1 + strong_gap):
                effective_pct = weak_pct
            max_size = balance / (1 + fee_rate)
            size = min(balance * effective_pct, max_size)
            if size >= 1:
                position = Position(pair, price, size / price, size,
                                    price * (1 + tp_pct), price)
                entry_candle = i
                dca_count    = 0
                balance -= size + size * fee_rate

        if enable_dca and position is not None and dca_count < max_dca:
            next_drop = dca_drop + dca_count * _dca_step
            drop = (position.entry_price - price) / position.entry_price
            if drop >= next_drop:
                max_size  = balance / (1 + fee_rate)
                dca_value = min(balance * dca_pct, max_size)
                if dca_value >= 1:
                    apply_dca(position, price, dca_value)
                    dca_count += 1
                    balance -= dca_value + dca_value * fee_rate

        if position is not None and rsi > rsi_sell:
            if price >= position.entry_price * (1 + min_exit):
                bf = position.value_eur * fee_rate
                sf = position.amount * price * fee_rate
                balance += position.amount * price - sf
                trades.append(Trade(calc_pnl(position, price, bf, sf), bf + sf, "signal", position.dca_count > 0))
                position = None

    if position is not None:
        price = float(close_arr[-1])
        bf = position.value_eur * fee_rate
        sf = position.amount * price * fee_rate
        balance += position.amount * price - sf
        trades.append(Trade(calc_pnl(position, price, bf, sf), bf + sf, "end_of_data", position.dca_count > 0))

    return trades, balance


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summarise(label: str, trades: list[Trade], start: float, wide: bool = False):
    realized = [t for t in trades if t.exit_reason != "end_of_data"]
    open_eod = len(trades) - len(realized)
    if not realized:
        print(f"  {label:<44}  no closed trades")
        return
    pnl   = sum(t.pnl  for t in realized)
    fees  = sum(t.fees for t in realized)
    wins  = sum(1 for t in realized if t.pnl > 0)
    n     = len(realized)
    avg   = pnl / n
    worst = min(t.pnl for t in realized)
    best  = max(t.pnl for t in realized)
    eod   = f"+{open_eod}" if open_eod else "  "
    reasons = {}
    for t in realized:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    exits = "  ".join(f"{k}×{v}" for k, v in reasons.items())

    if wide:
        dca_n = sum(1 for t in realized if t.dca_used)
        print(f"  {label:<44}  n={n:>3}{eod}  W/L={wins}/{n-wins}"
              f"  PnL={pnl:>+7.2f}  avg={avg:>+5.2f}  worst={worst:>+6.2f}  best={best:>+5.2f}"
              f"  fees={fees:.2f}  dca×{dca_n}  [{exits}]")
    else:
        tax   = min(max(pnl, 0), 30000) * 0.30 + max(max(pnl, 0) - 30000, 0) * 0.34
        after = start + pnl - (tax if pnl > 0 else 0)
        print(f"  {label:<36}  n={n:>3}  {eod:>4}  W/L={wins}/{n-wins}"
              f"  PnL={pnl:>+7.2f}  after-tax={after:>7.2f}"
              f"  worst={worst:>+7.2f}  fees={fees:.2f}  [{exits}]")


def _header(days: int, title: str, wide: bool = False):
    width = 125 if wide else 105
    print(f"\n{'━'*width}")
    print(f"  {days}d — {title}")
    print(f"{'━'*width}")


# ---------------------------------------------------------------------------
# Sweep helpers
# ---------------------------------------------------------------------------

def _mark_current(scenarios: list[dict], **current):
    for s in scenarios:
        match = all(
            abs(s.get(k, float("nan")) - v) < 1e-9 if isinstance(v, float) else s.get(k) == v
            for k, v in current.items()
        )
        if match:
            s["label"] += "  ◄ current"


def _run_sweep(days_list: list[int], title: str, scenarios: list[dict], pairs: list[str]):
    start = config.SPOT_SIMULATION_BALANCE
    for days in days_list:
        _header(days, title, wide=True)
        print(f"  {'Scenario':<44}  {'n':>3}      {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
              f"  {'worst':>7}  {'best':>6}  fees   dca  exits")
        print(f"  {'─'*44}  {'─'*3}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*4}")
        for pair in pairs:
            df = fetch(pair, days)
            print(f"\n  ── {pair} ──")
            for s in scenarios:
                kwargs = {k: v for k, v in s.items() if k != "label"}
                trades, _ = run_pair(pair, df, start, **kwargs)
                summarise(s["label"], trades, start, wide=True)
        print()


# ---------------------------------------------------------------------------
# Sweep modes
# ---------------------------------------------------------------------------

def sweep_exit(days_list: list[int]):
    scenarios = [
        dict(rsi_sell=65, tp_pct=0.05, label="sell>65  TP=5%"),
        dict(rsi_sell=65, tp_pct=0.07, label="sell>65  TP=7%"),
        dict(rsi_sell=65, tp_pct=0.10, label="sell>65  TP=10%"),
        dict(rsi_sell=70, tp_pct=0.05, label="sell>70  TP=5%"),
        dict(rsi_sell=70, tp_pct=0.07, label="sell>70  TP=7%"),
        dict(rsi_sell=70, tp_pct=0.10, label="sell>70  TP=10%"),
        dict(rsi_sell=75, tp_pct=0.05, label="sell>75  TP=5%"),
        dict(rsi_sell=75, tp_pct=0.07, label="sell>75  TP=7%"),
        dict(rsi_sell=75, tp_pct=0.10, label="sell>75  TP=10%"),
    ]
    _mark_current(scenarios, tp_pct=config.SPOT_TAKE_PROFIT_PCT)
    _run_sweep(days_list, "Sell RSI + Take-profit sweep", scenarios, PAIRS)


def sweep_floor(days_list: list[int]):
    scenarios = [
        dict(floor_pct=0.01, label="floor=1%"),
        dict(floor_pct=0.02, label="floor=2%"),
        dict(floor_pct=0.03, label="floor=3%"),
        dict(floor_pct=0.04, label="floor=4%"),
        dict(floor_pct=0.05, label="floor=5%"),
    ]
    _mark_current(scenarios, floor_pct=config.SPOT_PROFIT_FLOOR_PCT)
    _run_sweep(days_list, "Profit floor sweep", scenarios, PAIRS)


def sweep_trail(days_list: list[int]):
    scenarios = [
        dict(trail_pct=0.02, label="trail=2%"),
        dict(trail_pct=0.03, label="trail=3%"),
        dict(trail_pct=0.04, label="trail=4%"),
        dict(trail_pct=0.05, label="trail=5%"),
        dict(trail_pct=0.06, label="trail=6%"),
        dict(trail_pct=0.07, label="trail=7%"),
    ]
    _mark_current(scenarios, trail_pct=config.SPOT_TRAILING_STOP_PCT)
    _run_sweep(days_list, "Trailing stop sweep", scenarios, PAIRS)


def sweep_buyrsi(days_list: list[int]):
    scenarios = [
        dict(rsi_buy=25, label="buy<25"),
        dict(rsi_buy=27, label="buy<27"),
        dict(rsi_buy=30, label="buy<30"),
        dict(rsi_buy=32, label="buy<32"),
        dict(rsi_buy=33, label="buy<33"),
        dict(rsi_buy=35, label="buy<35"),
    ]
    _mark_current(scenarios, rsi_buy=config.SPOT_RSI_OVERSOLD)
    _run_sweep(days_list, "Buy RSI threshold sweep", scenarios, PAIRS)


def sweep_min_exit(days_list: list[int]):
    scenarios = [
        dict(min_exit=0.000, label="min_exit=0%   (no gate)"),
        dict(min_exit=0.005, label="min_exit=0.5%"),
        dict(min_exit=0.010, label="min_exit=1%"),
        dict(min_exit=0.015, label="min_exit=1.5%"),
        dict(min_exit=0.020, label="min_exit=2%"),
        dict(min_exit=0.025, label="min_exit=2.5%"),
        dict(min_exit=0.030, label="min_exit=3%"),
        dict(min_exit=0.040, label="min_exit=4%"),
    ]
    _mark_current(scenarios, min_exit=config.SPOT_MIN_EXIT_PROFIT_PCT)
    _run_sweep(days_list, "Min exit profit sweep", scenarios, PAIRS)


def sweep_rsiperiod(days_list: list[int]):
    scenarios = [
        dict(rsi_period=5,  label="RSI(5)"),
        dict(rsi_period=6,  label="RSI(6)"),
        dict(rsi_period=7,  label="RSI(7)"),
        dict(rsi_period=8,  label="RSI(8)"),
        dict(rsi_period=9,  label="RSI(9)"),
        dict(rsi_period=10, label="RSI(10)"),
        dict(rsi_period=12, label="RSI(12)"),
        dict(rsi_period=14, label="RSI(14)"),
        dict(rsi_period=21, label="RSI(21)"),
    ]
    start = config.SPOT_SIMULATION_BALANCE
    for days in days_list:
        _header(days, "RSI period sweep", wide=True)
        print(f"  {'Scenario':<44}  {'n':>3}      {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
              f"  {'worst':>7}  {'best':>6}  fees   dca  exits")
        print(f"  {'─'*44}  {'─'*3}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*4}")
        for pair in PAIRS:
            df = fetch(pair, days)
            cur_period = config.rsi_period_for(pair)
            print(f"\n  ── {pair} (current: RSI({cur_period})) ──")
            for s in scenarios:
                label = s["label"] + ("  ◄ current" if s["rsi_period"] == cur_period else "")
                trades, _ = run_pair(pair, df, start, rsi_period=s["rsi_period"])
                summarise(label, trades, start, wide=True)
        print()


def sweep_timestop(days_list: list[int]):
    scenarios = [
        dict(time_stop_days=0,  label="no time stop  (current)"),
        dict(time_stop_days=14, label="time stop 14d"),
        dict(time_stop_days=21, label="time stop 21d"),
        dict(time_stop_days=30, label="time stop 30d"),
        dict(time_stop_days=45, label="time stop 45d"),
        dict(time_stop_days=60, label="time stop 60d"),
        dict(time_stop_days=90, label="time stop 90d"),
    ]
    _run_sweep(days_list, "Time-based stop sweep (close if no profit floor within N days)", scenarios, PAIRS)


def sweep_maxdrawdown(days_list: list[int]):
    scenarios = [
        dict(max_drawdown=0,    label="no drawdown guard  (current)"),
        dict(max_drawdown=0.10, label="pause buys >10% drawdown"),
        dict(max_drawdown=0.15, label="pause buys >15% drawdown"),
        dict(max_drawdown=0.20, label="pause buys >20% drawdown"),
        dict(max_drawdown=0.25, label="pause buys >25% drawdown"),
        dict(max_drawdown=0.30, label="pause buys >30% drawdown"),
    ]
    _run_sweep(days_list, "Max portfolio drawdown guard sweep", scenarios, PAIRS)


def sweep_ema_gap(days_list: list[int]):
    scenarios = [
        dict(ema_gap=0.00, label="gap=0%   (current)"),
        dict(ema_gap=0.01, label="gap=1%   above EMA"),
        dict(ema_gap=0.02, label="gap=2%   above EMA"),
        dict(ema_gap=0.03, label="gap=3%   above EMA"),
        dict(ema_gap=0.05, label="gap=5%   above EMA"),
        dict(ema_gap=0.07, label="gap=7%   above EMA"),
        dict(ema_gap=0.10, label="gap=10%  above EMA"),
        dict(ema_gap=0.15, label="gap=15%  above EMA"),
    ]
    _run_sweep(days_list, "EMA gap filter sweep (price must be X% above EMA200 to buy)", scenarios, PAIRS)



def sweep_volume(days_list: list[int]):
    scenarios = [
        dict(vol_period=0,  vol_mult=1.0, label="off  (baseline)"),
        dict(vol_period=10, vol_mult=1.0, label="vol > 10-bar avg"),
        dict(vol_period=20, vol_mult=1.0, label="vol > 20-bar avg"),
        dict(vol_period=30, vol_mult=1.0, label="vol > 30-bar avg"),
        dict(vol_period=20, vol_mult=1.5, label="vol > 1.5× 20-bar avg"),
        dict(vol_period=20, vol_mult=2.0, label="vol > 2.0× 20-bar avg"),
    ]
    _run_sweep(days_list, "Volume filter sweep (buy only on above-average volume)", scenarios, PAIRS)


def sweep_cooldown(days_list: list[int]):
    scenarios = [
        dict(stop_cooldown=0,   label="off  (baseline)"),
        dict(stop_cooldown=4,   label="cooldown=4 bars   (~1h)"),
        dict(stop_cooldown=8,   label="cooldown=8 bars   (~2h)"),
        dict(stop_cooldown=16,  label="cooldown=16 bars  (~4h)"),
        dict(stop_cooldown=32,  label="cooldown=32 bars  (~8h)"),
        dict(stop_cooldown=96,  label="cooldown=96 bars  (~1d)"),
        dict(stop_cooldown=192, label="cooldown=192 bars (~2d)"),
    ]
    _run_sweep(days_list, "Stop cooldown sweep (block re-entry N candles after trailing stop)", scenarios, PAIRS)


def sweep_divergence(days_list: list[int]):
    scenarios = [
        dict(div_lookback=0,   label="off  (baseline — any RSI<30 dip)"),
        dict(div_lookback=20,  label="div lookback=20 bars  (~5h)"),
        dict(div_lookback=40,  label="div lookback=40 bars  (~10h)"),
        dict(div_lookback=60,  label="div lookback=60 bars  (~15h)"),
        dict(div_lookback=80,  label="div lookback=80 bars  (~20h)"),
        dict(div_lookback=120, label="div lookback=120 bars (~30h)"),
        dict(div_lookback=192, label="div lookback=192 bars (~2d)"),
    ]
    _run_sweep(days_list, "RSI divergence sweep (price LL + RSI HL = stronger entry)", scenarios, PAIRS)


def sweep_htf_rsi(days_list: list[int]):
    start = config.SPOT_SIMULATION_BALANCE
    for days in days_list:
        _header(days, "Multi-timeframe RSI sweep (15m signal + HTF confirmation)", wide=True)
        print(f"  {'Scenario':<44}  {'n':>3}      {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
              f"  {'worst':>7}  {'best':>6}  fees   dca  exits")
        print(f"  {'─'*44}  {'─'*3}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*4}")
        for pair in PAIRS:
            df = fetch(pair, days)
            rp  = config.rsi_period_for(pair)
            print(f"\n  ── {pair} (RSI({rp})) ──")

            # baseline
            trades, _ = run_pair(pair, df, start, ema_gap=config.SPOT_EMA_GAP_PCT)
            summarise("no HTF filter  (current)", trades, start, wide=True)

            for htf_ivl in ["1h", "4h"]:
                htf_rsi = fetch_htf_rsi(pair, days, htf_ivl, rp)
                arr = align_htf(df, htf_rsi)
                for thresh in [30, 35, 40, 50]:
                    label = f"+ {htf_ivl} RSI({rp}) < {thresh}"
                    trades, _ = run_pair(pair, df, start,
                                         ema_gap=config.SPOT_EMA_GAP_PCT,
                                         htf_rsi_arr=arr,
                                         htf_rsi_threshold=thresh)
                    summarise(label, trades, start, wide=True)
        print()


def sweep_interval(days_list: list[int]):
    start = config.SPOT_SIMULATION_BALANCE
    intervals = ["15m", "1h", "4h"]
    for days in days_list:
        _header(days, "Candle interval sweep", wide=True)
        print(f"  {'Scenario':<44}  {'n':>3}      {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
              f"  {'worst':>7}  {'best':>6}  fees   dca  exits")
        print(f"  {'─'*44}  {'─'*3}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*4}")
        for pair in PAIRS:
            print(f"\n  ── {pair} ──")
            for ivl in intervals:
                df = fetch(pair, days, interval=ivl)
                # also test with RSI tuned to the interval (faster RSI on slower candles)
                rsi_p = config.rsi_period_for(pair)
                tag_cur = "  ◄ current" if ivl == INTERVAL else ""
                label = f"{ivl}  RSI({rsi_p}){tag_cur}"
                trades, _ = run_pair(pair, df, start,
                                     ema_gap=config.SPOT_EMA_GAP_PCT,
                                     interval=ivl)
                summarise(label, trades, start, wide=True)
                # also try RSI(14) on higher timeframes
                if ivl != INTERVAL:
                    for rp in ([14] if rsi_p != 14 else []):
                        trades, _ = run_pair(pair, df, start,
                                             ema_gap=config.SPOT_EMA_GAP_PCT,
                                             rsi_period=rp,
                                             interval=ivl)
                        summarise(f"{ivl}  RSI({rp})", trades, start, wide=True)
        print()


def sweep_daily_ema(days_list: list[int]):
    start = config.SPOT_SIMULATION_BALANCE
    ema_spans = [50, 100, 200]
    for days in days_list:
        _header(days, "Daily EMA filter sweep (secondary trend guard on 1d candles)", wide=True)
        print(f"  {'Scenario':<44}  {'n':>3}      {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
              f"  {'worst':>7}  {'best':>6}  fees   dca  exits")
        print(f"  {'─'*44}  {'─'*3}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*4}")
        for pair in PAIRS:
            df = fetch(pair, days)
            print(f"\n  ── {pair} ──")
            # baseline — no daily filter
            trades, _ = run_pair(pair, df, start, ema_gap=config.SPOT_EMA_GAP_PCT)
            summarise("no daily filter  (current)", trades, start, wide=True)
            for span in ema_spans:
                daily_ema = fetch_daily_ema(pair, days, ema_span=span)
                arr = align_daily_ema(df, daily_ema)
                label = f"+ daily EMA{span} filter"
                trades, _ = run_pair(pair, df, start, ema_gap=config.SPOT_EMA_GAP_PCT,
                                     daily_ema_arr=arr)
                summarise(label, trades, start, wide=True)
        print()


def run_pair_multipos(
    pair: str, df: pd.DataFrame, start_balance: float,
    max_slots:  int   = 3,
    pos_pct:    float = None,
    tp_pct:     float = None,
    trail_pct:  float = None,
    floor_pct:  float = None,
    rsi_buy:    int   = None,
    rsi_sell:   int   = None,
    rsi_period: int   = None,
    min_exit:   float = None,
    ema_gap:    float = 0.0,
) -> tuple[list[Trade], float]:
    """Like run_pair but allows up to max_slots independent open positions; no DCA."""
    fee_rate   = config.SPOT_FEE
    pos_pct    = pos_pct    if pos_pct    is not None else config.SPOT_POSITION_SIZE_PCT
    tp_pct     = tp_pct     if tp_pct     is not None else config.SPOT_TAKE_PROFIT_PCT
    trail_pct  = trail_pct  if trail_pct  is not None else config.SPOT_TRAILING_STOP_PCT
    floor_pct  = floor_pct  if floor_pct  is not None else config.SPOT_PROFIT_FLOOR_PCT
    rsi_buy    = rsi_buy    if rsi_buy    is not None else config.SPOT_RSI_OVERSOLD
    rsi_sell   = rsi_sell   if rsi_sell   is not None else config.rsi_overbought_for(pair)
    rsi_period = rsi_period if rsi_period is not None else config.rsi_period_for(pair)
    min_exit   = min_exit   if min_exit   is not None else config.SPOT_MIN_EXIT_PROFIT_PCT

    rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = precompute(df, rsi_period)

    balance:   float         = start_balance
    positions: list[Position] = []
    trades:    list[Trade]    = []

    for i in range(WARMUP, len(close_arr)):
        price = float(close_arr[i])
        rsi   = float(rsi_arr[i])
        ema   = float(ema_arr[i])
        hi    = float(hi_arr[i])
        lo    = float(lo_arr[i])

        still_open: list[Position] = []
        for pos in positions:
            update_peak(pos, hi)
            stop_level = max(
                pos.entry_price * (1 + fee_rate + floor_pct) / (1 - fee_rate),
                pos.peak() * (1 - trail_pct),
            )
            if hi >= pos.entry_price * (1 + tp_pct):
                ep = pos.entry_price * (1 + tp_pct)
                bf = pos.value_eur * fee_rate
                sf = pos.amount * ep * fee_rate
                balance += pos.amount * ep - sf
                trades.append(Trade(calc_pnl(pos, ep, bf, sf), bf + sf, "take_profit"))
                continue
            if pos.peak() > stop_level and lo <= stop_level:
                ep = stop_level
                bf = pos.value_eur * fee_rate
                sf = pos.amount * ep * fee_rate
                balance += pos.amount * ep - sf
                trades.append(Trade(calc_pnl(pos, ep, bf, sf), bf + sf, "trailing_stop"))
                continue
            if rsi > rsi_sell and price >= pos.entry_price * (1 + min_exit):
                bf = pos.value_eur * fee_rate
                sf = pos.amount * price * fee_rate
                balance += pos.amount * price - sf
                trades.append(Trade(calc_pnl(pos, price, bf, sf), bf + sf, "signal"))
                continue
            still_open.append(pos)
        positions = still_open

        if len(positions) < max_slots and rsi < rsi_buy and price > ema * (1 + ema_gap):
            max_size = balance / (1 + fee_rate)
            size = min(balance * pos_pct, max_size)
            if size >= 1:
                positions.append(Position(pair, price, size / price, size,
                                          price * (1 + tp_pct), price))
                balance -= size + size * fee_rate

    for pos in positions:
        price = float(close_arr[-1])
        bf = pos.value_eur * fee_rate
        sf = pos.amount * price * fee_rate
        balance += pos.amount * price - sf
        trades.append(Trade(calc_pnl(pos, price, bf, sf), bf + sf, "end_of_data"))

    return trades, balance


def sweep_multipos(days_list: list[int]):
    start = config.SPOT_SIMULATION_BALANCE
    half  = start / 2

    for days in days_list:
        _header(days, "Multi-position sweep (N independent slots per pair, no DCA within slots)", wide=True)
        print(f"  {'Scenario':<44}  {'n':>3}      {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
              f"  {'worst':>7}  {'best':>6}  fees   dca  exits")
        print(f"  {'─'*44}  {'─'*3}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*4}")

        dfs = {p: fetch(p, days) for p in ["ETHEUR", "SOLEUR"]}

        for pair in ["ETHEUR", "SOLEUR"]:
            df = dfs[pair]
            print(f"\n  ── {pair} (start=€{start:.0f}) ──")
            t, _ = run_pair(pair, df, start, max_dca=config.SPOT_DCA_MAX,
                            dca_step=config.SPOT_DCA_STEP_PCT)
            summarise(f"1 slot + {config.SPOT_DCA_MAX} DCA  (current)", t, start, wide=True)
            for slots in [1, 2, 3]:
                t, _ = run_pair_multipos(pair, df, start, max_slots=slots)
                label = f"{slots} slot{'s' if slots > 1 else ''}  no DCA"
                if slots == 1:
                    label += "  (baseline)"
                summarise(label, t, start, wide=True)

        print(f"\n  ── ETH+SOL combined (€{half:.0f} each) ──")
        eth_cur, _ = run_pair("ETHEUR", dfs["ETHEUR"], half,
                               max_dca=config.SPOT_DCA_MAX, dca_step=config.SPOT_DCA_STEP_PCT)
        sol_cur, _ = run_pair("SOLEUR", dfs["SOLEUR"], half,
                               max_dca=config.SPOT_DCA_MAX, dca_step=config.SPOT_DCA_STEP_PCT)
        summarise(f"1 slot + {config.SPOT_DCA_MAX} DCA  (current)", eth_cur + sol_cur, start, wide=True)
        for slots in [1, 2, 3]:
            eth_t, _ = run_pair_multipos("ETHEUR", dfs["ETHEUR"], half, max_slots=slots)
            sol_t, _ = run_pair_multipos("SOLEUR", dfs["SOLEUR"], half, max_slots=slots)
            label = f"{slots} slot{'s' if slots > 1 else ''}  no DCA"
            if slots == 1:
                label += "  (baseline)"
            summarise(label, eth_t + sol_t, start, wide=True)
        print()


def sweep_multidca(days_list: list[int]):
    scenarios = [
        dict(max_dca=0,                         label="no DCA"),
        dict(max_dca=1,                         label="1 DCA @ -1%        (current)"),
        dict(max_dca=2,                         label="2 DCAs @ -1% -2%"),
        dict(max_dca=3,                         label="3 DCAs @ -1% -2% -3%"),
        dict(max_dca=2, dca_step=0.02,          label="2 DCAs @ -1% -3%"),
        dict(max_dca=3, dca_step=0.02,          label="3 DCAs @ -1% -3% -5%"),
        dict(max_dca=2, dca_pct=0.50,           label="2 DCAs @ -1% -2%  size=50%"),
        dict(max_dca=3, dca_pct=0.33,           label="3 DCAs @ -1% -2% -3%  size=33%"),
    ]
    _run_sweep(days_list, "Multi-DCA sweep", scenarios, PAIRS)


def sweep_tieredsize(days_list: list[int]):
    # baseline: flat 75% always
    # tiers: full size (75%) only when price is X% above EMA, else reduced size Y%
    scenarios = [
        dict(label="flat 75%  (current)",                                          ),
        dict(label="strong≥5%→75%  weak→50%",  strong_gap=0.05, weak_pct=0.50),
        dict(label="strong≥5%→75%  weak→40%",  strong_gap=0.05, weak_pct=0.40),
        dict(label="strong≥5%→75%  weak→33%",  strong_gap=0.05, weak_pct=0.33),
        dict(label="strong≥7%→75%  weak→50%",  strong_gap=0.07, weak_pct=0.50),
        dict(label="strong≥7%→75%  weak→40%",  strong_gap=0.07, weak_pct=0.40),
        dict(label="strong≥10%→75% weak→50%",  strong_gap=0.10, weak_pct=0.50),
        dict(label="strong≥10%→75% weak→33%",  strong_gap=0.10, weak_pct=0.33),
        dict(label="flat 50%",                  pos_pct=0.50,   dca_pct=0.50),
    ]
    start = config.SPOT_SIMULATION_BALANCE
    for days in days_list:
        _header(days, "Tiered position size sweep (full size only when price well above EMA)", wide=True)
        print(f"  {'Scenario':<44}  {'n':>3}      {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
              f"  {'worst':>7}  {'best':>6}  fees   dca  exits")
        print(f"  {'─'*44}  {'─'*3}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*4}")
        for pair in PAIRS:
            df = fetch(pair, days)
            print(f"\n  ── {pair} ──")
            for s in scenarios:
                kwargs = {k: v for k, v in s.items() if k != "label"}
                kwargs.setdefault("ema_gap", config.SPOT_EMA_GAP_PCT)
                kwargs.setdefault("pos_pct", config.SPOT_POSITION_SIZE_PCT)
                kwargs.setdefault("dca_pct", config.SPOT_POSITION_SIZE_PCT)
                trades, _ = run_pair(pair, df, start, **kwargs)
                summarise(s["label"], trades, start, wide=True)
        print()


def sweep_hardstop(days_list: list[int]):
    scenarios = [
        dict(hard_stop=None,  label="no hard stop  (current)"),
        dict(hard_stop=0.10,  label="hard stop -10%"),
        dict(hard_stop=0.15,  label="hard stop -15%"),
        dict(hard_stop=0.20,  label="hard stop -20%"),
        dict(hard_stop=0.25,  label="hard stop -25%"),
        dict(hard_stop=0.30,  label="hard stop -30%"),
        dict(hard_stop=0.40,  label="hard stop -40%"),
    ]
    _run_sweep(days_list, "Hard stop loss sweep", scenarios, PAIRS)


def sweep_ema(days_list: list[int]):
    scenarios = [
        dict(ema_span=50,  label="EMA50"),
        dict(ema_span=100, label="EMA100"),
        dict(ema_span=150, label="EMA150"),
        dict(ema_span=200, label="EMA200  ◄ current"),
        dict(ema_span=250, label="EMA250"),
        dict(ema_span=300, label="EMA300"),
    ]
    _run_sweep(days_list, "EMA trend filter sweep", scenarios, PAIRS)


def sweep_dca(days_list: list[int]):
    start = config.SPOT_SIMULATION_BALANCE
    for days in days_list:
        _header(days, "DCA parameter sweep", wide=True)
        print(f"  {'Scenario':<44}  {'n':>3}      {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
              f"  {'worst':>7}  {'best':>6}  fees   dca  exits")
        print(f"  {'─'*44}  {'─'*3}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*4}")
        for pair in PAIRS:
            df = fetch(pair, days)
            print(f"\n  ── {pair} ──")
            base, _ = run_pair(pair, df, start, enable_dca=False)
            summarise("no DCA  (baseline)", base, start, wide=True)
            for drop in [0.01, 0.02, 0.03]:
                for size in [0.50, 0.75]:
                    cur = (abs(drop - config.SPOT_DCA_DROP_PCT) < 1e-9 and
                           abs(size - config.SPOT_DCA_SIZE_PCT) < 1e-9)
                    label = f"drop={drop*100:.0f}%  size={size*100:.0f}%{'  ◄ current' if cur else ''}"
                    trades, _ = run_pair(pair, df, start, dca_drop=drop, dca_pct=size)
                    summarise(label, trades, start, wide=True)
        print()


# ---------------------------------------------------------------------------
# Monthly top-up simulation
# ---------------------------------------------------------------------------

def run_topup(start: float, monthly: float, days: int):
    fee       = config.SPOT_FEE
    pos_pct   = config.SPOT_POSITION_SIZE_PCT
    tp_pct    = config.SPOT_TAKE_PROFIT_PCT
    trail_pct = config.SPOT_TRAILING_STOP_PCT
    dca_drop  = config.SPOT_DCA_DROP_PCT
    floor_pct = config.SPOT_PROFIT_FLOOR_PCT

    print(f"Loading candles…", flush=True)
    dfs = {p: fetch(p, days) for p in PAIRS}

    # Align candles to common timestamps
    common_idx = None
    for df in dfs.values():
        idx = set(df["open_time"].astype(str))
        common_idx = idx if common_idx is None else common_idx & idx
    aligned = {p: df[df["open_time"].astype(str).isin(common_idx)].reset_index(drop=True)
               for p, df in dfs.items()}
    min_len = min(len(df) for df in aligned.values())

    # Precompute RSI/EMA for each pair over the full dataset (same as run_pair)
    precomp = {p: precompute(aligned[p], config.rsi_period_for(p)) for p in PAIRS}
    print(f"Running {min_len - WARMUP:,} candles…", flush=True)

    balance    = start
    positions  = {}
    total_pnl  = total_fees = added = 0.0
    trades     = 0
    last_month = None
    monthly_log = []

    for i in range(WARMUP, min_len):
        ts        = aligned[PAIRS[0]].iloc[i]["open_time"].to_pydatetime()
        month_key = (ts.year, ts.month)
        if last_month is not None and month_key != last_month:
            balance += monthly
            added   += monthly
        last_month = month_key

        for pair in PAIRS:
            rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = precomp[pair]
            price = float(close_arr[i])
            hi    = float(hi_arr[i])
            lo    = float(lo_arr[i])
            rsi   = float(rsi_arr[i])
            ema   = float(ema_arr[i])
            pos   = positions.get(pair)

            if pos:
                update_peak(pos, hi)
                stop_level = max(
                    pos.entry_price * (1 + fee + floor_pct) / (1 - fee),
                    pos.peak() * (1 - trail_pct),
                )
                if hi >= pos.take_profit_price:
                    ep = pos.take_profit_price
                    bf = pos.value_eur * fee;  sf = pos.amount * ep * fee
                    balance += pos.amount * ep - sf
                    total_pnl += calc_pnl(pos, ep, bf, sf);  total_fees += bf + sf;  trades += 1
                    del positions[pair];  continue
                if pos.peak() > stop_level and lo <= stop_level:
                    ep = stop_level
                    bf = pos.value_eur * fee;  sf = pos.amount * ep * fee
                    balance += pos.amount * ep - sf
                    total_pnl += calc_pnl(pos, ep, bf, sf);  total_fees += bf + sf;  trades += 1
                    del positions[pair];  continue

            rsi_buy  = config.SPOT_RSI_OVERSOLD
            rsi_sell = config.rsi_overbought_for(pair)
            min_exit = config.min_exit_for(pair)

            if pair not in positions and rsi < rsi_buy and price > ema:
                size = balance * pos_pct
                if size >= 1:
                    bf = size * fee
                    pos = Position(pair, price, size / price, size, price * (1 + tp_pct), price)
                    positions[pair] = pos;  balance -= size + bf;  total_fees += bf

            if pair in positions:
                pos = positions[pair]
                if not pos.dca_count > 0 and (pos.entry_price - price) / pos.entry_price >= dca_drop:
                    dv = balance * pos_pct
                    if dv >= 1:
                        bf = dv * fee;  apply_dca(pos, price, dv)
                        balance -= dv + bf;  total_fees += bf

            if pair in positions and rsi > rsi_sell:
                pos = positions[pair]
                if price >= pos.entry_price * (1 + min_exit):
                    bf = pos.value_eur * fee;  sf = pos.amount * price * fee
                    balance += pos.amount * price - sf
                    total_pnl += calc_pnl(pos, price, bf, sf);  total_fees += bf + sf;  trades += 1
                    del positions[pair]

        pos_value = sum(p.amount * float(aligned[pr].iloc[i]["close"]) for pr, p in positions.items())
        monthly_log.append((ts, balance + pos_value))

    # Mark open positions to market at end — track separately from realized PnL
    open_pnl = 0.0
    open_pairs = []
    for pair, pos in list(positions.items()):
        price = float(aligned[pair].iloc[-1]["close"])
        bf = pos.value_eur * fee;  sf = pos.amount * price * fee
        balance += pos.amount * price - sf
        pnl = calc_pnl(pos, price, bf, sf)
        open_pnl   += pnl
        total_fees += bf + sf
        open_pairs.append(f"{pair} {pnl:+.2f}")

    realized_pnl  = total_pnl
    combined_pnl  = realized_pnl + open_pnl
    tax           = min(max(0, realized_pnl), 30000) * 0.30 + max(0, realized_pnl - 30000) * 0.34
    after_tax_pnl = realized_pnl - tax
    total_invested = start + added

    # --- pair performance table ---
    half = start / 2
    dfs_full = {p: fetch(p, days) for p in PAIRS}
    print(f"\nPair comparison — {days}d  (start=€{start:.0f})")
    print(f"  {'Scenario':<36}  {'n':>4}  {'open':>4}  {'W/L':<7}  {'PnL':>8}  {'after-tax':>10}  {'worst':>8}  fees")
    print(f"  {'─'*36}  {'─'*4}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*10}  {'─'*8}  {'─'*5}")
    eth_t, _ = run_pair("ETHEUR", dfs_full["ETHEUR"], start)
    sol_t, _ = run_pair("SOLEUR", dfs_full["SOLEUR"], start)
    eth2, _  = run_pair("ETHEUR", dfs_full["ETHEUR"], half)
    sol2, _  = run_pair("SOLEUR", dfs_full["SOLEUR"], half)
    summarise("ETHEUR only      [pos=75%]", eth_t,       start)
    summarise("SOLEUR only      [pos=75%]", sol_t,       start)
    summarise("ETH+SOL combined [pos=75%]", eth2 + sol2, start)

    print(f"\n{'═'*54}")
    print(f"  ETH + SOL  |  {days} days  |  +€{monthly:.0f}/month")
    print(f"{'═'*54}")
    print(f"  Total invested   : €{total_invested:.2f}  (€{start:.0f} start + €{added:.0f} added)")
    print(f"  Final balance    : €{balance:.2f}")
    print(f"  Realized PnL     : €{realized_pnl:+.2f}  ({trades} closed trades)")
    if open_pnl != 0:
        print(f"  Open pos. PnL    : €{open_pnl:+.2f}  ({', '.join(open_pairs)})")
    print(f"  Tax (FI 30%)     : €{tax:.2f}")
    print(f"  After-tax PnL    : €{after_tax_pnl:+.2f}")
    print(f"  After-tax bal    : €{start + added + after_tax_pnl:.2f}")
    print(f"  ROI on invested  : {(balance - total_invested) / total_invested * 100:+.1f}%")
    print(f"  Fees             : €{total_fees:.2f}")

    print(f"\n  Month-by-month portfolio value (cash + open positions):")
    print(f"  {'Month':<10}  {'Value':>8}  {'vs Invested':>12}")
    print(f"  {'─'*10}  {'─'*8}  {'─'*12}")
    seen = set();  cumulative = start
    for ts, val in monthly_log:
        key = (ts.year, ts.month)
        if key not in seen:
            seen.add(key)
            print(f"  {ts.strftime('%Y-%m'):<10}  €{val:>7.2f}  {val - cumulative:>+11.2f}")
            cumulative += monthly


# ---------------------------------------------------------------------------
# Default: pair comparison
# ---------------------------------------------------------------------------

def run_combined(days_list: list[int]):
    start = config.SPOT_SIMULATION_BALANCE
    half  = start / 2

    print(f"\nPair comparison — current settings")
    print(f"ETHEUR RSI({config.rsi_period_for('ETHEUR')}) sell>{config.rsi_overbought_for('ETHEUR')}"
          f"  SOLEUR RSI({config.rsi_period_for('SOLEUR')}) sell>{config.rsi_overbought_for('SOLEUR')}"
          f"  buy<{config.SPOT_RSI_OVERSOLD}  floor={config.SPOT_PROFIT_FLOOR_PCT*100:.0f}%"
          f"  pos={config.SPOT_POSITION_SIZE_PCT*100:.0f}%  DCA drop={config.SPOT_DCA_DROP_PCT*100:.0f}%")

    for days in days_list:
        _header(days, "solo vs combined")
        print(f"  {'Scenario':<36}  {'n':>4}  {'open':>4}  {'W/L':<7}  {'PnL':>8}  {'after-tax':>10}  {'worst':>8}  fees")
        print(f"  {'─'*36}  {'─'*4}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*10}  {'─'*8}  {'─'*5}")
        dfs = {p: fetch(p, days) for p in PAIRS}
        for pos_pct in [0.50, 0.75]:
            tag  = f"pos={int(pos_pct*100)}%"
            eth_t, _ = run_pair("ETHEUR", dfs["ETHEUR"], start, pos_pct=pos_pct, dca_pct=pos_pct)
            sol_t, _ = run_pair("SOLEUR", dfs["SOLEUR"], start, pos_pct=pos_pct, dca_pct=pos_pct)
            eth2, _  = run_pair("ETHEUR", dfs["ETHEUR"], half,  pos_pct=pos_pct, dca_pct=pos_pct)
            sol2, _  = run_pair("SOLEUR", dfs["SOLEUR"], half,  pos_pct=pos_pct, dca_pct=pos_pct)
            summarise(f"ETHEUR only      [{tag}]", eth_t, start)
            summarise(f"SOLEUR only      [{tag}]", sol_t, start)
            summarise(f"ETH+SOL combined [{tag}]", eth2 + sol2, start)
            print()


# ---------------------------------------------------------------------------
# SOLEUR-focused strategy comparison
# ---------------------------------------------------------------------------

def sweep_solfocus(days_list: list[int]):
    """Compare SOLEUR strategies: SPOT current, ACTIVE, and DCA variants tuned for SOL."""
    start = config.SPOT_SIMULATION_BALANCE
    half  = start / 2

    # SOLEUR-specific current params
    sol_rsi_period = config.rsi_period_for("SOLEUR")
    sol_rsi_ob     = config.rsi_overbought_for("SOLEUR")
    sol_min_exit   = config.SPOT_MIN_EXIT_PROFIT_PCT  # SOLEUR override

    scenarios = [
        # label, run_pair kwargs
        ("SPOT current",
         dict(rsi_period=sol_rsi_period, rsi_buy=config.SPOT_RSI_OVERSOLD, rsi_sell=sol_rsi_ob,
              floor_pct=config.SPOT_PROFIT_FLOOR_PCT, trail_pct=config.SPOT_TRAILING_STOP_PCT,
              min_exit=0.03, ema_gap=config.SPOT_EMA_GAP_PCT,
              max_dca=config.SPOT_DCA_MAX, dca_step=config.SPOT_DCA_STEP_PCT)),

        ("ACTIVE (no DCA)",
         dict(rsi_period=sol_rsi_period, rsi_buy=40, rsi_sell=58,
              floor_pct=0.003, trail_pct=0.012, min_exit=0.003, ema_gap=0.0,
              max_dca=0, enable_dca=False)),

        ("FREQ+DCA  rsi<38 trail=3% floor=1.5%",
         dict(rsi_period=sol_rsi_period, rsi_buy=38, rsi_sell=65,
              floor_pct=0.015, trail_pct=0.030, min_exit=0.010, ema_gap=0.0,
              max_dca=3, dca_step=config.SPOT_DCA_STEP_PCT)),

        ("ACTIVE+DCA  rsi<40 trail=2% floor=0.8%",
         dict(rsi_period=sol_rsi_period, rsi_buy=40, rsi_sell=62,
              floor_pct=0.008, trail_pct=0.020, min_exit=0.005, ema_gap=0.0,
              max_dca=3, dca_step=config.SPOT_DCA_STEP_PCT)),

        ("HYBRID  rsi<33 trail=3.5% floor=2% DCA×2",
         dict(rsi_period=sol_rsi_period, rsi_buy=33, rsi_sell=70,
              floor_pct=0.020, trail_pct=0.035, min_exit=0.015, ema_gap=0.0,
              max_dca=2, dca_step=config.SPOT_DCA_STEP_PCT)),
    ]

    for days in days_list:
        _header(days, "SOLEUR strategy focus — DCA variants", wide=True)
        print(f"  {'Scenario':<48}  {'n':>3}      {'W/L':<7}  {'PnL':>8}  {'avg':>6}"
              f"  {'worst':>7}  {'best':>6}  fees   dca  exits")
        print(f"  {'─'*48}  {'─'*3}  {'─'*4}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}  {'─'*5}  {'─'*4}")

        df_sol = fetch("SOLEUR", days)
        df_eth = fetch("ETHEUR", days)

        print(f"\n  ── SOLEUR only (€{start:.0f}) ──")
        for label, kwargs in scenarios:
            trades, _ = run_pair("SOLEUR", df_sol, start, **kwargs)
            summarise(label, trades, start, wide=True)

        print(f"\n  ── allocation: where to put €{start:.0f} ──")
        sol_full, _ = run_pair("SOLEUR", df_sol, start,
                               rsi_period=sol_rsi_period, rsi_buy=config.SPOT_RSI_OVERSOLD,
                               rsi_sell=sol_rsi_ob, floor_pct=config.SPOT_PROFIT_FLOOR_PCT,
                               trail_pct=config.SPOT_TRAILING_STOP_PCT, min_exit=0.03,
                               ema_gap=config.SPOT_EMA_GAP_PCT,
                               max_dca=config.SPOT_DCA_MAX, dca_step=config.SPOT_DCA_STEP_PCT)
        eth_half, _ = run_pair("ETHEUR", df_eth, half)
        sol_half, _ = run_pair("SOLEUR", df_sol, half,
                               rsi_period=sol_rsi_period, rsi_buy=config.SPOT_RSI_OVERSOLD,
                               rsi_sell=sol_rsi_ob, floor_pct=config.SPOT_PROFIT_FLOOR_PCT,
                               trail_pct=config.SPOT_TRAILING_STOP_PCT, min_exit=0.03,
                               ema_gap=config.SPOT_EMA_GAP_PCT,
                               max_dca=config.SPOT_DCA_MAX, dca_step=config.SPOT_DCA_STEP_PCT)
        eth_q, _  = run_pair("ETHEUR", df_eth, start * 0.25)
        sol_3q, _ = run_pair("SOLEUR", df_sol, start * 0.75,
                              rsi_period=sol_rsi_period, rsi_buy=config.SPOT_RSI_OVERSOLD,
                              rsi_sell=sol_rsi_ob, floor_pct=config.SPOT_PROFIT_FLOOR_PCT,
                              trail_pct=config.SPOT_TRAILING_STOP_PCT, min_exit=0.03,
                              ema_gap=config.SPOT_EMA_GAP_PCT,
                              max_dca=config.SPOT_DCA_MAX, dca_step=config.SPOT_DCA_STEP_PCT)
        summarise("100% SOL  (SPOT current params)", sol_full,          start, wide=True)
        summarise("50% ETH + 50% SOL  (current)",   eth_half + sol_half, start, wide=True)
        summarise("25% ETH + 75% SOL  (SOL primary)", eth_q + sol_3q,   start, wide=True)
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args     = sys.argv[1:]
    day_args = [int(a) for a in args if a.isdigit()]
    str_args = [a for a in args if not a.isdigit()]

    if str_args and str_args[0] == "sweep":
        mode      = str_args[1].lower() if len(str_args) > 1 else "all"
        days_list = day_args or [730, 365]
        sweeps = {
            "exit":     sweep_exit,
            "floor":    sweep_floor,
            "trail":    sweep_trail,
            "buyrsi":   sweep_buyrsi,
            "dca":      sweep_dca,
            "ema":      sweep_ema,
            "minexit":  sweep_min_exit,
            "rsiper":   sweep_rsiperiod,
            "hardstop": sweep_hardstop,
            "emagap":      sweep_ema_gap,
            "timestop":    sweep_timestop,
            "maxdrawdown": sweep_maxdrawdown,
            "tieredsize":  sweep_tieredsize,
            "multidca":    sweep_multidca,
            "dailyema":    sweep_daily_ema,
            "interval":    sweep_interval,
            "htfrsi":      sweep_htf_rsi,
            "volume":      sweep_volume,
            "cooldown":    sweep_cooldown,
            "divergence":  sweep_divergence,
            "multipos":    sweep_multipos,
            "solfocus":    sweep_solfocus,
        }
        if mode == "all":
            for fn in sweeps.values():
                fn(days_list)
        elif mode in sweeps:
            sweeps[mode](days_list)
        else:
            print(f"Unknown sweep '{mode}'. Options: {', '.join(sweeps)}, all")
            sys.exit(1)
    elif str_args and str_args[0] == "topup":
        # positional floats after "topup": start monthly days
        num_args = [float(a) for a in str_args[1:] if a.replace(".", "").isdigit()]
        num_args += [float(a) for a in args if a.isdigit()]
        start   = num_args[0] if len(num_args) > 0 else config.SPOT_SIMULATION_BALANCE
        monthly = num_args[1] if len(num_args) > 1 else 25.0
        days    = int(num_args[2]) if len(num_args) > 2 else 730
        run_topup(start, monthly, days)
    else:
        days_list = day_args or [730, 365]
        run_combined(days_list)
