# Backtest Sweep Examples

How to use the backtest tools to find good parameters, illustrated with real examples from the currently running shadow profiles.

Add `--cached` to any command after the first run to skip candle syncing and use the local cache — much faster for iterating on the same window.

Every run saves a full copy of its output to `backtest_results/` with a filename that describes what was run and when (e.g. `spot_sweep_rsi_buy_730d_365d_2026-08-27.txt`). The path is printed at the top of each run. Each sweep also includes a footer legend explaining every column.

Sweep values for each axis are defined in `sweep_ranges.toml` — edit that file and changes take effect on the next run.

---

## Example 1 — Evaluating a new pair: NEAR2

NEAR was shortlisted as a candidate live pair. First step: run the baseline backtest to see if the default settings even work on NEAREUR.

```bash
# Baseline: does NEAR respond to the strategy at all? (uses global defaults: RSI 14, buy<30, sell>75)
.venv/bin/python pair_sweep.py NEAREUR 730

# Same thing but skip re-syncing if you already have candles
.venv/bin/python pair_sweep.py NEAREUR 730 --cached
```

The baseline (RSI 14) was weak — roughly +€55 over 730 days. `pair_sweep.py` runs all sweeps in sequence, so you get RSI period, buy threshold, exit, trail, floor, DCA, and EMA gap in one shot. The RSI period sweep showed RSI(7) dominated. Then:

```bash
# Confirm RSI(7) over multiple windows with a focussed buy-threshold sweep
.venv/bin/python backtest.py sweep rsi_buy 730 365 180 --cached
```

RSI period=7 with buy<30 and sell>75 came out significantly ahead (+€272 vs +€55 over 730d). That became **NEAR2**:

```
SPOT_SHADOW_NEAR2_PAIRS=NEAREUR
SPOT_SHADOW_NEAR2_RSI_PERIOD=7
SPOT_SHADOW_NEAR2_RSI_OVERSOLD=30
SPOT_SHADOW_NEAR2_RSI_OVERBOUGHT=75
SPOT_SHADOW_NEAR2_TRAILING_STOP_PCT=0.025
SPOT_SHADOW_NEAR2_PROFIT_FLOOR_PCT=0.02
SPOT_SHADOW_NEAR2_MIN_EXIT_PROFIT_PCT=0.01
SPOT_SHADOW_NEAR2_TAKE_PROFIT_PCT=0.03
SPOT_SHADOW_NEAR2_DCA_MAX=3
```

The original **NEAR** profile (global defaults, RSI 14) was kept running as a control.

---

## Example 2 — Finding the ACTIVE profile: no-DCA fast exit

The hypothesis was that DCA was holding back performance on SOLEUR — capital gets tied up averaging down and misses the next entry. The DCA sweep showed this clearly:

```bash
# How much does DCA actually help on the live pairs?
.venv/bin/python backtest.py sweep dca_drop 365 180 --cached
```

DCA=0 (all-in, no averaging) outperformed DCA=3 on SOLEUR over 180d. Then tightened the RSI exit to close faster after recovery:

```bash
# How sensitive is exit RSI on shorter windows?
.venv/bin/python backtest.py sweep rsi_sell 180 90 --cached
```

sell>65 captured recoveries earlier without sacrificing much on big runs. That became **ACTIVE** (SOL+ETH) and **ACTIVE_SOL**:

```
SPOT_SHADOW_ACTIVE_PAIRS=SOLEUR,ETHEUR
SPOT_SHADOW_ACTIVE_RSI_OVERSOLD=33
SPOT_SHADOW_ACTIVE_RSI_OVERBOUGHT=65
SPOT_SHADOW_ACTIVE_DCA_MAX=0
SPOT_SHADOW_ACTIVE_TAKE_PROFIT_PCT=0.05
SPOT_SHADOW_ACTIVE_TRAILING_STOP_PCT=0.025
SPOT_SHADOW_ACTIVE_PROFIT_FLOOR_PCT=0.015
SPOT_SHADOW_ACTIVE_MIN_EXIT_PROFIT_PCT=0.01
SPOT_SHADOW_ACTIVE_EMA_GAP_PCT=0.0
```

