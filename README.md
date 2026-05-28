# Kairos

RSI + EMA200 trend-following bot for Binance EUR pairs. Runs in simulation or live mode. Includes a web dashboard and unified backtest tool.

## Strategy

- **Buy** when RSI < 30 and price is above EMA200
- **Exit** via trailing stop with a profit floor (floor dominates until price runs far enough above entry)
- **DCA** averages down once per position when price drops ≥1% from entry
- **Take-profit** at +5% acts as a spike catcher; rarely fires in practice
- Per-pair RSI periods and sell thresholds (ETHEUR uses RSI(7)/sell>65, SOLEUR uses RSI(14)/sell>75)

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
| `TRADING_PAIRS` | ETHEUR, SOLEUR | Hardcoded in `bot/config.py` |
| `INTERVAL` | `15m` | Candle interval |
| `POSITION_SIZE_PCT` | `0.75` | Fraction of balance per trade |
| `DCA_DROP_PCT` | `0.01` | Drop from entry to trigger DCA |
| `DCA_SIZE_PCT` | `0.75` | Fraction of balance for DCA buy |
| `TAKE_PROFIT_PCT` | `0.05` | Take-profit target (+5%) |
| `TRAILING_STOP_PCT` | `0.05` | Trailing stop distance |
| `PROFIT_FLOOR_PCT` | `0.03` | Minimum profit before stop can fire |
| `RSI_PERIOD` | `14` | Default RSI period |
| `RSI_PERIOD_ETHEUR` | `7` | Per-pair override |
| `RSI_OVERSOLD` | `30` | Buy threshold |
| `RSI_OVERBOUGHT` | `75` | Sell threshold (default) |
| `RSI_OVERBOUGHT_ETHEUR` | `65` | Per-pair override |
| `BINANCE_API_KEY` | | Required for live mode |
| `BINANCE_SECRET_KEY` | | Required for live mode |
| `DISCORD_WEBHOOK_URL` | | Optional trade alerts |

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
  config.py       — settings loaded from .env, per-pair helpers
  engine.py       — main loop, signal execution, Discord tick summaries
  simulator.py    — position management for both simulation and live mode
  exchange.py     — Binance API wrapper (klines, prices, orders, balance)
  strategy.py     — RSI + EMA200 signal computation
  candles.py      — local SQLite candle cache with incremental sync
  risk.py         — Position dataclass, DCA, trailing stop, PnL
  notifier.py     — Discord webhooks and log buffering
  db.py           — trade log and tax summary (SQLite)
web/
  app.py          — Flask dashboard API
  templates/
    index.html    — single-page dashboard
data/
  trades.db       — trade history (simulation + live, tagged by mode)
  state.json      — simulation state (balance, open positions)
  state_live.json — live state (open positions; balance fetched from Binance)
backtest.py       — unified backtest and sweep tool
start.sh / stop.sh
```
