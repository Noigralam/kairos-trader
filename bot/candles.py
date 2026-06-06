"""
Local candle cache. Stores OHLCV data in trades.db and syncs incrementally.

Public API:
    sync(pair, interval)          — fetch new candles since last stored, called each tick
    initial_sync(pair, interval, days) — one-time bulk download
    get_df(pair, interval, limit) — read from local DB, returns DataFrame
"""
import sqlite3
import time
import logging
from datetime import datetime, timezone, timedelta

import pandas as pd

from .db import DB_PATH
from .spot_exchange import get_client

log = logging.getLogger("cryptobot")

CANDLES_PER_REQUEST = 1000


def _insert(conn: sqlite3.Connection, pair: str, interval: str, rows: list):
    conn.executemany(
        "INSERT OR REPLACE INTO candles (pair, interval, open_time, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (pair, interval, int(r[0]), float(r[1]), float(r[2]),
             float(r[3]), float(r[4]), float(r[5]))
            for r in rows
        ],
    )


def _last_open_time(pair: str, interval: str) -> int | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT MAX(open_time) FROM candles WHERE pair=? AND interval=?",
        (pair, interval),
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else None


def initial_sync(pair: str, interval: str, days: int = 365):
    """Bulk-download history. Safe to call again — skips already-stored candles."""
    client = get_client()
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    # skip candles we already have
    last = _last_open_time(pair, interval)
    if last is not None:
        start_ms = last + 1

    total = 0
    cursor = start_ms
    conn = sqlite3.connect(DB_PATH)
    log.info(f"[CANDLES] Bulk sync {pair}/{interval} from {datetime.fromtimestamp(start_ms/1000, tz=timezone.utc).date()}…")

    while cursor < end_ms:
        batch = client.get_klines(symbol=pair, interval=interval,
                                  startTime=cursor, limit=CANDLES_PER_REQUEST)
        if not batch:
            break
        _insert(conn, pair, interval, batch)
        total += len(batch)
        cursor = batch[-1][0] + 1
        time.sleep(0.08)  # stay well under Binance's 1200 weight/min rate limit

    conn.commit()
    conn.close()
    log.info(f"[CANDLES] {pair}/{interval} — stored {total} new candles")


def sync(pair: str, interval: str):
    """Fetch only candles newer than the last stored one. Call each tick."""
    last = _last_open_time(pair, interval)
    start_ms = (last + 1) if last is not None else None

    client = get_client()
    kwargs = dict(symbol=pair, interval=interval, limit=CANDLES_PER_REQUEST)
    if start_ms:
        kwargs["startTime"] = start_ms

    batch = client.get_klines(**kwargs)
    if not batch:
        return

    # Binance always returns the currently-open (incomplete) candle as the last entry.
    # Drop it so RSI/EMA are never computed on a partial candle.
    batch = batch[:-1]
    if not batch:
        return

    conn = sqlite3.connect(DB_PATH)
    _insert(conn, pair, interval, batch)
    conn.commit()
    conn.close()


def get_df(pair: str, interval: str, limit: int = 250) -> pd.DataFrame:
    """Read the most recent `limit` closed candles from local DB."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT open_time, open, high, low, close, volume FROM candles "
        "WHERE pair=? AND interval=? ORDER BY open_time DESC LIMIT ?",
        (pair, interval, limit),
    ).fetchall()
    conn.close()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows[::-1], columns=["open_time", "open", "high", "low", "close", "volume"])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df.reset_index(drop=True)
