# Kairos

RSI + EMA200 mean-reversion bot for Binance. Runs in simulation or live mode with two independent engines:
- **Spot** — EUR pairs (SOLEUR, ETHEUR, …), 15m candles
- **Futures** — USDT-M perpetuals (ETHUSDT, SOLUSDT), 15m candles, configurable leverage

Includes a web dashboard, Discord integration, shadow simulators, and backtest/sweep tools for both engines.

## Strategy

### Spot
- **Buy** when RSI drops below the oversold threshold and price is above EMA200 × (1 + ema_gap)
- **Sell signal** when RSI recovers above the overbought threshold — blocked if profit is below `SPOT_MIN_EXIT_PROFIT_PCT`
- **Trailing stop** with a profit floor — rises as price climbs, only fires once above the floor
- **Take-profit** at a configurable target acts as a spike catcher
- **DCA** averages down in tranches as price drops further from entry
- **Partial close** optionally sells a fraction at take-profit and trails the remainder with a tighter stop
- **Time stop** closes stalled positions after N days to free capital
- **Cooldown / re-entry gate** optionally blocks re-entry for N candles or until price drops X% after a stop
- Fast stop-check loop runs every 30s between candles
- Per-pair RSI period, overbought threshold, and other overrides supported

### Futures
- Same RSI + EMA200 signal as spot, on 15m candles
- Isolated-margin LONG only with configurable leverage (default 2×)
- Exits via **take-profit** or **trailing stop** (RSI signal is not used for exits)
- Funding cost applied on open longs at each 8h settlement; optional funding rate gate skips entries when funding is expensive
- Fast-check loop runs every 30s for stop/TP/liquidation/funding between candles
- DCA effectively disabled by default (`FUTURES_DCA_DROP_PCT=0.99`)

## Requirements

- Python 3.10+
- Dependencies: `flask`, `python-binance`, `pandas`, `python-dotenv`, `requests`, `discord.py`

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

