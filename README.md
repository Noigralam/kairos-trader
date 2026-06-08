# Kairos

RSI + EMA200 mean-reversion bot for Binance. Runs in simulation or live mode with two independent engines:
- **Spot** — EUR pairs (ETHEUR, SOLEUR, ADAEUR), 15m candles
- **Futures** — USDT-M perpetuals (ETHUSDT, SOLUSDT), 15m candles, configurable leverage

Includes a web dashboard, Discord integration, and backtest tools for both engines.

## Strategy

### Spot
- **Buy** when RSI drops below the oversold threshold (default 30) and price is above EMA200 + gap filter
- **Sell signal** when RSI recovers above the overbought threshold — blocked if profit is below `SPOT_MIN_EXIT_PROFIT_PCT`
- **Trailing stop** with a profit floor — the stop rises as price climbs, only fires once above the floor
- **Take-profit** at +5% acts as a spike catcher
- **DCA** averages down once per position when price drops ≥1% from entry
- Fast-check loop runs every 60s for stop/TP checks between candles
- Per-pair RSI and overbought thresholds (e.g. `SPOT_RSI_PERIOD_ETHEUR=7`)

### Futures
- Same RSI + EMA200 signal as spot, on 15m candles
- Isolated-margin LONG only with configurable leverage (default 2×)
- Exits via **take-profit** or **trailing stop** (with profit floor) — RSI overbought signal is ignored, trailing stop handles exits
- DCA effectively disabled by default (`FUTURES_DCA_DROP_PCT=0.99`)
- Funding cost applied on open longs at each 8h settlement
- Fast-check loop runs every 60s for stop/TP/liquidation/funding between candles

## Requirements

- Python 3.10+
- Dependencies: `flask`, `python-binance`, `pandas`, `python-dotenv`, `requests`, `discord.py`

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` — every setting is documented inline. At minimum:
- Set `SPOT_MODE=simulation` (default) to paper trade without API keys
- Set `SPOT_TRADING_PAIRS` and `FUTURES_TRADING_PAIRS` to the pairs you want
- Set `FUTURES_ENABLED=false` if you only want to run spot

> **Note:** The bot reads `.env` only on startup. Restart after any changes.

### 3. Verify simulation works

```bash
./start.sh
```

Open `http://localhost:8888` and confirm the dashboard loads, the status badge shows **running**, and tick logs appear every 15 minutes. Check `data/kairos.log` if anything looks wrong:

```bash
tail -f data/kairos.log
```

### 4. Go live (optional)

#### Binance API keys

1. Log in to Binance → Profile → API Management → Create API
2. Choose **System generated**
3. Enable exactly these permissions:
   - ✅ Enable Reading
   - ✅ Enable Spot & Margin Trading
   - ✅ Enable Futures (only if using futures live mode)
   - ❌ Everything else (no withdrawals, no transfers)
4. Restrict access to your server's IP for extra safety
5. Add the key and secret to `.env`:
   ```
   BINANCE_API_KEY=your_key
   BINANCE_SECRET_KEY=your_secret
   ```

#### Switch to live

In `.env`, set:
```
SPOT_MODE=live
```

Then restart:
```bash
./stop.sh && ./start.sh
```

On startup in live mode the bot fetches your real EUR balance from Binance and logs it. Confirm this looks correct in `data/kairos.log` before leaving it running.

## Running

```bash
./start.sh          # start bot + dashboard in background
./stop.sh           # stop
tail -f data/kairos.log
```

Dashboard: `http://localhost:8888`

## Modes

Set `SPOT_MODE` / `FUTURES_MODE` in `.env`:

- `simulation` — paper trades, tracks virtual balance, no API keys needed
- `live` — places real orders, reads balance from exchange

Spot and futures are completely independent: separate balances, separate state files, separate trade logs. Switching one mode never affects the other.

## Configuration (`.env`)

See `.env.example` for the full annotated list. Key settings:

### Spot

