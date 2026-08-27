# Getting Started — Fresh Linux Setup

This guide assumes a fresh Ubuntu 22.04 / Debian 12 server (or a Raspberry Pi running Raspberry Pi OS). If you're on macOS, skip to step 3 — Python and git are already available.

---

## Step 1 — Install system dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
```

Verify Python is 3.10 or newer:

```bash
python3 --version
# Should print: Python 3.10.x or higher
```

---

## Step 2 — Get the code

```bash
git clone <repo-url> kairos
cd kairos
```

---

## Step 3 — Create the virtual environment and install packages

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

This takes 1–2 minutes. You never need to "activate" the venv — all commands use `.venv/bin/python` directly.

---

## Step 4 — Configure

```bash
cp .env.example .env
nano .env       # or: vi .env  /  gedit .env  / any editor you prefer
```

**Minimum changes for a first simulation run (no API keys needed):**

1. Set `SPOT_TRADING_PAIRS` to the pairs you want to watch, e.g. `SOLEUR,ETHEUR`
2. Leave `SPOT_MODE=simulation` — this paper-trades with virtual money, no real orders
3. Leave `FUTURES_ENABLED=false` unless you specifically want futures

Everything else has sensible defaults. You can tune parameters later once you see how the bot behaves.

> **Important:** Pair names must be exact Binance symbols — `SOLEUR` not `SOL/EUR` or `SOL-EUR`. Check https://www.binance.com/en/markets/spot for the correct format.

---

## Step 5 — Open the dashboard port

The dashboard runs on port 8888 by default. If your server has a firewall (it should), allow that port:

```bash
sudo ufw allow 8888
sudo ufw status    # confirm it shows "8888  ALLOW"
```

If you're running locally (same machine as your browser) you can skip this step.

---

## Step 6 — Start

```bash
./start.sh
```

Expected output:

```
Engine started (PID 12345)
Dashboard started (PID 12346)
Dashboard: http://192.168.1.x:8888
Engine log:    tail -f data/kairos.log
Dashboard log: tail -f data/dashboard.log
Stop:          ./stop.sh
```

Open the dashboard URL in your browser from any device on the same network.

---

## Step 7 — Verify it's running

The dashboard will show a yellow **"Waiting for first tick"** notice with the time of the next 15-minute candle. This is normal — the bot aligns to candle boundaries before doing anything. **Wait up to 15 minutes.**

Once the first tick fires you'll see:
- The status badge turns green
- RSI and price values appear
- The engine log shows `[TICK] Prices —`

To watch the log live:

```bash
tail -f data/kairos.log
```

To check whether both processes are running:

```bash
./status.sh
```

---

## Step 8 — Going live (when you're ready)

See the **Go live** section in [REFERENCE.md](REFERENCE.md) for the full Binance API key setup. Only do this after you've watched the simulation run for a while and are comfortable with how it behaves.

The short version:
1. Get Binance API keys (Reading + Spot Trading only — no withdrawal permissions)
2. Add them to `.env`
3. Set `SPOT_MODE=live`
4. `./stop.sh && ./start.sh`

---

## Common problems

**Dashboard not reachable from another device**
- Check `ufw allow 8888` was run
- Check `WEB_HOST=0.0.0.0` in `.env` (not `127.0.0.1`)
- Make sure you're using the server's LAN IP, not `localhost`

**"No module named X" on start**
- Run `.venv/bin/pip install -r requirements.txt` again
- Make sure you're running `./start.sh` from inside the `kairos/` directory

**Bot appears to do nothing for 15 minutes**
- This is normal — it's waiting for the next candle boundary
- Check `tail -f data/kairos.log` and look for `Waiting Xs to align to next 15m candle`
- If that line is there, everything is fine

**"Permission denied" running start.sh / stop.sh**
```bash
chmod +x start.sh stop.sh status.sh
```

**Restarting after a `.env` change**
The bot reads `.env` only on startup. Any change requires a restart:
```bash
./stop.sh && ./start.sh
```
