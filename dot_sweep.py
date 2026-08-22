"""Full parameter sweep for DOTEUR."""
import sys
from dotenv import load_dotenv
load_dotenv()

import backtest as bt

bt.PAIRS = ["DOTEUR"]

days_list = [730, 365, 180]

if "--cached" in sys.argv:
    bt.USE_CACHE = True
else:
    print("Syncing DOTEUR candles…", flush=True)
    from bot.candles import initial_sync
    initial_sync("DOTEUR", bt.INTERVAL, days=730)
    bt.USE_CACHE = True

sweeps = [
    ("Buy RSI threshold",   bt.sweep_buyrsi),
    ("RSI period",          bt.sweep_rsiperiod),
    ("Sell RSI + TP",       bt.sweep_exit),
    ("Profit floor",        bt.sweep_floor),
    ("Trailing stop",       bt.sweep_trail),
    ("Min exit profit",     bt.sweep_min_exit),
    ("DCA parameters",      bt.sweep_dca),
    ("EMA gap filter",      bt.sweep_ema_gap),
    ("Cooldown",            bt.sweep_cooldown),
]

for name, fn in sweeps:
    print(f"\n{'═'*70}")
    print(f"  DOTEUR  ·  {name}")
    print(f"{'═'*70}")
    fn(days_list)
