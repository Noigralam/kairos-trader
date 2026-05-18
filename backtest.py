"""
Backtest the current strategy against historical 15m candles from Binance.

Usage:
    python backtest.py [days]          # default: 365
    python backtest.py 90 BTCEUR       # single pair, 90 days
"""
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from bot import config
from bot.strategy import compute_signal, Signal
from bot.candles import get_df, initial_sync
from bot.risk import (
    Position, create_position, apply_dca,
    update_peak, check_trailing_stop, check_take_profit, calc_pnl,
)

INTERVAL   = "15m"
CANDLES_PER_REQUEST = 1000
WARMUP     = 210          # candles needed before EMA200 is meaningful
COOLDOWN_CANDLES = 16     # 4h re-entry cooldown (16 × 15m)


# ── data fetching ─────────────────────────────────────────────────────────────

def fetch_historical(pair: str, days: int) -> pd.DataFrame:
    needed = days * 24 * 4 + WARMUP  # 15m candles per day + warmup
    df = get_df(pair, INTERVAL, limit=needed)
    if len(df) >= needed * 0.9:
        print(f"  {pair}: {len(df):,} candles from local DB")
        return df
    print(f"  {pair}: only {len(df)} candles locally — syncing {days}d from Binance…", flush=True)
    initial_sync(pair, INTERVAL, days=days)
    df = get_df(pair, INTERVAL, limit=needed)
    print(f"  {pair}: {len(df):,} candles ready")
    return df


# ── per-trade record ──────────────────────────────────────────────────────────

@dataclass
class Trade:
    pair:        str
    entry_time:  datetime
    exit_time:   datetime
    entry_price: float
    exit_price:  float
    amount:      float
    pnl:         float
    fees:        float
    exit_reason: str


# ── backtest engine ───────────────────────────────────────────────────────────

