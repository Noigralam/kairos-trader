"""
Grid strategy backtest sweep.

Usage:
    python backtest_grid.py [pair] [days]
    python backtest_grid.py XRPEUR 180
    python backtest_grid.py XRPEUR 180 --cached

Sweeps spacing × levels and prints a ranked results table.
"""
import sys
import itertools

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from bot.candles import get_df, initial_sync

FEE        = 0.001   # 0.1% per side
WARMUP     = 0       # grid needs no indicator warmup
BALANCE    = 200.0
USE_CACHE  = "--cached" in sys.argv

CANDLES_PER_DAY = {"1m": 1440, "5m": 288, "15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1}


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch(pair: str, days: int, interval: str = "15m") -> pd.DataFrame:
    cpd    = CANDLES_PER_DAY.get(interval, 96)
    needed = days * cpd + WARMUP
    df = get_df(pair, interval, limit=needed)
    if USE_CACHE or len(df) >= needed * 0.9:
        return df
    print(f"  Syncing {pair}/{interval} {days}d…", flush=True)
    initial_sync(pair, interval, days=days)
    return get_df(pair, interval, limit=needed)


# ---------------------------------------------------------------------------
# Grid simulator
# ---------------------------------------------------------------------------

def run_grid(
    closes:  np.ndarray,
    highs:   np.ndarray,
    lows:    np.ndarray,
    times:   np.ndarray,
    spacing: float,
    levels:  int,
    balance: float = BALANCE,
) -> dict:
    """
    Simulate a grid strategy on pre-fetched candle arrays.

    Each level gets balance/levels EUR. On init the grid is centered on the
    first candle close. Each level has a buy_price (center*(1-i*spacing)) and
    sell_price (buy_price*(1+spacing)). Per candle:
      - sells fire if high >= sell_price of a filled slot
      - buys  fire if low  <= buy_price  of an empty slot
    Grid re-centers when ALL slots are empty and price > top_buy*(1+2*spacing).

    Returns a result dict with summary stats.
    """
    n          = len(closes)
    slot_eur   = balance / levels
    cash       = balance
    total_pnl  = 0.0
    total_fees = 0.0
    trades     = 0
    stuck_candles = 0     # candles where all slots are filled (unrealised loss risk)
    recenters  = 0

    # slots: list of dicts
    def init_slots(center: float) -> list:
        slots = []
        for i in range(levels):
            buy  = center * (1 - (i + 1) * spacing)
            sell = buy * (1 + spacing)
            slots.append({"buy": buy, "sell": sell,
                           "filled": False, "amount": 0.0, "cost": 0.0})
        return slots

    slots       = init_slots(closes[0])
    peak_port   = balance
    max_dd      = 0.0
    total_cycles = [0] * levels   # how many round-trips per level

    for i in range(n):
        high  = highs[i]
        low   = lows[i]
        close = closes[i]

        # --- sells first ---
        for li, s in enumerate(slots):
            if s["filled"] and high >= s["sell"]:
                sell_val  = s["amount"] * s["sell"]
                sell_fee  = sell_val  * FEE
                buy_fee   = s["cost"] * FEE
                pnl       = sell_val - s["cost"] - buy_fee - sell_fee
                cash             += sell_val - sell_fee
                total_pnl        += pnl
                total_fees       += sell_fee + buy_fee
                trades           += 1
                total_cycles[li] += 1
                s["filled"] = False
                s["amount"] = 0.0
                s["cost"]   = 0.0

        # --- buys ---
        for s in slots:
            if not s["filled"] and low <= s["buy"]:
                if cash >= slot_eur * (1 + FEE):
                    fee       = slot_eur * FEE
                    s["filled"] = True
                    s["amount"] = slot_eur / s["buy"]
                    s["cost"]   = slot_eur
                    cash       -= slot_eur + fee
                    total_fees += fee

        # --- recenter if all empty and price rallied above grid ---
        filled_count = sum(1 for s in slots if s["filled"])
        if filled_count == 0 and close > slots[0]["buy"] * (1 + spacing * 2):
            slots    = init_slots(close)
            recenters += 1

        # --- track stuck candles (all slots filled) ---
        if filled_count == levels:
            stuck_candles += 1

        # --- drawdown tracking ---
        held_val = sum(s["amount"] * close for s in slots if s["filled"])
        port_val = cash + held_val
        if port_val > peak_port:
            peak_port = port_val
        dd = (peak_port - port_val) / peak_port
        if dd > max_dd:
            max_dd = dd

    # --- final portfolio value (unrealised) ---
    held_val   = sum(s["amount"] * closes[-1] for s in slots if s["filled"])
    final_port = cash + held_val
    open_slots = sum(1 for s in slots if s["filled"])

    return {
        "spacing":       spacing,
        "levels":        levels,
        "trades":        trades,
        "total_pnl":     round(total_pnl, 4),
        "total_fees":    round(total_fees, 4),
        "final_port":    round(final_port, 4),
        "return_pct":    round((final_port - balance) / balance * 100, 2),
        "realised_pct":  round(total_pnl / balance * 100, 2),
        "max_dd_pct":    round(max_dd * 100, 2),
        "stuck_pct":     round(stuck_candles / n * 100, 1),
        "recenters":     recenters,
        "open_slots":    open_slots,
        "cycle_counts":  total_cycles,
    }


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

SPACINGS = [0.005, 0.008, 0.010, 0.012, 0.015, 0.020, 0.025, 0.030, 0.040, 0.050]
LEVELS   = [4, 6, 8, 10, 12, 16, 20]


def sweep(pair: str, df: pd.DataFrame):
    closes = df["close"].values.astype(float)
    highs  = df["high"].values.astype(float)
    lows   = df["low"].values.astype(float)
    times  = df["open_time"].values

    results = []
    combos  = list(itertools.product(SPACINGS, LEVELS))
    total   = len(combos)
    for idx, (sp, lv) in enumerate(combos, 1):
        r = run_grid(closes, highs, lows, times, sp, lv)
        results.append(r)
        print(f"\r  Progress {idx}/{total}", end="", flush=True)
    print()

    return results


def print_table(results: list, sort_by: str = "realised_pct", top_n: int = 20):
    df = pd.DataFrame(results).sort_values(sort_by, ascending=False)

    print(f"\n{'spacing':>8} {'levels':>6} {'trades':>7} {'realised%':>10} "
          f"{'return%':>9} {'max_dd%':>8} {'stuck%':>7} {'recenters':>10} "
          f"{'open_slots':>11} {'fees':>7}")
    print("-" * 95)
    for _, r in df.head(top_n).iterrows():
        sp_str = f"{r.spacing*100:.1f}%"
        print(f"{sp_str:>8} {int(r.levels):>6} {int(r.trades):>7} "
              f"{r.realised_pct:>+10.2f}% {r.return_pct:>+8.2f}% "
              f"{r.max_dd_pct:>7.1f}% {r.stuck_pct:>6.1f}% {int(r.recenters):>10} "
              f"{int(r.open_slots):>11} €{r.total_fees:>6.2f}")

    print(f"\n(sorted by realised P&L%, top {top_n} of {len(results)})")
    return df


def analyse_volatility(df: pd.DataFrame, pair: str):
    """Print candle range stats to help interpret spacing choices."""
    ranges = (df["high"] - df["low"]) / df["close"] * 100
    print(f"\n{pair} candle range (high-low / close) on 15m:")
    print(f"  median:  {ranges.median():.3f}%")
    print(f"  mean:    {ranges.mean():.3f}%")
    print(f"  p75:     {ranges.quantile(0.75):.3f}%")
    print(f"  p90:     {ranges.quantile(0.90):.3f}%")
    print(f"  p95:     {ranges.quantile(0.95):.3f}%")
    print(f"  (fee floor to profit = {FEE*2*100:.2f}% per round-trip)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pair = args[0].upper() if args else "XRPEUR"
    days = int(args[1]) if len(args) > 1 else 180

    print(f"\nGrid backtest sweep — {pair}  {days}d  balance=€{BALANCE}")
    print(f"Spacing values: {[f'{s*100:.1f}%' for s in SPACINGS]}")
    print(f"Level  values:  {LEVELS}")
    print(f"Combinations:   {len(SPACINGS) * len(LEVELS)}")

    df = fetch(pair, days)
    if df.empty:
        print("No candle data — run without --cached first.")
        sys.exit(1)

    print(f"Candles loaded: {len(df)}  ({df['open_time'].iloc[0]} → {df['open_time'].iloc[-1]})")
    analyse_volatility(df, pair)

    print(f"\nRunning {len(SPACINGS) * len(LEVELS)} simulations…")
    results = sweep(pair, df)

    ranked = print_table(results, sort_by="realised_pct")

    # Also show best by return% (includes open positions)
    print("\n--- Top 10 by total return% (including open unrealised) ---")
    print_table(results, sort_by="return_pct", top_n=10)

    # Best single spacing across all levels
    print("\n--- Best spacing (avg realised% across all level counts) ---")
    by_sp = pd.DataFrame(results).groupby("spacing")["realised_pct"].mean().sort_values(ascending=False)
    for sp, val in by_sp.items():
        print(f"  {sp*100:.1f}%  →  avg realised {val:+.2f}%")