| Variable | Default | Description |
|---|---|---|
| `SPOT_INVESTED` | `0` | Total EUR deposited; dashboard "from" reference (0 = first recorded balance) |
| `SPOT_MODE` | `simulation` | `simulation` or `live` |
| `SPOT_TRADING_PAIRS` | `ETHEUR,SOLEUR` | Comma-separated Binance EUR spot pairs |
| `SPOT_SIMULATION_BALANCE` | `200.0` | Starting balance for simulation (EUR) |
| `SPOT_FEE` | `0.001` | Binance maker/taker fee rate (0.1%) |
| `SPOT_INTERVAL` | `15m` | Candle interval |
| `SPOT_POSITION_SIZE_PCT` | `0.75` | Fraction of balance per trade |
| `SPOT_DCA_MAX` | `3` | Max DCA tranches per position |
| `SPOT_DCA_DROP_PCT` | `0.01` | Drop from entry to trigger first DCA |
| `SPOT_DCA_STEP_PCT` | `0.01` | Additional drop per subsequent tranche |
| `SPOT_DCA_SIZE_PCT` | `0.75` | Fraction of balance per DCA tranche |
| `SPOT_TAKE_PROFIT_PCT` | `0.05` | Take-profit target |
| `SPOT_TRAILING_STOP_PCT` | `0.05` | Trailing stop distance from peak |
| `SPOT_PROFIT_FLOOR_PCT` | `0.03` | Min profit before trailing stop can fire |
| `SPOT_MIN_EXIT_PROFIT_PCT` | `0.02` | Min profit before a signal sell can fire |
| `SPOT_RSI_PERIOD` | `14` | RSI lookback period |
| `SPOT_RSI_OVERSOLD` | `30` | Buy threshold |
| `SPOT_RSI_OVERBOUGHT` | `75` | Sell threshold |
| `SPOT_EMA_GAP_PCT` | `0.02` | Min % above EMA200 to allow a buy |
| `SPOT_TIME_STOP_DAYS` | `0` | Close stalled positions after N days (0 = off) |
| `SPOT_MAX_DRAWDOWN_PCT` | `0` | Pause buys if portfolio drops >X% from peak (0 = off) |
| `SPOT_STOP_CHECK_INTERVAL` | `60` | Between-candle stop check frequency (seconds) |

Per-pair overrides: append the pair name, e.g. `SPOT_RSI_PERIOD_ETHEUR=7`, `SPOT_RSI_OVERBOUGHT_SOLEUR=80`, `SPOT_EMA_GAP_PCT_ADAEUR=0`.

The last DCA tranche always uses 100% of the remaining balance regardless of `SPOT_DCA_SIZE_PCT`, so no capital sits idle until the position closes.

### Shadow simulation profiles

Shadow profiles run paper-trade simulations alongside the live bot using the same candle feed. Each profile has its own virtual balance and state file, and can trade a different set of pairs than the live bot.

Enable profiles with `SPOT_SHADOW_PROFILES=ACTIVE,HYBRID,ETH` (comma-separated names). Each profile supports the following overrides (prefix `SPOT_SHADOW_<NAME>_`):

| Suffix | Description |
|---|---|
| `PAIRS` | Comma-separated pairs to trade (defaults to `SPOT_TRADING_PAIRS` if omitted) |
| `BALANCE` | Starting virtual balance (EUR) |
| `RSI_OVERSOLD` / `RSI_OVERBOUGHT` / `RSI_PERIOD` | RSI parameters |
| `TRAILING_STOP_PCT` / `PROFIT_FLOOR_PCT` / `TAKE_PROFIT_PCT` | Exit parameters |
| `MIN_EXIT_PROFIT_PCT` | Min profit before a signal sell can fire |
| `EMA_GAP_PCT` | Min % above EMA200 to allow a buy |
| `DCA_MAX` / `DCA_DROP_PCT` / `DCA_STEP_PCT` / `DCA_SIZE_PCT` | DCA parameters |
| `POSITION_SIZE_PCT` | Fraction of balance per trade |

Example: `SPOT_SHADOW_ACTIVE_PAIRS=ETHEUR,SOLEUR`, `SPOT_SHADOW_ACTIVE_DCA_MAX=0`.

### Futures

| Variable | Default | Description |
|---|---|---|
| `FUTURES_ENABLED` | `false` | Enable futures engine |
| `FUTURES_MODE` | `simulation` | `simulation` or `live` |
| `FUTURES_TRADING_PAIRS` | `ETHUSDT,SOLUSDT` | USDT-M perpetual pairs |
| `FUTURES_SIMULATION_BALANCE` | `200.0` | Starting balance (USDT) |
| `FUTURES_LEVERAGE` | `2` | Leverage multiplier |
| `FUTURES_MARGIN_TYPE` | `ISOLATED` | `ISOLATED` (loss capped per position) or `CROSSED` |
| `FUTURES_FEE` | `0.0005` | Taker fee rate (0.05%); funding adds on top |
| `FUTURES_POSITION_SIZE_PCT` | `0.50` | Fraction of balance posted as margin per trade |
| `FUTURES_DCA_DROP_PCT` | `0.99` | Drop from entry to trigger DCA (0.99 = disabled) |
| `FUTURES_DCA_SIZE_PCT` | `0.75` | Fraction of balance for DCA |
| `FUTURES_TAKE_PROFIT_PCT` | `0.05` | Take-profit target (on notional) |
| `FUTURES_TRAILING_STOP_PCT` | `0.05` | Trailing stop distance from peak |
| `FUTURES_PROFIT_FLOOR_PCT` | `0.01` | Min profit before trailing stop can fire |
| `FUTURES_MIN_EXIT_PROFIT_PCT` | `0.02` | Min profit before signal exit fires |
| `FUTURES_RSI_PERIOD` | `7` | RSI lookback period |
| `FUTURES_RSI_OVERSOLD` | `25` | Buy threshold (RSI<25 outperforms RSI<35 over 2yr backtest) |
| `FUTURES_RSI_OVERBOUGHT` | `75` | Logged for reference; exits handled by trailing stop |
| `FUTURES_EMA_GAP_PCT` | `0.02` | Min % above EMA200 to allow a long entry |
| `FUTURES_MAX_DRAWDOWN_PCT` | `0` | Pause longs if portfolio drops >X% from peak (0 = off) |
| `FUTURES_STOP_CHECK_INTERVAL` | `60` | Between-candle risk check frequency (seconds) |

