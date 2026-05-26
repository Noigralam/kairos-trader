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

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from bot import config
from bot.candles import get_df, initial_sync
from bot.risk import Position, apply_dca, update_peak, calc_pnl

INTERVAL = "15m"
WARMUP   = 210
PAIRS    = ["ETHEUR", "SOLEUR"]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def fetch(pair: str, days: int) -> pd.DataFrame:
    needed = days * 24 * 4 + WARMUP
    df = get_df(pair, INTERVAL, limit=needed)
    if len(df) >= needed * 0.9:
        return df
    print(f"  Syncing {pair}/{INTERVAL} {days}d…", flush=True)
    initial_sync(pair, INTERVAL, days=days)
    return get_df(pair, INTERVAL, limit=needed)


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
    dca_rsi:    int   = None,
    enable_dca: bool  = True,
    tp_pct:     float = None,
    trail_pct:  float = None,
    floor_pct:  float = None,
    rsi_buy:    int   = None,
    rsi_sell:   int   = None,
    rsi_period: int   = None,
    ema_span:   int   = 200,
    min_exit:   float = None,
) -> tuple[list[Trade], float]:

    fee_rate   = config.BINANCE_FEE
    pos_pct    = pos_pct    if pos_pct    is not None else config.POSITION_SIZE_PCT
    dca_drop   = dca_drop   if dca_drop   is not None else config.DCA_DROP_PCT
    dca_pct    = dca_pct    if dca_pct    is not None else config.DCA_SIZE_PCT
    dca_rsi    = dca_rsi    if dca_rsi    is not None else config.DCA_RSI_THRESHOLD
    tp_pct     = tp_pct     if tp_pct     is not None else config.TAKE_PROFIT_PCT
    trail_pct  = trail_pct  if trail_pct  is not None else config.TRAILING_STOP_PCT
    floor_pct  = floor_pct  if floor_pct  is not None else config.PROFIT_FLOOR_PCT
    rsi_buy    = rsi_buy    if rsi_buy    is not None else config.RSI_OVERSOLD
    rsi_sell   = rsi_sell   if rsi_sell   is not None else config.rsi_overbought_for(pair)
    rsi_period = rsi_period if rsi_period is not None else config.rsi_period_for(pair)
    min_exit   = min_exit   if min_exit   is not None else config.MIN_EXIT_PROFIT_PCT

    rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = precompute(df, rsi_period, ema_span)

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
    start = config.SIMULATION_BALANCE
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
    _mark_current(scenarios, tp_pct=config.TAKE_PROFIT_PCT)
    _run_sweep(days_list, "Sell RSI + Take-profit sweep", scenarios, PAIRS)


def sweep_floor(days_list: list[int]):
    scenarios = [
        dict(floor_pct=0.01, label="floor=1%"),
        dict(floor_pct=0.02, label="floor=2%"),
        dict(floor_pct=0.03, label="floor=3%"),
        dict(floor_pct=0.04, label="floor=4%"),
        dict(floor_pct=0.05, label="floor=5%"),
    ]
    _mark_current(scenarios, floor_pct=config.PROFIT_FLOOR_PCT)
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
    _mark_current(scenarios, trail_pct=config.TRAILING_STOP_PCT)
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
    _mark_current(scenarios, rsi_buy=config.RSI_OVERSOLD)
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
    _mark_current(scenarios, min_exit=config.MIN_EXIT_PROFIT_PCT)
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
    start = config.SIMULATION_BALANCE
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
    start = config.SIMULATION_BALANCE
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
                    cur = (abs(drop - config.DCA_DROP_PCT) < 1e-9 and
                           abs(size - config.DCA_SIZE_PCT) < 1e-9)
                    label = f"drop={drop*100:.0f}%  size={size*100:.0f}%{'  ◄ current' if cur else ''}"
                    trades, _ = run_pair(pair, df, start, dca_drop=drop, dca_pct=size)
                    summarise(label, trades, start, wide=True)
        print()


# ---------------------------------------------------------------------------
# Monthly top-up simulation
# ---------------------------------------------------------------------------

