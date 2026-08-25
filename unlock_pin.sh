#!/bin/bash
# Unlock PIN lockouts. Usage:
#   ./unlock_pin.sh          — clear all locked IPs
#   ./unlock_pin.sh 1.2.3.4  — clear a specific IP

cd "$(dirname "$0")"
LOCKFILE="data/pin_lockouts.json"

if [ ! -f "$LOCKFILE" ]; then
    echo "No lockout file found — nothing to unlock."
    exit 0
fi

if [ -n "$1" ]; then
    .venv/bin/python -c "
import json, sys
ip = sys.argv[1]
with open('$LOCKFILE') as f:
    data = json.load(f)
if ip in data:
    del data[ip]
    with open('$LOCKFILE', 'w') as f:
        json.dump(data, f)
    print(f'Unlocked {ip}')
else:
    print(f'{ip} was not locked')
" "$1"
else
    echo "{}" > "$LOCKFILE"
    echo "All PIN lockouts cleared."
fi
