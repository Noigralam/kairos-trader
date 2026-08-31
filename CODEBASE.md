# Codebase Overview

Every Python file in the project, what it does, and how it fits together.
Total: ~10 100 lines across 23 files.

---

## Entry points

### `main.py` *(51 lines)*
Engine process entry point. Sets up logging to `data/cairn.log`, calls `init_db()`, then starts the spot engine, Discord bot, and (if enabled) futures engine. Keeps the main thread alive with a `while True: sleep(60)` loop so the daemon threads keep running. No Flask.

### `dashboard.py` *(44 lines)*
Dashboard process entry point. Sets `CAIRN_DASHBOARD_ONLY=1` before importing anything, which causes control endpoints in `app.py` to return 503 instead of trying to start engine threads in the wrong process. Sets up its own logging to `data/dashboard.log`, then starts Flask. No engine threads.

---

## `bot/` — trading engine

### `bot/__init__.py` *(1 line)*
Just `__version__`. Imported wherever the version string is needed — snapshots, log formatter, dashboard header.

### `bot/config.py` *(316 lines)*
Reads all settings from `.env` via `python-dotenv`. Exports every constant (`SPOT_MODE`, `FUTURES_LEVERAGE`, etc.) and per-pair helper functions (`rsi_period_for(pair)`, `ema_gap_for(pair)`, `futures_rsi_oversold_for(sym)`, etc.) that look up pair-specific overrides and fall back to globals. Also parses shadow profile definitions from env vars.

### `bot/strategy.py` *(84 lines)*
The shared signal brain. `compute_signal()` takes a candle DataFrame and returns a `SignalResult` with the RSI value, EMA200 trend line, signal direction (`BUY` / `SELL` / `HOLD`), and a human-readable reason string. Used identically by the spot engine, futures engine, all shadows, and the dashboard's signal endpoints.

### `bot/candles.py` *(120 lines)*
Local OHLCV cache backed by `data/trades.db`. `sync(pair, interval)` fetches only candles newer than the last stored one (incremental). `initial_sync()` does a bulk backfill on first run. `get_df()` returns a pandas DataFrame ready for strategy computation. Shared by spot, futures, and all backtests.

### `bot/db.py` *(258 lines)*
SQLite persistence layer (`data/trades.db`, WAL mode). Manages three tables: `trades` (every executed buy/sell with pair, price, amount, PnL, mode), `balance_history` (portfolio value snapshots over time), and the candle cache. `log_trade()` also hooks into `bot/tax.py` for live spot trades. Exposes query helpers used by the dashboard (trade stats, hold times, balance history series).

### `bot/notifier.py` *(369 lines)*
All outbound communication. Sends Discord webhook embeds for tick summaries, trade alerts, daily summaries, and bot status changes. Maintains an in-memory ring buffer of recent log lines served by `/api/log` when running in-process. Also fetches the Fear & Greed index and builds matplotlib chart images attached to tick embeds. `notify()` is the single call used throughout the codebase.

### `bot/discord_bot.py` *(301 lines)*
The `discord.py` slash-command bot (`/crp_status`, `/crp_pause`, `/crp_buy`, `/crp_close`, etc.). Runs in its own daemon thread. Commands call into spot engine functions directly. Requires `DISCORD_BOT_TOKEN` and `DISCORD_GUILD_ID`.

### `bot/tax.py` *(348 lines)*
FIFO capital gains tracker for live spot trades. Maintains `fifo_lots` and `fifo_disposals` tables in `trades.db`. `_add_lot()` records cost basis on BUY; `_dispose_fifo()` matches SELL proceeds against the oldest lots in order, recording gain/loss per disposal. `annual_summary()` returns year-by-year totals. `integrity_check()` compares lot quantities against held positions to catch mismatches. `rebuild()` wipes and re-processes all historical live trades from scratch.

### `bot/spot_exchange.py` *(96 lines)*
Thin wrapper around the Binance spot REST API. `get_price(pair)` returns the current ask. `get_quote_balance()` fetches the real quote-currency wallet balance. `place_order()` / `place_sell_order()` execute real trades in live mode. `get_account_asset()` fetches a specific asset balance for position reconciliation.

### `bot/spot_risk.py` *(63 lines)*
`Position` dataclass — the in-memory representation of an open spot position. Holds entry price, amount, cost basis, peak price, DCA count, take-profit price, and live-since timestamp. Methods: `trailing_stop_level()`, `unrealized_pnl()`, `peak()`. Also `apply_dca()` for averaging down and `calc_pnl()` for realised P&L.

### `bot/spot_engine.py` *(470 lines)*
The spot trading loop. Two threads: the main loop (aligns to candle boundaries, fetches prices, syncs candles, computes RSI+EMA signals, executes entries/DCAs, ticks all shadows) and a fast stop-check loop (runs every `SPOT_STOP_CHECK_INTERVAL` seconds, fetches prices, calls `check_stops()` on main and all shadows). Handles missed-tick detection and daily summary scheduling. Writes `data/status_spot.json` atomically on every tick and stop-check.

### `bot/spot_simulator.py` *(1 385 lines)*
The largest file. Three things in one:
1. `SimState` dataclass + `init()` / `_load()` / `_save()` — the main spot simulation state, persisted to `data/spot_state_{mode}.json`; written on first start so balance persists across restarts.
2. `open_position()`, `close_position()`, `dca_position()`, `check_stops()` — the full spot trade execution logic used by both live and simulation modes.
3. `SpotShadowSimulator` and `GridShadowSimulator` classes — each shadow has its own state file, parameter overrides, and `tick()` / `check_stops()` methods. `get_shadows()` lazy-inits from config on first call so the dashboard process can load shadow state without the engine running.