def run_topup(start: float, monthly: float, days: int):
    from bot.strategy import compute_signal, Signal

    fee       = config.BINANCE_FEE
    pos_pct   = config.POSITION_SIZE_PCT
    tp_pct    = config.TAKE_PROFIT_PCT
    trail_pct = config.TRAILING_STOP_PCT
    dca_drop  = config.DCA_DROP_PCT
    floor_pct = config.PROFIT_FLOOR_PCT
    min_exit  = config.MIN_EXIT_PROFIT_PCT

    print(f"Loading candles…", flush=True)
    dfs = {p: fetch(p, days) for p in PAIRS}

    common_idx = None
    for df in dfs.values():
        idx = set(df["open_time"].astype(str))
        common_idx = idx if common_idx is None else common_idx & idx
    aligned = {p: df[df["open_time"].astype(str).isin(common_idx)].reset_index(drop=True)
               for p, df in dfs.items()}
    min_len = min(len(df) for df in aligned.values())
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
            row   = aligned[pair].iloc[i]
            price = float(row["close"])
            hi    = float(row["high"])
            lo    = float(row["low"])
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

            result = compute_signal(aligned[pair].iloc[:i+1],
                                    rsi_period=config.rsi_period_for(pair),
                                    rsi_oversold=config.RSI_OVERSOLD,
                                    rsi_overbought=config.rsi_overbought_for(pair))

            if result.signal == Signal.BUY and pair not in positions:
                size = balance * pos_pct
                if size >= 1:
                    bf = size * fee
                    pos = Position(pair, price, size / price, size, price * (1 + tp_pct), price)
                    positions[pair] = pos;  balance -= size + bf;  total_fees += bf

            elif result.signal == Signal.BUY and pair in positions:
                pos = positions[pair]
                if not pos.dca_done and (pos.entry_price - price) / pos.entry_price >= dca_drop:
                    dv = balance * pos_pct
                    if dv >= 1:
                        bf = dv * fee;  apply_dca(pos, price, dv)
                        balance -= dv + bf;  total_fees += bf

            elif result.signal == Signal.SELL and pair in positions:
                pos = positions[pair]
                if price >= pos.entry_price * (1 + min_exit):
                    bf = pos.value_eur * fee;  sf = pos.amount * price * fee
                    balance += pos.amount * price - sf
                    total_pnl += calc_pnl(pos, price, bf, sf);  total_fees += bf + sf;  trades += 1
                    del positions[pair]

        pos_value = sum(p.amount * float(aligned[pr].iloc[i]["close"]) for pr, p in positions.items())
        monthly_log.append((ts, balance + pos_value))

    for pair, pos in list(positions.items()):
        price = float(aligned[pair].iloc[-1]["close"])
        bf = pos.value_eur * fee;  sf = pos.amount * price * fee
        balance += pos.amount * price - sf
        total_pnl += calc_pnl(pos, price, bf, sf);  total_fees += bf + sf

    tax           = min(max(0, total_pnl), 30000) * 0.30 + max(0, total_pnl - 30000) * 0.34
    after_tax_pnl = total_pnl - tax
    total_invested = start + added

    print(f"\n{'═'*54}")
    print(f"  ETH + SOL  |  {days} days  |  +€{monthly:.0f}/month")
    print(f"{'═'*54}")
    print(f"  Total invested : €{total_invested:.2f}  (€{start:.0f} start + €{added:.0f} added)")
    print(f"  Final balance  : €{balance:.2f}")
    print(f"  Trading PnL    : €{total_pnl:+.2f}")
    print(f"  Tax (FI 30%)   : €{tax:.2f}")
    print(f"  After-tax PnL  : €{after_tax_pnl:+.2f}")
    print(f"  After-tax bal  : €{start + added + after_tax_pnl:.2f}")
    print(f"  ROI on invested: {(balance - total_invested) / total_invested * 100:+.1f}%")
    print(f"  Trades         : {trades}  |  Fees: €{total_fees:.2f}")

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
    start = config.SIMULATION_BALANCE
    half  = start / 2

    print(f"\nPair comparison — current settings")
    print(f"ETHEUR RSI({config.rsi_period_for('ETHEUR')}) sell>{config.rsi_overbought_for('ETHEUR')}"
          f"  SOLEUR RSI({config.rsi_period_for('SOLEUR')}) sell>{config.rsi_overbought_for('SOLEUR')}"
          f"  buy<{config.RSI_OVERSOLD}  floor={config.PROFIT_FLOOR_PCT*100:.0f}%"
          f"  pos={config.POSITION_SIZE_PCT*100:.0f}%  DCA drop={config.DCA_DROP_PCT*100:.0f}%")

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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args     = sys.argv[1:]
    day_args = [int(a) for a in args if a.isdigit()]
    str_args = [a for a in args if not a.isdigit()]

    if str_args and str_args[0] == "sweep":
        mode      = str_args[1].lower() if len(str_args) > 1 else "all"
        days_list = day_args or [365, 180]
        sweeps = {
            "exit":    sweep_exit,
            "floor":   sweep_floor,
            "trail":   sweep_trail,
            "buyrsi":  sweep_buyrsi,
            "dca":     sweep_dca,
            "ema":     sweep_ema,
            "minexit": sweep_min_exit,
            "rsiper":  sweep_rsiperiod,
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
        start   = num_args[0] if len(num_args) > 0 else config.SIMULATION_BALANCE
        monthly = num_args[1] if len(num_args) > 1 else 25.0
        days    = int(num_args[2]) if len(num_args) > 2 else 365
        run_topup(start, monthly, days)
    else:
        days_list = day_args or [365, 180]
        run_combined(days_list)
