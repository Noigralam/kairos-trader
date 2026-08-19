import sqlite3
import os
import zoneinfo
from datetime import datetime

_TZ = zoneinfo.ZoneInfo("Europe/Helsinki")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS balance_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            balance     REAL    NOT NULL,
            mode        TEXT    NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            pair        TEXT    NOT NULL,
            side        TEXT    NOT NULL,
            price       REAL    NOT NULL,
            amount      REAL    NOT NULL,
            value_eur   REAL    NOT NULL,
            fee         REAL    NOT NULL,
            mode        TEXT    NOT NULL,
            pnl         REAL,
            notes       TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            pair        TEXT    NOT NULL,
            interval    TEXT    NOT NULL,
            open_time   INTEGER NOT NULL,
            open        REAL    NOT NULL,
            high        REAL    NOT NULL,
            low         REAL    NOT NULL,
            close       REAL    NOT NULL,
            volume      REAL    NOT NULL,
            PRIMARY KEY (pair, interval, open_time)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles (pair, interval, open_time)")
    conn.commit()
    conn.close()


def log_trade(pair, side, price, amount, value_eur, fee, mode, pnl=None, notes=None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO trades (timestamp, pair, side, price, amount, value_eur, fee, mode, pnl, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now(tz=_TZ).isoformat(), pair, side, price, amount, value_eur, fee, mode, pnl, notes))
    conn.commit()
    conn.close()


def get_hold_times(mode: str) -> dict:
    """Returns {sell_trade_id: hold_hours} by matching BUY→SELL pairs chronologically."""
    conn = sqlite3.connect(DB_PATH)
    buys  = conn.execute(
        "SELECT timestamp, pair, notes FROM trades WHERE mode=? AND side='BUY' ORDER BY timestamp",
        (mode,)
    ).fetchall()
    sells = conn.execute(
        "SELECT id, timestamp, pair FROM trades WHERE mode=? AND side='SELL' ORDER BY timestamp",
        (mode,)
    ).fetchall()
    conn.close()

    buys_by_pair: dict = {}
    for ts, pair, notes in buys:
        if 'dca' not in (notes or '').lower():
            buys_by_pair.setdefault(pair, []).append(ts)

    result: dict = {}
    for tid, ts, pair in sells:
        if buys_by_pair.get(pair):
            buy_ts = buys_by_pair[pair].pop(0)
            try:
                from datetime import datetime as _dt
                b = _dt.fromisoformat(buy_ts)
                s = _dt.fromisoformat(ts)
                result[tid] = round((s - b).total_seconds() / 3600, 1)
            except Exception:
                pass
    return result


def get_trades(limit=50, mode: str = None, offset: int = 0):
    conn = sqlite3.connect(DB_PATH)
    if mode:
        rows = conn.execute(
            "SELECT * FROM trades WHERE mode = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (mode, limit, offset)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
    conn.close()
    return rows


def get_trade_count(mode: str = None) -> int:
    conn = sqlite3.connect(DB_PATH)
    if mode:
        row = conn.execute("SELECT COUNT(*) FROM trades WHERE mode = ?", (mode,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
    conn.close()
    return row[0] if row else 0


def log_balance(balance: float, mode: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO balance_history (timestamp, balance, mode) VALUES (?, ?, ?)",
        (datetime.now(tz=_TZ).strftime("%Y-%m-%d %H:%M:%S"), balance, mode)
    )
    conn.commit()
    conn.close()


def get_starting_balance(mode: str) -> float | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT balance FROM balance_history WHERE mode=? ORDER BY timestamp ASC LIMIT 1", (mode,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_balance_history(mode: str, days: float = 30):
    conn = sqlite3.connect(DB_PATH)
    if days > 0:
        hours = days * 24
        modifier = f'-{hours} hours'
        rows = conn.execute("""
            SELECT timestamp, balance FROM balance_history
            WHERE mode = ? AND timestamp >= datetime('now', ?)
            ORDER BY timestamp ASC
        """, (mode, modifier)).fetchall()
    else:
        rows = conn.execute("""
            SELECT timestamp, balance FROM balance_history
            WHERE mode = ? ORDER BY timestamp ASC
        """, (mode,)).fetchall()
    conn.close()
    return rows


def get_trade_stats(mode: str):
    conn = sqlite3.connect(DB_PATH)
    sell_rows = conn.execute("""
        SELECT pnl, timestamp, pair
        FROM trades WHERE mode = ? AND side = 'SELL' AND pnl IS NOT NULL
    """, (mode,)).fetchall()
    buy_rows_raw = conn.execute("""
        SELECT timestamp, pair, notes
        FROM trades WHERE mode = ? AND side = 'BUY'
    """, (mode,)).fetchall()
    conn.close()

    sells = sell_rows
    if not sells:
        return None

    pnls       = [s[0] for s in sells]
    wins       = [p for p in pnls if p > 0]
    losses     = [p for p in pnls if p <= 0]
    best       = max(pnls)
    worst      = min(pnls)
    avg_pnl    = sum(pnls) / len(pnls)
    win_rate   = len(wins) / len(pnls) * 100

    # avg hold time: match BUY→SELL pairs per pair
    hold_times   = []
    buys_by_pair = {}
    for ts, pair, notes in buy_rows_raw:
        if 'dca' not in (notes or '').lower():
            buys_by_pair.setdefault(pair, []).append(ts)
    for _, ts, pair in sells:
        if buys_by_pair.get(pair):
            buy_ts = buys_by_pair[pair].pop(0)
            try:
                from datetime import datetime as dt
                b = dt.fromisoformat(buy_ts)
                s = dt.fromisoformat(ts)
                hold_times.append((s - b).total_seconds() / 3600)
            except Exception:
                pass

    avg_hold_h = sum(hold_times) / len(hold_times) if hold_times else None

    return {
        "total":      len(pnls),
        "wins":       len(wins),
        "losses":     len(losses),
        "win_rate":   round(win_rate, 1),
        "avg_pnl":    round(avg_pnl, 2),
        "best":       round(best, 2),
        "worst":      round(worst, 2),
        "avg_hold_h": round(avg_hold_h, 1) if avg_hold_h is not None else None,
    }


def get_tax_summary(mode="live"):
    """Realized P&L grouped by year for tax reporting."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT
            strftime('%Y', timestamp)                           AS year,
            SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END)        AS gains,
            SUM(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END)   AS losses,
            SUM(pnl)                                            AS net_pnl
        FROM trades
        WHERE pnl IS NOT NULL AND mode = ?
        GROUP BY year
        ORDER BY year DESC
    """, (mode,)).fetchall()
    conn.close()
    return rows
