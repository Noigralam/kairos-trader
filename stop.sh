#!/bin/bash
cd "$(dirname "$0")"

PID_FILE="data/bot.pid"

# Kill the PID-file process first
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "Crypto_Bot stopped (PID $PID)"
    else
        echo "Stale PID $PID in pid file"
    fi
    rm "$PID_FILE"
fi

# Kill any remaining main.py processes (handles out-of-sync pid file)
LEFTOVER=$(pgrep -f "python.*main\.py" 2>/dev/null)
if [ -n "$LEFTOVER" ]; then
    echo "Killing leftover process(es): $LEFTOVER"
    pkill -f "python.*main\.py"
fi
