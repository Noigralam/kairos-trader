#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

PID_FILE="data/bot.pid"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Bot is already running (PID $(cat "$PID_FILE"))"
    exit 0
fi

mkdir -p data
.venv/bin/python main.py > /dev/null 2>&1 &
echo $! > "$PID_FILE"

echo "Crypto_Bot started (PID $(cat "$PID_FILE"))"
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):$(grep WEB_PORT .env | cut -d= -f2)"
echo "Logs:      tail -f data/bot.log"
echo "Stop:      ./stop.sh"