**HYBRID** came from the same sweep session — it kept DCA=2 as a middle ground with the same RSI(7)+floor settings, and is compared against ACTIVE in the shadow ranking to see whether averaging down earns its capital cost.

---

## Example 3 — Finding the SOL4H parameters

Hypothesis: 4h candles filter out intraday noise and catch only meaningful dips. The interval sweep showed how win-rate and average hold time vary:

```bash
# Compare 15m / 30m / 1h / 4h on SOLEUR
.venv/bin/python backtest.py sweep interval 365 --cached
```

4h had fewer trades but a higher win rate. But the default 15m RSI params (period=7, buy<30) behaved differently at 4h — the RSI naturally sits higher on longer candles. Recalibrated:

```bash
# What RSI period and thresholds work at 4h?
.venv/bin/python pair_sweep.py SOLEUR 730 --cached
```

RSI(14) with buy<35 and sell>70 fitted the 4h rhythm. Trailing stop needed to be wider to survive intraday swings:

```bash
# Trail / floor / TP sweep at 4h — run against the longer windows because 4h has fewer candles
.venv/bin/python backtest.py sweep trail_pct 730 365 --cached
.venv/bin/python backtest.py sweep floor_pct 730 365 --cached
```

trail=3.5%, floor=2%, TP=7% came out ahead consistently. That became **SOL4H**:

```
SPOT_SHADOW_SOL4H_PAIRS=SOLEUR
SPOT_SHADOW_SOL4H_INTERVAL=4h
SPOT_SHADOW_SOL4H_RSI_PERIOD=14
SPOT_SHADOW_SOL4H_RSI_OVERSOLD=35
SPOT_SHADOW_SOL4H_RSI_OVERBOUGHT=70
SPOT_SHADOW_SOL4H_TRAILING_STOP_PCT=0.035
SPOT_SHADOW_SOL4H_PROFIT_FLOOR_PCT=0.02
SPOT_SHADOW_SOL4H_MIN_EXIT_PROFIT_PCT=0.015
SPOT_SHADOW_SOL4H_TAKE_PROFIT_PCT=0.07
SPOT_SHADOW_SOL4H_DCA_MAX=0
SPOT_SHADOW_SOL4H_EMA_GAP_PCT=0.0
```

SOL4H_FNG, SOL4H_FNG_CD, and SOL4H_FNG_CDT were added on top of these settings to test fear & greed gating and re-entry cooldowns without re-doing the base param search.

---

## Example 4 — Futures HIGH_TP: riding bigger moves

The baseline futures engine uses TP=5% and trail=5%. The question was whether a 10% TP would capture more of the large SOLUSDT / ETHUSDT moves that the 5% exit cuts short.

```bash
# Baseline futures result first
.venv/bin/python backtest_futures.py 365

# Sweep TP values
.venv/bin/python backtest_futures.py sweep tp 365 180 --cached

# Sweep trail in tandem — wider TP needs a wider trail to give positions room
.venv/bin/python backtest_futures.py sweep trail 365 180 --cached
```

TP=10% with trail=8% and floor=3% outperformed the baseline on the 365d window by capturing the big runs that were exiting too early. That became **HIGH_TP**:

```
FUTURES_SHADOW_HIGH_TP_TAKE_PROFIT_PCT=0.10
FUTURES_SHADOW_HIGH_TP_TRAILING_STOP_PCT=0.08
FUTURES_SHADOW_HIGH_TP_PROFIT_FLOOR_PCT=0.03
```

It runs both ETHUSDT and SOLUSDT (same as MAIN) to keep the comparison clean.

---

## Comparing all shadow profiles at once

Once you've added a new shadow to `.env`, run the full shadow comparison across multiple windows to see where it ranks:

```bash
# All spot shadows ranked — 365d, 180d, 90d
.venv/bin/python backtest.py shadows 365 180 90 --cached

# All futures shadows ranked
.venv/bin/python backtest_futures.py shadows 365 180 --cached
```

