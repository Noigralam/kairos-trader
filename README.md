# Kairos

RSI + EMA200 trend-following bot for Binance EUR pairs. Runs in simulation or live mode. Includes a web dashboard, Discord integration, and a unified backtest tool.

## Strategy

- **Buy** when RSI drops below the oversold threshold (default 30) and price is above EMA200
- **Exit via trailing stop** with a profit floor — the floor dominates until price runs far enough above entry
- **Min-exit guard** prevents a signal-based sell below a configurable profit threshold (default +2%)
- **DCA** averages down once per position when price drops ≥1% from entry and RSI confirms weakness
- **Take-profit** at +5% acts as a spike catcher
- Per-pair RSI periods and overbought thresholds: ETHEUR uses RSI(7)/sell>65, SOLEUR uses RSI(7)/sell>80

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in API keys and settings
```

## Running

```bash
./start.sh     # start bot + dashboard
./stop.sh      # stop
tail -f data/bot.log
```

Dashboard: `http://localhost:8888`

## Modes

Set `MODE` in `.env`:

- `simulation` — paper trades, tracks virtual balance, writes to `data/state.json`
- `live` — places real Binance market orders, reads balance from exchange, writes to `data/state_live.json`

Switching modes never touches the other mode's state file. Trade history is tagged by mode in `data/trades.db`.

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `MODE` | `simulation` | `simulation` or `live` |
| `SIMULATION_BALANCE` | `200.0` | Starting balance for simulation (EUR) |
| `INTERVAL` | `15m` | Candle interval |
| `POSITION_SIZE_PCT` | `0.75` | Fraction of balance per trade |
| `DCA_DROP_PCT` | `0.01` | Drop from entry to trigger DCA |
| `DCA_SIZE_PCT` | `0.75` | Fraction of balance for DCA buy |
| `TAKE_PROFIT_PCT` | `0.05` | Take-profit target (+5%) |
| `TRAILING_STOP_PCT` | `0.05` | Trailing stop distance |
| `PROFIT_FLOOR_PCT` | `0.03` | Minimum profit before trailing stop can fire |
| `MIN_EXIT_PROFIT_PCT` | `0.02` | Minimum profit before a sell signal can close a position |
| `RSI_PERIOD` | `14` | Default RSI period |
| `RSI_OVERSOLD` | `30` | Buy threshold |
| `RSI_OVERBOUGHT` | `75` | Default sell threshold |
| `BINANCE_API_KEY` | | Required for live mode |
| `BINANCE_SECRET_KEY` | | Required for live mode |
| `DISCORD_WEBHOOK_URL` | | Trade alerts and tick embeds |
| `DISCORD_BOT_TOKEN` | | Slash command bot (optional) |
| `DISCORD_GUILD_ID` | | Guild ID for instant slash command sync (optional) |

Per-pair overrides are supported for `RSI_PERIOD`, `RSI_OVERBOUGHT`, and `MIN_EXIT_PROFIT_PCT` by appending the pair name (e.g. `RSI_PERIOD_ETHEUR=7`).

## Discord

When `DISCORD_WEBHOOK_URL` is set, the bot posts tick embeds, trade alerts, extreme condition alerts (RSI < 20 / > 85, EMA200 crossovers), and a daily summary.

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

```bash
# Pair comparison — solo vs combined, current settings
python backtest.py
python backtest.py 90                        # custom window

# Monthly top-up simulation
python backtest.py topup 200 25 365          # start=€200, +€25/month, 365 days

# Parameter sweeps (run on both pairs)
python backtest.py sweep exit                # sell RSI + take-profit
python backtest.py sweep floor               # profit floor %
python backtest.py sweep trail               # trailing stop %
python backtest.py sweep buyrsi              # buy RSI threshold
python backtest.py sweep dca                 # DCA drop %, size %
python backtest.py sweep all                 # everything

# Custom day windows
python backtest.py sweep exit 365 180 90
```

## Project layout

```
bot/
  config.py        — settings loaded from .env, per-pair helpers
  engine.py        — main loop, candle alignment, signal execution
  simulator.py     — position management for simulation and live mode
  exchange.py      — Binance API wrapper (klines, prices, orders, balance)
  strategy.py      — RSI + EMA200 signal computation
  candles.py       — local SQLite candle cache with incremental sync
  risk.py          — Position dataclass, DCA, trailing stop, PnL
  notifier.py      — Discord webhooks, tick embeds, alerts, log buffering
  discord_bot.py   — discord.py slash command bot
  db.py            — trade log, balance history, tax summary (SQLite)
web/
  app.py           — Flask dashboard API
  templates/
    index.html     — single-page dashboard
data/
  trades.db        — trade history (simulation + live, tagged by mode)
  candles.db       — local candle cache
  state.json       — simulation state (balance, open positions)
  state_live.json  — live state (open positions; balance fetched from Binance)
  live_since.txt   — timestamp of first live mode start
backtest.py        — unified backtest and sweep tool
start.sh / stop.sh
```
