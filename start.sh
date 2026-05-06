#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

echo "Starting Crypto_Bot..."
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):$(grep WEB_PORT .env | cut -d= -f2)"
echo "Logs:      tail -f data/bot.log"
echo "Stop:      Ctrl+C"
echo ""

.venv/bin/python main.py
