#!/bin/bash
cd "$(dirname "$0")"

ENGINE_PID="data/cairn.pid"
DASHBOARD_PID="data/dashboard.pid"

echo "=== Cairn status ==="
echo ""

# Engine
if [ -f "$ENGINE_PID" ] && kill -0 "$(cat "$ENGINE_PID")" 2>/dev/null; then
    PID=$(cat "$ENGINE_PID")
    UPTIME=$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ')
    echo "  Engine:    running  (PID $PID, up $UPTIME)"
else
    echo "  Engine:    stopped"
fi

# Dashboard
if [ -f "$DASHBOARD_PID" ] && kill -0 "$(cat "$DASHBOARD_PID")" 2>/dev/null; then
    PID=$(cat "$DASHBOARD_PID")
    UPTIME=$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ')
    PORT=$(grep WEB_PORT .env 2>/dev/null | cut -d= -f2)
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "  Dashboard: running  (PID $PID, up $UPTIME)"
    echo "  URL:       http://${IP}:${PORT:-8888}"
else
    echo "  Dashboard: stopped"
fi

echo ""

# Last 5 engine log lines
if [ -f "data/cairn.log" ]; then
    echo "=== Last engine log lines ==="
    tail -5 data/cairn.log
    echo ""
fi