You never need to activate the venv — all commands use `.venv/bin/python` directly.

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`. The file is fully annotated. Minimum required before first run:

- **`SPOT_TRADING_PAIRS`** — which EUR pairs to trade (e.g. `SOLEUR,ETHEUR`)
- **`SPOT_MODE`** — leave as `simulation` to paper trade first (no API keys needed)
- **`FUTURES_ENABLED`** — set to `false` if you only want spot

Everything else has sensible defaults. You can tune parameters later.

> The bot reads `.env` only on startup — restart after any change.

### 3. Start in simulation

```bash
./start.sh
```

The script prints the dashboard URL (your machine's LAN IP), e.g.:

```
Dashboard: http://192.168.1.x:8888
```

Open it and confirm the status badge shows **running**. The first tick happens within 15 minutes when the next candle closes. If something looks wrong:

```bash
tail -f data/kairos.log
```

### 4. Go live (optional)

#### Get Binance API keys

1. Log in to Binance → Profile → API Management → Create API
2. Choose **System generated**
3. Enable exactly these permissions:
   - ✅ Enable Reading
   - ✅ Enable Spot & Margin Trading
   - ✅ Enable Futures (only if using futures live mode)
   - ❌ Everything else (no withdrawals, no transfers)
4. Restrict access to your server's IP for extra safety
5. Add both keys to `.env` under `BINANCE_API_KEY` and `BINANCE_SECRET_KEY`

#### Switch to live

In `.env` set `SPOT_MODE=live`, then restart:

```bash
./stop.sh && ./start.sh
```

On startup in live mode the bot fetches your real EUR balance from Binance and logs it. Check `data/kairos.log` to confirm it looks correct before leaving it running.

## Running

```bash
./start.sh          # start bot + dashboard in background
./stop.sh           # stop
tail -f data/kairos.log
```

`start.sh` prints the dashboard URL on startup. `watchdog.sh` can be used to auto-restart on crash.

Two modes (set via `SPOT_MODE` / `FUTURES_MODE` in `.env`):
- `simulation` — paper trades, virtual balance, no API keys needed
- `live` — places real orders on Binance

Spot and futures are completely independent: separate balances, state files, and trade logs.

## Configuration (`.env`)

See `.env.example` for the full annotated list. Key settings:

### Spot

| Variable | Default | Description |
|---|---|---|
| `SPOT_MODE` | `simulation` | `simulation` or `live` |
| `SPOT_TRADING_PAIRS` | `ETHEUR,SOLEUR` | Comma-separated Binance EUR spot pairs; first pair gets priority for DCA funds when multiple fire simultaneously |
| `SPOT_INVESTED` | `0` | Total EUR deposited; dashboard "from X€" reference (0 = use first recorded balance) |
| `SPOT_SIMULATION_BALANCE` | `200.0` | Starting balance for simulation (EUR) |
| `SPOT_FEE` | `0.001` | Binance maker/taker fee rate (0.1%) |
| `SPOT_INTERVAL` | `15m` | Candle interval |
| `SPOT_POSITION_SIZE_PCT` | `0.75` | Fraction of balance per new position |
| `SPOT_DCA_MAX` | `3` | Max DCA tranches per position |
| `SPOT_DCA_DROP_PCT` | `0.01` | Price drop from entry to trigger first DCA |
| `SPOT_DCA_STEP_PCT` | `0.01` | Additional drop per subsequent tranche |
| `SPOT_DCA_SIZE_PCT` | `0.75` | Fraction of remaining balance per DCA tranche |
| `SPOT_TAKE_PROFIT_PCT` | `0.05` | Take-profit target above entry |
| `SPOT_TRAILING_STOP_PCT` | `0.025` | Trailing stop drop from peak |
| `SPOT_PROFIT_FLOOR_PCT` | `0.015` | Min profit before trailing stop can fire |
| `SPOT_MIN_EXIT_PROFIT_PCT` | `0.02` | Min profit before RSI signal sell can fire |
| `SPOT_RSI_PERIOD` | `14` | RSI lookback (global default; per-pair overrides common) |
| `SPOT_RSI_OVERSOLD` | `30` | Buy threshold |
| `SPOT_RSI_OVERBOUGHT` | `75` | Sell threshold |
| `SPOT_EMA_GAP_PCT` | `0` | Min % above EMA200 to allow a buy (0 = no gap filter) |
| `SPOT_DAILY_EMA_FILTER` | `false` | Also require price > daily EMA200 |
| `SPOT_TIME_STOP_DAYS` | `0` | Close stalled positions after N days (0 = off) |
| `SPOT_MAX_DRAWDOWN_PCT` | `0.20` | Pause buys if portfolio drops >X% from peak |
| `SPOT_STOP_CHECK_INTERVAL` | `30` | Between-candle stop check frequency (seconds) |
| `SPOT_STOP_COOLDOWN_CANDLES` | `0` | Block re-entry for N candles after a trailing stop fires |
| `SPOT_REENTRY_DROP_PCT` | `0` | After a stop, require price to drop X% before re-entry (0 = off) |
| `SPOT_PARTIAL_CLOSE_PCT` | `0` | Sell this fraction at take-profit, trail the rest (0 = off) |
| `SPOT_PARTIAL_CLOSE_TRAIL_PCT` | `0.02` | Trailing stop on the remainder after partial close |
| `SPOT_VOLUME_FILTER_PERIOD` | `0` | Require volume > N-bar rolling mean (0 = off) |
| `SPOT_VOLUME_FILTER_MULT` | `1.5` | Volume multiple required |

**Per-pair overrides** — append the pair name (e.g. `_SOLEUR`) to any of these:

`SPOT_RSI_PERIOD`, `SPOT_RSI_OVERSOLD`, `SPOT_RSI_OVERBOUGHT`, `SPOT_EMA_GAP_PCT`, `SPOT_MIN_EXIT_PROFIT_PCT`, `SPOT_TAKE_PROFIT_PCT`, `SPOT_TRAILING_STOP_PCT`, `SPOT_PROFIT_FLOOR_PCT`, `SPOT_DCA_MAX`, `SPOT_DCA_DROP_PCT`, `SPOT_DCA_STEP_PCT`, `SPOT_TIME_STOP_DAYS`, `SPOT_STOP_COOLDOWN_CANDLES`, `SPOT_REENTRY_DROP_PCT`, `SPOT_VOLUME_FILTER_PERIOD`, `SPOT_VOLUME_FILTER_MULT`, `SPOT_PARTIAL_CLOSE_PCT`, `SPOT_PARTIAL_CLOSE_TRAIL_PCT`

Current per-pair overrides in use:
```
# SOL — fast, volatile pair; short RSI reacts quicker to momentum swings
SPOT_RSI_PERIOD_SOLEUR=7
# SOL — high overbought threshold; SOL tends to stay elevated before reversing
SPOT_RSI_OVERBOUGHT_SOLEUR=80
# SOL — lower exit profit floor; SOL moves fast so a small gain beats a miss
SPOT_MIN_EXIT_PROFIT_PCT_SOLEUR=0.01

