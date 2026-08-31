#!/bin/bash
cd "$(dirname "$0")"

ENGINE_PID="data/cairn.pid"
DASHBOARD_PID="data/dashboard.pid"

TARGET="${1:-both}"

stop_engine() {
    if [ -f "$ENGINE_PID" ]; then
        PID=$(cat "$ENGINE_PID")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID"
            echo "Engine stopped (PID $PID)"
        else
            echo "Stale engine PID $PID"
        fi
        rm "$ENGINE_PID"
    fi
    LEFTOVER=$(pgrep -f "python.*main\.py" 2>/dev/null)
    if [ -n "$LEFTOVER" ]; then
        echo "Killing leftover engine process(es): $LEFTOVER"
        pkill -f "python.*main\.py"
    fi
}

stop_dashboard() {
    if [ -f "$DASHBOARD_PID" ]; then
        PID=$(cat "$DASHBOARD_PID")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID"
            echo "Dashboard stopped (PID $PID)"
        else
            echo "Stale dashboard PID $PID"
        fi
        rm "$DASHBOARD_PID"
    fi
    LEFTOVER=$(pgrep -f "python.*dashboard\.py" 2>/dev/null)
    if [ -n "$LEFTOVER" ]; then
        echo "Killing leftover dashboard process(es): $LEFTOVER"
        pkill -f "python.*dashboard\.py"
    fi
}

case "$TARGET" in
    engine)    stop_engine ;;
    dashboard) stop_dashboard ;;
    both)      stop_engine; stop_dashboard ;;
    *)
        echo "Usage: $0 [engine|dashboard|both]"
        echo "  engine    — stop trading engine only"
        echo "  dashboard — stop web dashboard only"
        echo "  both      — stop both (default)"
        exit 1
        ;;
esac
