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


def get_trades(limit=50):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


def get_tax_summary():
    """Realized P&L grouped by year for Finnish Vero tax reporting."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT
            strftime('%Y', timestamp)                           AS year,
            SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END)        AS gains,
            SUM(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END)   AS losses,
            SUM(pnl)                                            AS net_pnl
        FROM trades
        WHERE pnl IS NOT NULL AND mode = 'live'
        GROUP BY year
        ORDER BY year DESC
    """).fetchall()
    conn.close()
    return rows