# ETH — short RSI for faster signal response on a lower-volatility pair
SPOT_RSI_PERIOD_ETHEUR=7
# ETH — lower overbought threshold; ETH reverses earlier than SOL
SPOT_RSI_OVERBOUGHT_ETHEUR=65

# ADA — very short RSI; ADA oscillates rapidly and needs an aggressive period
SPOT_RSI_PERIOD_ADAEUR=5
# ADA — slightly relaxed oversold threshold; ADA rarely dips to the default 30
SPOT_RSI_OVERSOLD_ADAEUR=33
# ADA — EMA gap filter disabled; ADA often trades below EMA200 for extended periods
SPOT_EMA_GAP_PCT_ADAEUR=0
```

When `SPOT_DCA_MAX` is 0 for a pair, the bot goes all-in on entry. The last DCA tranche always uses 100% of remaining balance so no capital sits idle.

### Shadow simulation profiles

Shadow profiles run paper-trade simulations alongside the live bot using the same candle feed. Each profile has its own virtual balance, state file, and parameter set.

Enable with `SPOT_SHADOW_PROFILES=PROFILE1,PROFILE2,...`. Each profile supports the following overrides (prefix `SPOT_SHADOW_<NAME>_`):

| Suffix | Description |
|---|---|
| `PAIRS` | Comma-separated pairs to trade |
| `INTERVAL` | Candle interval (defaults to `SPOT_INTERVAL`) |
| `BALANCE` | Starting virtual balance (EUR) |
| `RSI_PERIOD` / `RSI_OVERSOLD` / `RSI_OVERBOUGHT` | RSI parameters |
| `EMA_GAP_PCT` | EMA gap filter |
| `TAKE_PROFIT_PCT` | Take-profit target |
| `TRAILING_STOP_PCT` / `PROFIT_FLOOR_PCT` | Trailing stop parameters |
| `MIN_EXIT_PROFIT_PCT` | Min profit before signal sell fires |
| `DCA_MAX` / `DCA_DROP_PCT` / `DCA_STEP_PCT` / `DCA_SIZE_PCT` | DCA parameters |
| `POSITION_SIZE_PCT` | Fraction of balance per trade |
| `TIME_STOP_DAYS` | Time stop (0 = off) |
| `STOP_COOLDOWN_CANDLES` | Re-entry cooldown after trailing stop |
| `REENTRY_DROP_PCT` | Re-entry price gate after a stop |
| `PARTIAL_CLOSE_PCT` / `PARTIAL_CLOSE_TRAIL_PCT` | Partial close parameters |
| `VOLUME_FILTER_PERIOD` / `VOLUME_FILTER_MULT` | Volume filter |
| `TYPE=grid` | Grid strategy instead of RSI mean-reversion |


### Futures

| Variable | Default | Description |
|---|---|---|
| `FUTURES_ENABLED` | `false` | Enable futures engine |
| `FUTURES_MODE` | `simulation` | `simulation` or `live` |
| `FUTURES_TRADING_PAIRS` | `ETHUSDT,SOLUSDT` | USDT-M perpetual pairs |
| `FUTURES_SIMULATION_BALANCE` | `200.0` | Starting balance (USDT) |
| `FUTURES_LEVERAGE` | `2` | Leverage multiplier |
| `FUTURES_MARGIN_TYPE` | `ISOLATED` | `ISOLATED` or `CROSSED` |
| `FUTURES_FEE` | `0.0005` | Taker fee rate (0.05%) |
| `FUTURES_POSITION_SIZE_PCT` | `1.0` | Fraction of balance posted as margin per position |
| `FUTURES_DCA_DROP_PCT` | `0.99` | Drop to trigger DCA (0.99 = effectively disabled) |
| `FUTURES_DCA_SIZE_PCT` | `0.75` | Fraction of balance for DCA tranche |
| `FUTURES_TAKE_PROFIT_PCT` | `0.05` | Take-profit target (on notional) |
| `FUTURES_TRAILING_STOP_PCT` | `0.05` | Trailing stop from peak |
| `FUTURES_PROFIT_FLOOR_PCT` | `0.01` | Min profit before trailing stop fires |
| `FUTURES_RSI_PERIOD` | `7` | RSI lookback |
| `FUTURES_RSI_OVERSOLD` | `25` | Buy threshold |
| `FUTURES_EMA_GAP_PCT` | `0.02` | Min % above EMA200 to allow entry |
| `FUTURES_MAX_FUNDING_RATE` | `0.0005` | Skip entry when 8h funding rate exceeds this (0 = disabled) |
| `FUTURES_MAX_DRAWDOWN_PCT` | `0` | Pause longs if portfolio drops >X% from peak |
| `FUTURES_STOP_CHECK_INTERVAL` | `30` | Between-candle risk check frequency (seconds) |

Per-pair overrides: append the symbol, e.g. `FUTURES_RSI_PERIOD_ETHUSDT=7`.

Futures shadows use `FUTURES_SHADOW_PROFILES` and `FUTURES_SHADOW_<NAME>_` prefixes.

### Credentials & dashboard

| Variable | Description |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_SECRET_KEY` | Required for live mode |
| `DISCORD_WEBHOOK_URL` | Trade alerts and tick embeds |
| `DISCORD_BOT_TOKEN` | Slash command bot (optional) |
| `DISCORD_GUILD_ID` | Guild ID for instant slash command registration |
| `WEB_HOST` | Dashboard bind address (`0.0.0.0` to expose on LAN) |
| `WEB_PORT` | Dashboard port (default 8888) |
| `TAX_RATE_LOW` | Capital gains rate on net P&L up to `TAX_BRACKET` (default 0.30) |
| `TAX_BRACKET` | EUR threshold between low and high tax rate (default 30000) |
| `TAX_RATE_HIGH` | Capital gains rate on net P&L above `TAX_BRACKET` (default 0.34) |

