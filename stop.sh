#!/bin/bash
cd "$(dirname "$0")"

PID_FILE="data/bot.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found — bot may not be running"
    exit 0
fi

PID=$(cat "$PID_FILE")

if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    rm "$PID_FILE"
    echo "Crypto_Bot stopped (PID $PID)"
else
    echo "Bot is not running (stale PID $PID)"
    rm "$PID_FILE"
fi
