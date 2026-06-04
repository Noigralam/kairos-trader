# Kairos

RSI + EMA200 trend-following bot for Binance. Runs in simulation or live mode with two independent engines:
- **Spot** — EUR pairs (ETHEUR, SOLEUR), 15m candles
- **Futures** — USDT-M perpetuals (ETHUSDT, SOLUSDT), 15m candles, configurable leverage

Includes a web dashboard, Discord integration, and backtest tools for both engines.

## Strategy

### Spot
- **Buy** when RSI drops below the oversold threshold (default 30) and price is above EMA200
- **Exit via trailing stop** with a profit floor — the stop rises once price crosses the floor above entry
- **Min-exit guard** prevents a sell below a configurable profit threshold (default +2%)
- **DCA** averages down once per position when price drops ≥4% from entry
- **Take-profit** at +10% acts as a spike catcher
- Per-pair RSI and overbought thresholds (e.g. `RSI_PERIOD_ETHEUR=7`)

### Futures
- Same RSI + EMA signal as spot, on 15m candles
- Isolated-margin LONG only with configurable leverage (default 2×)
- Exits via **take-profit**, **trailing stop** (with profit floor), or **liquidation** check
- DCA averages down once when price drops 1% from entry
- Funding cost applied every 8h on open positions
- Separate fast-check loop runs every 60s for risk management (stop/TP/liquidation) without waiting for the next 15m candle

## Requirements

- Python 3.10+
- Dependencies: `flask`, `python-binance`, `pandas`, `python-dotenv`, `requests`, `discord.py`

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in API keys and settings
```

### Binance API keys (live mode only)

Simulation mode requires no API keys. For live trading:

1. Log in to Binance → Profile → API Management → Create API
2. Choose **System generated**
3. Enable exactly these permissions:
   - ✅ Enable Reading
   - ✅ Enable Spot & Margin Trading
   - ✅ Enable Futures (if using futures live mode)
   - ❌ Everything else (no withdrawals, no transfers)
4. Restrict access to your server's IP address for extra safety
5. Copy the key and secret into `.env` as `BINANCE_API_KEY` and `BINANCE_SECRET_KEY`

## Running

```bash
./start.sh     # start bot + dashboard
./stop.sh      # stop
tail -f data/bot.log
```

Dashboard: `http://localhost:8888`

## Modes

Set `MODE` / `FUTURES_MODE` in `.env`:

- `simulation` — paper trades, tracks virtual balance, no API keys needed
- `live` — places real orders, reads balance from exchange

Spot and futures are completely independent: separate balances, separate state files, separate trade logs. Switching one mode never affects the other.

## Configuration (`.env`)

### Spot

| Variable | Default | Description |
|---|---|---|
| `MODE` | `simulation` | `simulation` or `live` |
| `TRADING_PAIRS` | `ETHEUR,SOLEUR` | Comma-separated Binance EUR spot pairs |
| `SIMULATION_BALANCE` | `200.0` | Starting balance for simulation (EUR) |
| `INTERVAL` | `15m` | Candle interval |
| `POSITION_SIZE_PCT` | `0.75` | Fraction of balance per trade |
| `DCA_DROP_PCT` | `0.04` | Drop from entry to trigger DCA |
| `DCA_SIZE_PCT` | `0.50` | Fraction of balance for DCA buy |
| `TAKE_PROFIT_PCT` | `0.10` | Take-profit target |
| `TRAILING_STOP_PCT` | `0.05` | Trailing stop distance |
| `PROFIT_FLOOR_PCT` | `0.03` | Min profit before trailing stop can fire |
| `MIN_EXIT_PROFIT_PCT` | `0.02` | Min profit before a signal sell can fire |
| `RSI_PERIOD` | `14` | Default RSI period |
| `RSI_OVERSOLD` | `30` | Buy threshold |
| `RSI_OVERBOUGHT` | `75` | Default sell threshold |
| `BINANCE_API_KEY` | | Required for live mode |
| `BINANCE_SECRET_KEY` | | Required for live mode |
| `DISCORD_WEBHOOK_URL` | | Trade alerts and tick embeds |
| `DISCORD_BOT_TOKEN` | | Slash command bot (optional) |
| `DISCORD_GUILD_ID` | | Guild ID for instant slash command sync (optional) |
| `WEB_HOST` | `127.0.0.1` | Dashboard bind address (`0.0.0.0` to expose on LAN) |
| `WEB_PORT` | `8888` | Dashboard port |

Per-pair overrides: append the pair name, e.g. `RSI_PERIOD_ETHEUR=7`, `RSI_OVERBOUGHT_SOLEUR=80`.

### Futures