## Discord

When `DISCORD_WEBHOOK_URL` is set the bot posts tick embeds, trade alerts, extreme RSI/EMA alerts, and a daily summary.

Slash commands (requires `DISCORD_BOT_TOKEN`):

| Command | Description |
|---|---|
| `/crp_status` | Status, balance, and open positions |
| `/crp_pause` | Pause trading (no new entries) |
| `/crp_resume` | Resume after a pause |
| `/crp_buy` | Manually open a position |
| `/crp_close` | Manually close an open position |
| `/crp_summary` | Post a performance summary |
| `/crp_config` | Show current configuration |
| `/crp_clear` | Delete all messages in the channel |

## Backtest

Add `--cached` to any backtest command to skip candle syncing and use only the local cache (faster, reproducible).

### Spot

```bash
.venv/bin/python backtest.py                        # current settings, default window
.venv/bin/python backtest.py 90                     # custom day window
.venv/bin/python backtest.py shadows                # all shadow profiles ranked (365d + 180d)
.venv/bin/python backtest.py shadows 365 180 90     # custom windows

.venv/bin/python backtest.py topup 200 25 730       # monthly top-up simulation

.venv/bin/python backtest.py sweep buyrsi           # buy RSI threshold
.venv/bin/python backtest.py sweep trail            # trailing stop %
.venv/bin/python backtest.py sweep floor            # profit floor %
.venv/bin/python backtest.py sweep exit             # min-exit profit %
.venv/bin/python backtest.py sweep dca              # DCA settings
.venv/bin/python backtest.py sweep cooldown         # re-entry cooldown
.venv/bin/python backtest.py sweep all              # all sweeps

.venv/bin/python pair_sweep.py XRPEUR               # full sweep for a single pair (730/365/180d)
.venv/bin/python pair_sweep.py DOTEUR 365 --cached  # custom window, skip sync
```

