"""
Compare running ETHEUR only, SOLEUR only, or both combined.
Uses current optimised settings (per-pair RSI period, floor=3%).

Usage:
    python backtest_combined.py [days]   # default: 365
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
        return df
    initial_sync(pair, INTERVAL, days=days)
    return get_df(pair, INTERVAL, limit=needed)


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


def run_pair(pair: str, df: pd.DataFrame, start_balance: float,
             pos_pct_override: float = None, dca_pct_override: float = None) -> tuple[list[Trade], float]:
    fee_rate  = config.BINANCE_FEE
    pos_pct   = pos_pct_override if pos_pct_override is not None else config.POSITION_SIZE_PCT
    dca_drop  = config.DCA_DROP_PCT
    dca_pct   = dca_pct_override if dca_pct_override is not None else config.DCA_SIZE_PCT
    tp_pct    = config.TAKE_PROFIT_PCT
    trail_pct = config.TRAILING_STOP_PCT
    floor_pct = config.PROFIT_FLOOR_PCT
    min_exit  = config.MIN_EXIT_PROFIT_PCT
    rsi_buy   = config.RSI_OVERSOLD
    rsi_sell  = config.RSI_OVERBOUGHT
    dca_rsi   = config.DCA_RSI_THRESHOLD
    rsi_p     = config.rsi_period_for(pair)

    rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = precompute(df, rsi_p)

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
                max_size = balance / (1 + fee_rate)
                dv = min(balance * dca_pct, max_size)
                if dv >= 1:
                    apply_dca(position, price, dv)
                    balance -= dv + dv * fee_rate

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


def summarise(label: str, trades: list[Trade], start: float, end: float):
    # exclude end_of_data — only count realized (closed) trades
    realized = [t for t in trades if t.exit_reason != "end_of_data"]
    open_eod = len(trades) - len(realized)

    pnl   = sum(t.pnl  for t in realized) if realized else 0
    fees  = sum(t.fees for t in realized) if realized else 0
    wins  = sum(1 for t in realized if t.pnl > 0)
    n     = len(realized)
    bal   = start + pnl
    tax   = min(max(pnl, 0), 30000) * 0.30 + max(max(pnl, 0) - 30000, 0) * 0.34
    after = bal - tax if pnl > 0 else bal
    worst = min((t.pnl for t in realized), default=0)
    reasons = {}
    for t in realized:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    r_str = "  ".join(f"{k}×{v}" for k, v in reasons.items())
    eod_str = f"+{open_eod}" if open_eod else ""
    print(f"  {label:<36}  n={n:>3}  {eod_str:>5}  W/L={wins}/{n-wins}"
          f"  PnL={pnl:>+7.2f}  after-tax={after:>7.2f}"
          f"  worst={worst:>+7.2f}  {fees:.2f}  [{r_str}]")


if __name__ == "__main__":
    args      = [a for a in sys.argv[1:] if a.isdigit()]
    days_list = [int(a) for a in args] or [365, 180]

    pairs = ["ETHEUR", "SOLEUR"]
    start = config.SIMULATION_BALANCE

    print(f"\nSolo vs combined — current settings")
    print(f"ETHEUR RSI({config.rsi_period_for('ETHEUR')})  SOLEUR RSI({config.rsi_period_for('SOLEUR')})"
          f"  buy<{config.RSI_OVERSOLD}  sell>{config.RSI_OVERBOUGHT}"
          f"  floor={config.PROFIT_FLOOR_PCT*100:.0f}%  pos={config.POSITION_SIZE_PCT*100:.0f}%\n")

    pos_sizes = [0.50, 0.75]

    for days in days_list:
        print(f"{'━'*100}")
        print(f"  {days}d window")
        print(f"{'━'*100}")
        print(f"  {'Scenario':<36}  {'n':>4}  {'open':>6}  {'W/L':<7}  {'PnL':>8}  {'after-tax':>10}  {'worst':>8}  fees")
        print(f"  {'─'*36}  {'─'*4}  {'─'*6}  {'─'*7}  {'─'*8}  {'─'*10}  {'─'*8}  {'─'*5}")

        dfs = {p: fetch(p, days) for p in pairs}

        for pos_pct in pos_sizes:
            tag  = f"pos={int(pos_pct*100)}%"
            half = start / 2
            eth_t,  _ = run_pair("ETHEUR", dfs["ETHEUR"], start, pos_pct, pos_pct)
            sol_t,  _ = run_pair("SOLEUR", dfs["SOLEUR"], start, pos_pct, pos_pct)
            eth_t2, _ = run_pair("ETHEUR", dfs["ETHEUR"], half,  pos_pct, pos_pct)
            sol_t2, _ = run_pair("SOLEUR", dfs["SOLEUR"], half,  pos_pct, pos_pct)
            summarise(f"ETHEUR only      [{tag}]", eth_t,          start, start)
            summarise(f"SOLEUR only      [{tag}]", sol_t,          start, start)
            summarise(f"ETH+SOL combined [{tag}]", eth_t2 + sol_t2, start, start)
            print()
