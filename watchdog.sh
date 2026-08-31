#!/bin/bash
cd "$(dirname "$0")"

PID_FILE="data/cairn.pid"
ENV_FILE=".env"

# load DISCORD_WEBHOOK_URL from .env
DISCORD_WEBHOOK_URL=$(grep -E '^DISCORD_WEBHOOK_URL=' "$ENV_FILE" | cut -d= -f2-)

alert() {
    local msg="$1"
    if [ -n "$DISCORD_WEBHOOK_URL" ]; then
        curl -s -X POST "$DISCORD_WEBHOOK_URL" \
            -H "Content-Type: application/json" \
            -d "{\"content\": \"$msg\"}" > /dev/null
    fi
}

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

if ! is_running; then
    alert "⚠️ **Cairn is down** — bot process not found. Attempting restart..."
    bash start.sh
    sleep 5
    if is_running; then
        alert "✅ **Cairn restarted** successfully (PID $(cat "$PID_FILE"))."
    else
        alert "❌ **Cairn restart failed** — manual intervention required."
    fi
fi