### Futures

```bash
.venv/bin/python backtest_futures.py                # baseline, current config
.venv/bin/python backtest_futures.py 90             # custom window
.venv/bin/python backtest_futures.py shadows        # all futures shadow profiles ranked (365d + 180d)

.venv/bin/python backtest_futures.py sweep rsi      # RSI thresholds
.venv/bin/python backtest_futures.py sweep trail    # trailing stop %
.venv/bin/python backtest_futures.py sweep floor    # profit floor %
.venv/bin/python backtest_futures.py sweep tp       # take-profit %
.venv/bin/python backtest_futures.py sweep pos      # position size %
.venv/bin/python backtest_futures.py sweep dca      # DCA settings
.venv/bin/python backtest_futures.py sweep lev      # leverage
.venv/bin/python backtest_futures.py sweep all      # all sweeps
```

The futures backtest models isolated margin, 0.05% taker fee, 0.01%/8h funding on open longs, and liquidation at `entry × (1 − 1/lev + 0.5%)`.

### Grid

```bash
.venv/bin/python backtest_grid.py                   # all configured SPOT_TRADING_PAIRS, 180d
.venv/bin/python backtest_grid.py XRPEUR            # single pair, 180d
.venv/bin/python backtest_grid.py XRPEUR SOLEUR 365 # multiple pairs, custom days
.venv/bin/python backtest_grid.py XRPEUR --cached   # skip sync
```

Sweeps spacing × level combinations and ranks by realised P&L%.

## Project layout

```
bot/
  config.py             — all settings from .env, per-pair helpers (spot + futures)
  spot_engine.py        — spot trading loop: candle alignment, signal execution, stop-check thread
  spot_simulator.py     — spot shadow simulators + SpotShadowSimulator class
  spot_exchange.py      — Binance spot API wrapper
  spot_risk.py          — Position dataclass, trailing stop, DCA, PnL
  strategy.py           — RSI + EMA200 signal (shared by spot + futures)
  candles.py            — local SQLite candle cache, incremental sync (spot + futures)
  notifier.py           — Discord webhooks, tick embeds, alerts, log buffering
  discord_bot.py        — discord.py slash command bot
  db.py                 — trade log, balance history, candles (SQLite, WAL mode)
  futures_engine.py     — futures loop: 15m signal thread + 30s risk/funding thread
  futures_simulator.py  — futures shadow simulators + FuturesShadowSimulator class
  futures_exchange.py   — Binance futures API wrapper
  futures_risk.py       — FuturesPosition dataclass, liquidation price, PnL
web/
  app.py                — Flask dashboard + REST API (spot + futures endpoints)
  templates/
    index.html          — single-page dashboard (Spot / Futures tabs)
data/
  trades.db             — trade history, balance history, candle cache (WAL)
  spot_state_*.json     — spot engine state (* = simulation or live; one active at a time)
  futures_state_*.json  — futures engine state (* = simulation or live; one active at a time)
  spot_state_shadow_*.json    — per-shadow spot state
  futures_state_shadow_*.json — per-shadow futures state
backtest.py             — spot backtest + parameter sweep tool
backtest_futures.py     — futures backtest + parameter sweep tool
xrp_sweep.py            — XRP/BNB parameter sweep
dot_sweep.py            — DOT parameter sweep
main.py                 — entry point: starts spot engine, futures engine, web dashboard
start.sh / stop.sh      — process management
watchdog.sh             — auto-restart on crash
```