| Variable | Default | Description |
|---|---|---|
| `FUTURES_ENABLED` | `false` | Enable futures engine |
| `FUTURES_MODE` | `simulation` | `simulation` or `live` |
| `FUTURES_TRADING_PAIRS` | `ETHUSDT,SOLUSDT` | USDT-M perpetual pairs |
| `FUTURES_SIMULATION_BALANCE` | `200.0` | Starting balance (USDT) |
| `FUTURES_LEVERAGE` | `2` | Leverage multiplier |
| `FUTURES_MARGIN_TYPE` | `ISOLATED` | `ISOLATED` or `CROSSED` |
| `FUTURES_POSITION_SIZE_PCT` | `0.75` | Fraction of balance per trade (margin) |
| `FUTURES_DCA_DROP_PCT` | `0.01` | Drop from entry to trigger DCA |
| `FUTURES_DCA_SIZE_PCT` | `0.75` | Fraction of balance for DCA |
| `FUTURES_TAKE_PROFIT_PCT` | `0.05` | Take-profit target |
| `FUTURES_TRAILING_STOP_PCT` | `0.05` | Trailing stop distance |
| `FUTURES_PROFIT_FLOOR_PCT` | `0.03` | Min profit before trailing stop can fire |
| `FUTURES_MIN_EXIT_PROFIT_PCT` | `0.02` | Min profit before signal sell fires |
| `FUTURES_STOP_CHECK_INTERVAL` | `60` | Risk check frequency (seconds) |
| `FUTURES_RSI_PERIOD` | `7` | Default RSI period for futures |
| `FUTURES_RSI_OVERSOLD` | `30` | Buy threshold |
| `FUTURES_RSI_OVERBOUGHT` | `75` | Sell threshold |
| `FUTURES_EMA_GAP_PCT` | `0.02` | Min % above EMA200 to allow entry |

Per-pair overrides: `FUTURES_RSI_PERIOD_ETHUSDT=7`, etc.

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
# Pair comparison — solo vs combined, current settings
python backtest.py
python backtest.py 90                        # custom window

# Monthly top-up simulation
python backtest.py topup 200 25 365          # start=€200, +€25/month, 365 days

# Parameter sweeps
python backtest.py sweep trail               # trailing stop %
python backtest.py sweep floor               # profit floor %
python backtest.py sweep exit                # sell RSI + take-profit
python backtest.py sweep buyrsi              # buy RSI threshold
python backtest.py sweep dca                 # DCA drop %, size %
python backtest.py sweep lev                 # leverage
python backtest.py sweep all                 # everything

# Custom day windows
python backtest.py sweep trail 365 180 90
```

### Futures

```bash
# Baseline — current config
python backtest_futures.py
python backtest_futures.py 90

# Parameter sweeps (ETHUSDT + SOLUSDT)
python backtest_futures.py sweep trail       # trailing stop %
python backtest_futures.py sweep tp          # take-profit %
python backtest_futures.py sweep pos         # position size %
python backtest_futures.py sweep dca         # DCA drop % + size %
python backtest_futures.py sweep floor       # profit floor %
python backtest_futures.py sweep lev         # leverage
python backtest_futures.py sweep rsi         # RSI buy/sell thresholds
python backtest_futures.py sweep all         # everything

# Custom day windows
python backtest_futures.py sweep all 365 180 90
```

The futures backtest models: isolated margin, 0.05% taker fee, 0.01%/8h funding (longs), and liquidation at `entry × (1 − 1/lev + 0.5%)`.

## Project layout

```
bot/
  config.py          — settings from .env, per-pair helpers (spot + futures)
  engine.py          — spot trading loop, candle alignment, signal execution
  simulator.py       — spot position management (simulation + live)
  exchange.py        — Binance spot API wrapper
  strategy.py        — RSI + EMA200 signal computation (shared by spot + futures)
  candles.py         — local SQLite candle cache with incremental sync
  risk.py            — spot Position dataclass, DCA, trailing stop, PnL
  notifier.py        — Discord webhooks, tick embeds, alerts, log buffering
  discord_bot.py     — discord.py slash command bot
  db.py              — trade log, balance history, tax summary (SQLite)
  futures_engine.py  — futures trading loop (15m signal + 60s risk threads)
  futures_simulator.py — futures position management (simulation + live)
  futures_exchange.py — Binance futures API wrapper
  futures_risk.py    — FuturesPosition dataclass, liquidation, PnL
web/
  app.py             — Flask dashboard API (spot + futures endpoints)
  templates/
    index.html       — single-page dashboard (Spot / Futures tabs)
data/
  trades.db          — trade history (all modes, tagged by mode string)
  candles.db         — local candle cache (spot + futures pairs)
  state.json         — spot simulation state
  state_live.json    — spot live state
  futures_state.json — futures simulation/live state
  live_since.txt     — timestamp of first spot live mode start
backtest.py          — spot backtest and sweep tool
backtest_futures.py  — futures backtest and sweep tool
start.sh / stop.sh
```