Profiles that appear consistently in the top half across all windows are candidates for promotion. Profiles that only win on one window are likely overfit to that period.

---

## Example 5 — Random search: find good combinations fast

Single-axis sweeps vary one parameter while holding all others fixed. Random search samples random combinations across *all* axes simultaneously — useful for finding parameter sets that work well together rather than individually.

```bash
# 200 random combinations across all axes, 365d window
.venv/bin/python backtest.py random 200 365 --cached

# 500 trials, validated across two windows
.venv/bin/python backtest.py random 500 365 730 --cached
```

Output: top 20 results ranked by return %, showing every parameter value for each combo. Once you spot a promising row, copy the values into a shadow profile or use the dashboard's "Use" button to load them directly into the form.

---

## Example 6 — 2-axis grid: understand interactions between parameters

Some parameters interact strongly — for example, a tight trailing stop works better with a high profit floor. The grid sweep tests every combination of two axes and prints a return-% matrix so you can see the relationship at a glance.

```bash
# How does trailing stop % interact with profit floor %?
.venv/bin/python backtest.py grid trail_pct floor_pct 365 --cached

# RSI buy threshold vs take-profit %
.venv/bin/python backtest.py grid rsi_buy tp_pct 365 730 --cached

# DCA drop % vs max DCA tranches
.venv/bin/python backtest.py grid dca_drop max_dca 365 --cached
```

Output: a matrix where rows = axis 1 values, columns = axis 2 values, each cell = return %. The best cell is marked with `◄`. Use this after random search to zoom in on a region — random search points you toward a promising area, the grid shows the exact shape of it.

Both random search, 2-axis grid, and optimizer are also available from the dashboard backtest tab (no CLI needed).

---

## Example 7 — Optimizer (random search → coordinate descent)

The optimizer finds a parameter combination that performs well across multiple time windows simultaneously. It runs random search first to find good starting points, then refines each axis one at a time (coordinate descent) until it can't improve further.

```bash
# 200 random trials, then coordinate descent, scored across three windows
.venv/bin/python backtest.py optimize 200 180 365 730

# More trials = better starting points = less chance of a bad local optimum
.venv/bin/python backtest.py optimize 500 180 365 730 --cached
```

Output:
- Per-window table showing what return the best params achieved on each window
- Best params table with the winning value for every axis
- Ready-to-paste `.env` snippet

The first integer is the random trial count (default 200 if omitted); remaining integers are day windows. The scoring function averages `return_pct` across all windows — params that win on 180d, 365d, and 730d simultaneously rank above params that only win on one.

**Runtime**: roughly 3–4 seconds per trial plus coordinate descent rounds. 200 trials × 3 windows ≈ 5–10 min; 500 trials ≈ 15–20 min. Use `--cached` for repeat runs.

---

## General workflow

```bash
# 1. Explore a new pair — full single-axis sweep in one go
.venv/bin/python pair_sweep.py XRPEUR 730

# 2. Run the optimizer to find a robust parameter combination
.venv/bin/python backtest.py optimize 300 180 365 730 --cached

# 3. Or explore manually: random search first, then drill into specific axes
.venv/bin/python backtest.py random 300 365 --cached
.venv/bin/python backtest.py sweep rsi_buy 730 365 180 --cached
.venv/bin/python backtest.py sweep trail_pct 730 365 180 --cached
.venv/bin/python backtest.py sweep dca_drop 365 180 --cached

# 4. Check interactions between two parameters
.venv/bin/python backtest.py grid trail_pct floor_pct 365 --cached

# 5. Validate the combined settings with a full shadow comparison
.venv/bin/python backtest.py shadows 365 180 90 --cached

# 6. Add a shadow profile to .env, restart engine, and let it run live
./stop.sh engine && ./start.sh engine
```

> Always validate across at least two windows (e.g. 365d + 180d). A setting that only wins on the longest window is usually picking up a single lucky trade.
