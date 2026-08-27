#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

ENGINE_PID="data/kairos.pid"
DASHBOARD_PID="data/dashboard.pid"

mkdir -p data

TARGET="${1:-both}"

start_engine() {
    if [ -f "$ENGINE_PID" ] && kill -0 "$(cat "$ENGINE_PID")" 2>/dev/null; then
        echo "Engine already running (PID $(cat "$ENGINE_PID"))"
    else
        .venv/bin/python main.py > /dev/null 2>&1 &
        echo $! > "$ENGINE_PID"
        echo "Engine started (PID $(cat "$ENGINE_PID"))"
    fi
}

start_dashboard() {
    if [ -f "$DASHBOARD_PID" ] && kill -0 "$(cat "$DASHBOARD_PID")" 2>/dev/null; then
        echo "Dashboard already running (PID $(cat "$DASHBOARD_PID"))"
    else
        .venv/bin/python dashboard.py > /dev/null 2>&1 &
        echo $! > "$DASHBOARD_PID"
        echo "Dashboard started (PID $(cat "$DASHBOARD_PID"))"
    fi
}

case "$TARGET" in
    engine)    start_engine ;;
    dashboard) start_dashboard ;;
    both)      start_engine; start_dashboard ;;
    *)
        echo "Usage: $0 [engine|dashboard|both]"
        echo "  engine    — start trading engine only"
        echo "  dashboard — start web dashboard only"
        echo "  both      — start both (default)"
        exit 1
        ;;
esac

if [ "$TARGET" = "both" ] || [ "$TARGET" = "dashboard" ]; then
    echo "Dashboard: http://$(hostname -I | awk '{print $1}'):$(grep WEB_PORT .env | cut -d= -f2)"
fi
echo "Engine log:    tail -f data/kairos.log"
echo "Dashboard log: tail -f data/dashboard.log"
echo "Stop:          ./stop.sh"