### `bot/futures_exchange.py` *(157 lines)*
Binance USDT-M futures REST wrapper. `get_mark_price()`, `get_klines()`, `get_funding_rate()`, `get_next_funding_time()`, `get_usdt_balance()`, `set_leverage()`, `set_margin_type()`. In live mode, also `place_futures_order()` for real order execution.

### `bot/futures_risk.py` *(121 lines)*
`FuturesPosition` dataclass for an open futures long. Tracks entry price, amount, margin, leverage, highest price, funding paid, and opened-at timestamp. Methods: `liquidation_price()`, `trailing_stop_level()`, `unrealized_pnl()`, `pnl_pct()`.

### `bot/futures_simulator.py` *(713 lines)*
Futures counterpart to `spot_simulator.py`. `FuturesSimState` + `init()` / `_load()` / `_save()` — state persisted to `data/futures_state_{mode}.json`, written immediately on first start so the session persists. `open_long()`, `dca_long()`, `close_long()`, `check_stops()`, `apply_funding()` — full futures trade logic with liquidation checks, isolated margin accounting, and funding cost application. `FuturesShadowSimulator` for parameter variant shadows. `get_futures_shadows()` lazy-inits from config on first call.

### `bot/futures_engine.py` *(443 lines)*
Futures trading loop, same two-thread structure as spot: 15-minute signal loop and 30-second stop-check loop. `start()` calls `sim_init()` and `init_futures_shadows()` before writing the first snapshot, so the dashboard always sees loaded state from the very first second. Handles funding rate settlement gating and writes `data/status_futures.json` atomically every tick.

---

## `web/`

### `web/__init__.py` *(0 lines)*
Empty — marks `web/` as a package.

### `web/app.py` *(2 272 lines)*
The Flask dashboard. ~50 API endpoints covering: spot and futures status (read from JSON snapshots), trades, balance history, signals, charts (OHLCV + RSI + EMA200 + Bollinger Bands + actual trade markers), shadow profile status/trades/signals/charts, futures shadow ranking, FIFO tax summary/export/integrity/rebuild, Fear & Greed index, and recent log. Backtest API: `/api/backtest/run`, `/api/backtest/sweep`, `/api/backtest/fullsweep`, `/api/backtest/randomsearch`, `/api/backtest/2axissweep`, `/api/backtest/optimize` — all async (return a job_id, polled via `/api/backtest/<job_id>`). Shadow creation endpoint writes a new profile block to `.env` on the fly. Control endpoints (`/api/control`, `/api/futures/control`) return HTTP 503 when `CAIRN_DASHBOARD_ONLY=1`. PIN-gated actions use a lockout file to rate-limit brute force. Version badge in the dashboard header reflects the running dashboard process (`__version__`), not the engine snapshot.

---

## Backtest / research tools

### `backtest.py` *(1 884 lines)*
Spot backtest engine and parameter sweep tool. Replays historical candles from the local cache against the same trade logic used in the live engine. Modes:
- **Baseline** — single run with current config, one or more day windows
- **Shadows** — all shadow profiles ranked by return
- **Sweep** — vary one axis over 12 preset values (`sweep_ranges.toml`); 16 axes available: `rsi_buy`, `rsi_period`, `rsi_sell`, `tp_pct`, `trail_pct`, `floor_pct`, `min_exit`, `pos_pct`, `dca_drop`, `dca_step`, `max_dca`, `ema_gap`, `time_stop_days`, `stop_cooldown`, `hard_stop`, `partial_close_pct`. Legacy short names (`buyrsi`, `trail`, `floor`, etc.) still accepted as aliases. `sweep all` runs every axis.
- **Random** — `backtest.py random N days…` samples N random combinations from all axes simultaneously; prints top 20 ranked by return. First integer is trial count, remaining integers are day windows.
- **Grid** — `backtest.py grid param1 param2 days` tests all 12×12 combinations of two axes; prints a return-% matrix.
- **Optimize** — `backtest.py optimize N days…` runs random search (N trials) then coordinate descent from the top 3 starting points, scoring by average return across all specified windows. Prints best params table and a `.env` snippet. May take 5–20 min. First integer is trial count.
- **Topup** — monthly capital injection simulation.

Results print as ranked tables with a footer legend. All output is mirrored to a descriptively-named file in `backtest_results/`. Sweep axis values live in `sweep_ranges.toml` — edit and rerun, no code change needed.

### `backtest_futures.py` *(654 lines)*
Futures backtest with isolated-margin accounting: models 0.05% taker fee, 0.01%/8h funding on open longs, and liquidation at `entry × (1 − 1/leverage + 0.5%)`. Sweeps: RSI thresholds, trailing stop, profit floor, take-profit, position size, DCA, and leverage. Also does shadow profile comparison ranked by return. Footer legend and output file mirroring identical to `backtest.py`.

### `backtest_grid.py` *(326 lines)*
Grid strategy backtester. Places simulated limit orders at fixed price levels around a centre price and tracks fills as price oscillates. Sweeps spacing and number-of-levels combinations, ranks by realised P&L. Footer legend explains all grid-specific columns (spacing, levels, trades, realised%, stuck%, recenters, open_slots, fees) and how the grid mechanics work. Output mirrored to `backtest_results/`. Used for evaluating `XRP_GRID`, `SOL_GRID_*` shadow profiles.

### `pair_sweep.py` *(63 lines)*
Convenience wrapper around `backtest.py`. Points `PAIRS` at a single spot pair and runs all 15 sweep axes (RSI period, buy threshold, exit/TP, floor, trail, min exit, position size, DCA drop/step/max, EMA gap, hard stop, time stop, cooldown, partial close) across 730d / 365d / 180d in one shot. The natural starting point when evaluating a new pair candidate. See `SWEEP_EXAMPLES.md` for worked examples.
