"""
Full parameter sweep for XRPEUR.

Runs: exit, floor, trail, buyrsi, min_exit, rsiperiod, dca, emagap
across 730d / 365d / 180d windows.
"""
import sys
from dotenv import load_dotenv
load_dotenv()

import backtest as bt

bt.PAIRS = ["XRPEUR"]

days_list = [730, 365, 180]

if "--cached" in sys.argv:
    bt.USE_CACHE = True
else:
    # Sync candles once for all windows
    print("Syncing XRPEUR candles…", flush=True)
    from bot.candles import initial_sync
    initial_sync("XRPEUR", bt.INTERVAL, days=730)
    bt.USE_CACHE = True

sweeps = [
    ("Buy RSI threshold",     bt.sweep_buyrsi),
    ("RSI period",            bt.sweep_rsiperiod),
    ("Sell RSI + TP",         bt.sweep_exit),
    ("Profit floor",          bt.sweep_floor),
    ("Trailing stop",         bt.sweep_trail),
    ("Min exit profit",       bt.sweep_min_exit),
    ("DCA parameters",        bt.sweep_dca),
    ("EMA gap filter",        bt.sweep_ema_gap),
]

for name, fn in sweeps:
    print(f"\n{'═'*70}")
    print(f"  XRPEUR  ·  {name}")
    print(f"{'═'*70}")
    fn(days_list)