def run_pair(
    df: pd.DataFrame, pair: str, balance: float,
    tp_pct: float = None, trail_pct: float = None,
    cooldown: int = 0, profit_floor: float = 0.01,
    ema_period: int = 200,
) -> tuple[list[Trade], float]:
    fee_rate  = config.BINANCE_FEE
    pos_pct   = config.POSITION_SIZE_PCT
    tp_pct    = tp_pct    if tp_pct    is not None else config.TAKE_PROFIT_PCT
    trail_pct = trail_pct if trail_pct is not None else config.TRAILING_STOP_PCT
    dca_drop  = config.DCA_DROP_PCT
    min_exit  = config.MIN_EXIT_PROFIT_PCT

    position: Position | None = None
    trades: list[Trade]       = []
    cooldown_remaining        = 0

    # Skip the first ema_period+10 candles so the EMA has enough history to
    # produce a meaningful value — before this point it's calculated from too
    # few data points and would generate false signals.
    warmup = ema_period + 10
    for i in range(warmup, len(df)):
        window = df.iloc[: i + 1]
        row    = df.iloc[i]
        price  = float(row["close"])
        ts     = row["open_time"].to_pydatetime()

        if cooldown_remaining > 0:
            cooldown_remaining -= 1

        # stop checks first (use candle high/low for realism)
        if position is not None:
            hi = float(row["high"])
            lo = float(row["low"])
            update_peak(position, hi)

            # override trailing stop level using backtest trail_pct + profit_floor
            stop_level = max(
                position.entry_price * (1 + fee_rate + profit_floor) / (1 - fee_rate),
                position.peak() * (1 - trail_pct),
            )

            if check_take_profit(position, hi):
                exit_price = position.take_profit_price
                buy_fee    = position.value_eur * fee_rate
                sell_fee   = position.amount * exit_price * fee_rate
                pnl        = calc_pnl(position, exit_price, buy_fee, sell_fee)
                balance   += position.amount * exit_price - sell_fee
                trades.append(Trade(pair, position._entry_time, ts,
                                    position.entry_price, exit_price,
                                    position.amount, pnl, buy_fee + sell_fee, "take_profit"))
                position = None
                cooldown_remaining = cooldown
                continue

            if position.peak() > stop_level and lo <= stop_level:
                exit_price = stop_level
                buy_fee    = position.value_eur * fee_rate
                sell_fee   = position.amount * exit_price * fee_rate
                pnl        = calc_pnl(position, exit_price, buy_fee, sell_fee)
                balance   += position.amount * exit_price - sell_fee
                trades.append(Trade(pair, position._entry_time, ts,
                                    position.entry_price, exit_price,
                                    position.amount, pnl, buy_fee + sell_fee, "trailing_stop"))
                position = None
                cooldown_remaining = cooldown
                continue

        result = compute_signal(window, ema_trend=ema_period)

        if result.signal == Signal.BUY and position is None and cooldown_remaining == 0:
            size = balance * pos_pct
            if size < 1:
                continue
            amount    = size / price
            buy_fee   = size * fee_rate
            tp_price  = price * (1 + tp_pct)
            position  = Position(pair, price, amount, size, tp_price, price)
            position._entry_time = ts
            balance  -= size + buy_fee

        elif result.signal == Signal.BUY and position is not None:
            drop = (position.entry_price - price) / position.entry_price
            if not position.dca_done and drop >= dca_drop:
                dca_value = balance * pos_pct
                if dca_value >= 1:
                    buy_fee = dca_value * fee_rate
                    apply_dca(position, price, dca_value)
                    balance -= dca_value + buy_fee

        elif result.signal == Signal.SELL and position is not None:
            min_exit_price = position.entry_price * (1 + min_exit)
            if price >= min_exit_price:
                buy_fee    = position.value_eur * fee_rate
                sell_fee   = position.amount * price * fee_rate
                pnl        = calc_pnl(position, price, buy_fee, sell_fee)
                balance   += position.amount * price - sell_fee
                trades.append(Trade(pair, position._entry_time, ts,
                                    position.entry_price, price,
                                    position.amount, pnl, buy_fee + sell_fee, "signal"))
                position = None
                cooldown_remaining = cooldown

    # close any open position at last price
    if position is not None:
        price    = float(df.iloc[-1]["close"])
        buy_fee  = position.value_eur * fee_rate
        sell_fee = position.amount * price * fee_rate
        pnl      = calc_pnl(position, price, buy_fee, sell_fee)
        balance += position.amount * price - sell_fee
        trades.append(Trade(pair, position._entry_time, df.iloc[-1]["open_time"].to_pydatetime(),
                            position.entry_price, price,
                            position.amount, pnl, buy_fee + sell_fee, "end_of_data"))

    return trades, balance


# ── reporting ─────────────────────────────────────────────────────────────────

