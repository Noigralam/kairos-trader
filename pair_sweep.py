"""
Full parameter sweep for a single spot pair.

Usage:
    python pair_sweep.py XRPEUR [--cached]
    python pair_sweep.py DOTEUR [--cached]
    python pair_sweep.py SOLEUR 365 [--cached]

Runs all single-axis sweeps across 730d / 365d / 180d windows (or custom days).
"""
import sys
from dotenv import load_dotenv
load_dotenv()

import backtest as bt

args     = sys.argv[1:]
cached   = "--cached" in args
args     = [a for a in args if a != "--cached"]
pair_arg = next((a for a in args if not a.isdigit()), None)
day_args = [int(a) for a in args if a.isdigit()]

if not pair_arg:
    print("Usage: python pair_sweep.py PAIR [days] [--cached]")
    print("Example: python pair_sweep.py XRPEUR 365 --cached")
    sys.exit(1)

pair      = pair_arg.upper()
days_list = day_args or [730, 365, 180]

bt.PAIRS = [pair]

if cached:
    bt.USE_CACHE = True
else:
    print(f"Syncing {pair} candles…", flush=True)
    from bot.candles import initial_sync
    initial_sync(pair, bt.INTERVAL, days=max(days_list))
    bt.USE_CACHE = True

sweeps = [
    ("Buy RSI threshold",      bt.sweep_buyrsi),
    ("RSI period",             bt.sweep_rsiperiod),
    ("Sell RSI + TP",          bt.sweep_exit),
    ("Profit floor",           bt.sweep_floor),
    ("Trailing stop",          bt.sweep_trail),
    ("Min exit profit",        bt.sweep_min_exit),
    ("Position size",          bt.sweep_posize),
    ("DCA parameters",         bt.sweep_dca),
    ("DCA max tranches",       bt.sweep_maxdca),
    ("DCA step %",             bt.sweep_dcastep),
    ("EMA gap filter",         bt.sweep_ema_gap),
    ("Hard stop",              bt.sweep_hardstop),
    ("Time stop",              bt.sweep_timestop),
    ("Stop cooldown",          bt.sweep_cooldown),
    ("Partial close",          bt.sweep_partialclose),
]

for name, fn in sweeps:
    print(f"\n{'═'*70}")
    print(f"  {pair}  ·  {name}")
    print(f"{'═'*70}")
    fn(days_list)