Per-pair overrides: `FUTURES_RSI_PERIOD_ETHUSDT=7`, etc.

### Credentials & dashboard

| Variable | Description |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_SECRET_KEY` | Required for live mode |
| `DISCORD_WEBHOOK_URL` | Trade alerts and tick embeds |
| `DISCORD_BOT_TOKEN` | Slash command bot (optional) |
| `DISCORD_GUILD_ID` | Guild ID for instant slash command sync (optional) |
| `WEB_HOST` | Dashboard bind address (`0.0.0.0` to expose on LAN) |
| `WEB_PORT` | Dashboard port (default 8888) |

## Discord

When `DISCORD_WEBHOOK_URL` is set, the bot posts tick embeds, trade alerts, extreme condition alerts (RSI < 20 / > 85, EMA200 crossovers), and a daily summary.

### Adding the bot to your server

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) → your application → OAuth2 → URL Generator
2. Scopes: `bot` + `applications.commands`
3. Bot permissions: `Send Messages`, `Read Message History`, `Manage Messages`
4. Open the generated URL and invite the bot to your server

When `DISCORD_BOT_TOKEN` is set, the following slash commands are available:

| Command | Description |
|---|---|
| `/crp_status` | Show status, balance and open positions |
| `/crp_pause` | Pause trading (no new trades) |
| `/crp_resume` | Resume after a pause |
| `/crp_buy` | Manually open a position |
| `/crp_close` | Manually close an open position |
| `/crp_summary` | Post a performance summary to the channel |
| `/crp_config` | Show current configuration |
| `/crp_clear` | Delete all messages in the channel |

## Backtest

### Spot

```bash
python backtest.py                           # pair comparison, current settings
python backtest.py 90                        # custom day window

python backtest.py topup 200 25 730          # monthly top-up: start=€200, +€25/month, 730 days

python backtest.py sweep buyrsi              # buy RSI threshold
python backtest.py sweep trail               # trailing stop %
python backtest.py sweep floor               # profit floor %
python backtest.py sweep exit                # min-exit profit %
python backtest.py sweep dca                 # DCA drop % + size %
python backtest.py sweep all                 # all sweeps

python backtest.py sweep trail 730 365 180   # custom day windows
```

### Futures

```bash
python backtest_futures.py                   # baseline, current config
python backtest_futures.py 90                # custom day window

python backtest_futures.py sweep rsi         # RSI buy/sell thresholds
python backtest_futures.py sweep trail       # trailing stop %
python backtest_futures.py sweep floor       # profit floor %
python backtest_futures.py sweep tp          # take-profit %
python backtest_futures.py sweep pos         # position size %
python backtest_futures.py sweep dca         # DCA settings
python backtest_futures.py sweep lev         # leverage
python backtest_futures.py sweep all         # all sweeps

python backtest_futures.py sweep all 730 365 180
```

The futures backtest models isolated margin, 0.05% taker fee, 0.01%/8h funding on open longs, and liquidation at `entry × (1 − 1/lev + 0.5%)`.

## Project layout

```
bot/
  config.py                     — all settings from .env, per-pair helpers (spot + futures)
  spot_engine.py                — spot trading loop: candle alignment, signal execution, stop-check thread
  spot_simulator.py             — spot position management (simulation + live)
  spot_exchange.py              — Binance spot API wrapper
  spot_risk.py                  — spot Position dataclass, trailing stop, DCA, PnL
  strategy.py                   — RSI + EMA200 signal (shared by spot + futures)
  candles.py                    — local SQLite candle cache, incremental sync (shared by spot + futures)
  notifier.py                   — Discord webhooks, tick embeds, alerts, log buffering
  discord_bot.py                — discord.py slash command bot
  db.py                         — trade log, balance history, tax summary (SQLite)
  futures_engine.py             — futures loop: 15m signal thread + 60s risk/funding thread
  futures_simulator.py          — futures position management (simulation + live)
  futures_exchange.py           — Binance futures API wrapper
  futures_risk.py               — FuturesPosition dataclass, liquidation price, PnL
web/
  app.py                        — Flask dashboard API (spot + futures endpoints)
  templates/
    index.html                  — single-page dashboard (Spot / Futures tabs)
data/
  trades.db                     — trade history and candle cache (all modes)
  spot_state_simulation.json    — spot simulation state
  spot_state_live.json          — spot live state
  futures_state_simulation.json — futures simulation state
  futures_state_live.json       — futures live state
  spot_live_since.txt           — timestamp of first spot live mode start
backtest.py                     — spot backtest and parameter sweep tool
backtest_futures.py             — futures backtest and parameter sweep tool
start.sh / stop.sh
```