def report(pair: str, trades: list[Trade], start_balance: float, end_balance: float):
    print(f"\n{'─'*54}")
    print(f"  {pair}")
    print(f"{'─'*54}")
    if not trades:
        print("  No trades executed.")
        return

    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl  = sum(t.pnl  for t in trades)
    total_fees = sum(t.fees for t in trades)

    # max drawdown on cumulative pnl curve
    cumulative = 0.0
    peak_pnl   = 0.0
    max_dd     = 0.0
    for t in trades:
        cumulative += t.pnl
        if cumulative > peak_pnl:
            peak_pnl = cumulative
        dd = peak_pnl - cumulative
        if dd > max_dd:
            max_dd = dd

    avg_hold = sum(
        (t.exit_time - t.entry_time).total_seconds() / 3600
        for t in trades
    ) / len(trades)

    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    # Finnish capital gains tax: 30% up to €30k, 34% above
    taxable  = max(0.0, total_pnl)
    tax      = min(taxable, 30000) * 0.30 + max(0.0, taxable - 30000) * 0.34
    after_tax_pnl     = total_pnl - tax
    after_tax_balance = start_balance + after_tax_pnl

    print(f"  Trades        : {len(trades)}  (W {len(wins)} / L {len(losses)})  win rate {len(wins)/len(trades)*100:.0f}%")
    print(f"  Total PnL     : €{total_pnl:+.2f}  (after tax: €{after_tax_pnl:+.2f})")
    print(f"  Tax (FI)      : €{tax:.2f}  ({tax/taxable*100:.0f}% of gains)" if taxable > 0 else f"  Tax (FI)      : €0.00")
    print(f"  Fees paid     : €{total_fees:.4f}")
    print(f"  Max drawdown  : €{max_dd:.2f}")
    print(f"  Avg hold time : {avg_hold:.1f}h")
    print(f"  Best trade    : €{max(t.pnl for t in trades):+.2f}")
    print(f"  Worst trade   : €{min(t.pnl for t in trades):+.2f}")
    print(f"  Exit reasons  : {', '.join(f'{k} ×{v}' for k, v in reasons.items())}")
    print(f"  Balance       : €{start_balance:.2f} → €{end_balance:.2f}  ({(end_balance/start_balance-1)*100:+.1f}%)  after tax: €{after_tax_balance:.2f}")

    print(f"\n  {'Entry':19}  {'Exit':19}  {'PnL':>8}  Reason")
    print(f"  {'─'*19}  {'─'*19}  {'─'*8}  {'─'*13}")
    for t in trades[-20:]:   # last 20 trades
        entry = t.entry_time.strftime("%Y-%m-%d %H:%M")
        exit_ = t.exit_time.strftime("%Y-%m-%d %H:%M")
        print(f"  {entry}  {exit_}  €{t.pnl:>+7.2f}  {t.exit_reason}")
    if len(trades) > 20:
        print(f"  … {len(trades)-20} earlier trades omitted")


# ── main ──────────────────────────────────────────────────────────────────────

SCENARIOS = [
    {"label": "EMA200  floor=1.0%", "tp": 0.10, "trail": 0.05, "cooldown": 0, "floor": 0.010, "ema": 200},
    {"label": "EMA100  floor=1.0%", "tp": 0.10, "trail": 0.05, "cooldown": 0, "floor": 0.010, "ema": 100},
]

ALL_PAIRS = ["BTCEUR", "ETHEUR", "SOLEUR", "XRPEUR"]

if __name__ == "__main__":
    args  = sys.argv[1:]
    days  = int(args[0]) if args and args[0].isdigit() else 365
    pairs = [args[1].upper()] if len(args) > 1 else ALL_PAIRS

    print(f"\nBacktest  |  interval={INTERVAL}  days={days}  pairs={', '.join(pairs)}")
    print(f"Strategy  |  RSI(7) oversold<30 / overbought>65\n")

    # fetch data once per pair, reuse across scenarios
    data = {}
    for pair in pairs:
        data[pair] = fetch_historical(pair, days)

    start_balance = config.SIMULATION_BALANCE

    for scenario in SCENARIOS:
        label    = scenario["label"]
        tp       = scenario["tp"]
        trail    = scenario["trail"]
        cooldown = scenario["cooldown"]
        floor    = scenario["floor"]
        ema      = scenario["ema"]

        print(f"\n{'═'*54}")
        print(f"  SCENARIO: {label}  |  TP={tp*100:.0f}%  trail={trail*100:.0f}%")
        print(f"{'═'*54}")

        total_pnl = 0.0
        for pair in pairs:
            trades, end_balance = run_pair(data[pair], pair, start_balance,
                                           tp_pct=tp, trail_pct=trail, cooldown=cooldown,
                                           profit_floor=floor, ema_period=ema)
            report(pair, trades, start_balance, end_balance)
            total_pnl += end_balance - start_balance

        if len(pairs) > 1:
            taxable_combined = max(0.0, total_pnl)
            tax_combined     = min(taxable_combined, 30000) * 0.30 + max(0.0, taxable_combined - 30000) * 0.34
            after_tax        = total_pnl - tax_combined
            print(f"\n  Combined PnL : €{total_pnl:+.2f}  (after tax: €{after_tax:+.2f})")
            print(f"  Tax (FI)     : €{tax_combined:.2f}")
