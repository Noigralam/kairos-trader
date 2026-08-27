"""
Finnish capital gains tax module — FIFO cost basis tracking.

Scope: spot live trades only (sell-for-fiat EUR pairs on Binance = foreign provider).
Finnish Vero rules: FIFO matching, separate gain/loss totals, annual reporting.

Note: These figures cover bot trades only. Capital gains/losses must be combined
with all other capital income (e.g. equity trading) when applying the shared
€1,000 annual deduction allowance (TVL 50 §).
"""
import csv
import io
import sqlite3

from bot.db import DB_PATH


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS tax_lots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset       TEXT    NOT NULL,
    pair        TEXT    NOT NULL,
    acquired_at TEXT    NOT NULL,
    quantity    REAL    NOT NULL,
    cost_eur    REAL    NOT NULL,
    remaining   REAL    NOT NULL,
    exchange    TEXT    NOT NULL DEFAULT 'Binance',
    provider    TEXT    NOT NULL DEFAULT 'foreign',
    trade_id    INTEGER
);

CREATE TABLE IF NOT EXISTS tax_disposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset           TEXT    NOT NULL,
    pair            TEXT    NOT NULL,
    disposed_at     TEXT    NOT NULL,
    quantity        REAL    NOT NULL,
    proceeds_eur    REAL    NOT NULL,
    cost_basis_eur  REAL    NOT NULL,
    gain_loss_eur   REAL    NOT NULL,
    lot_id          INTEGER,
    lot_acquired_at TEXT    NOT NULL,
    exchange        TEXT    NOT NULL DEFAULT 'Binance',
    provider        TEXT    NOT NULL DEFAULT 'foreign',
    trade_id        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tax_lots_fifo     ON tax_lots(asset, acquired_at);
CREATE INDEX IF NOT EXISTS idx_tax_lots_remaining ON tax_lots(asset, remaining);
CREATE INDEX IF NOT EXISTS idx_tax_disposals_year ON tax_disposals(disposed_at);
"""


def init_schema():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _asset(pair: str) -> str:
    """'SOLEUR' → 'SOL', 'ETHUSDT' → 'ETH'"""
    for suffix in ("EUR", "USDT", "USDC", "BTC"):
        if pair.endswith(suffix):
            return pair[: -len(suffix)]
    return pair


def _fee_to_eur(side: str, fee: float, price: float, amount: float) -> float:
    """
    Binance deducts BUY fees from received asset (SOL), SELL fees from EUR.
    Heuristic: if fee < 1% of amount, it's in asset units → convert to EUR.
    """
    if side == "BUY" and fee < amount * 0.01:
        return fee * price
    return fee


# ---------------------------------------------------------------------------
# Core: add lot / dispose FIFO
# ---------------------------------------------------------------------------

def _add_lot(conn, trade_id, asset, pair, acquired_at, quantity, cost_eur):
    conn.execute(
        "INSERT INTO tax_lots (asset, pair, acquired_at, quantity, cost_eur, remaining, trade_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (asset, pair, acquired_at, quantity, cost_eur, quantity, trade_id),
    )


def _dispose_fifo(conn, trade_id, asset, pair, disposed_at, quantity, proceeds_eur_gross, fee_eur):
    """
    FIFO-match `quantity` of `asset` against oldest lots.
    Creates one tax_disposals row per lot consumed.
    proceeds_eur_gross: raw EUR received before fee deduction.
    fee_eur: disposal fee in EUR (reduces proceeds proportionally per lot).
    """
    proceeds_net = proceeds_eur_gross - fee_eur

    lots = conn.execute(
        "SELECT id, acquired_at, quantity, cost_eur, remaining "
        "FROM tax_lots WHERE asset = ? AND remaining > 1e-9 ORDER BY acquired_at ASC",
        (asset,),
    ).fetchall()

    remaining_to_sell = quantity
    total_lots_used = 0.0

    for lot_id, lot_acquired_at, lot_qty, lot_cost_eur, lot_remaining in lots:
        if remaining_to_sell <= 1e-9:
            break

        take = min(lot_remaining, remaining_to_sell)
        fraction = take / quantity

        # cost basis proportional to units taken from this lot
        cost_per_unit = lot_cost_eur / lot_qty
        cost_basis = take * cost_per_unit

        # proceeds proportional to units taken from this lot
        lot_proceeds = proceeds_net * fraction

        gain_loss = lot_proceeds - cost_basis

        conn.execute(
            "INSERT INTO tax_disposals "
            "(asset, pair, disposed_at, quantity, proceeds_eur, cost_basis_eur, "
            " gain_loss_eur, lot_id, lot_acquired_at, trade_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (asset, pair, disposed_at, take, round(lot_proceeds, 6),
             round(cost_basis, 6), round(gain_loss, 6),
             lot_id, lot_acquired_at, trade_id),
        )

        conn.execute(
            "UPDATE tax_lots SET remaining = remaining - ? WHERE id = ?",
            (take, lot_id),
        )

        remaining_to_sell -= take
        total_lots_used += take

    if remaining_to_sell > 1e-4:
        # Should not happen if lot ledger is complete
        conn.execute(
            "INSERT INTO tax_disposals "
            "(asset, pair, disposed_at, quantity, proceeds_eur, cost_basis_eur, "
            " gain_loss_eur, lot_id, lot_acquired_at, trade_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'unknown', ?)",
            (asset, pair, disposed_at, remaining_to_sell,
             proceeds_net * (remaining_to_sell / quantity),
             0.0, proceeds_net * (remaining_to_sell / quantity),
             trade_id),
        )

    # Zero out micro-residuals left by Binance SOL-fee deductions (gross lot qty
    # slightly exceeds net wallet balance sold). These were never in the wallet.
    conn.execute(
        "UPDATE tax_lots SET remaining = 0 WHERE asset = ? AND remaining < 0.02",
        (asset,),
    )


# ---------------------------------------------------------------------------
# Rebuild from history
# ---------------------------------------------------------------------------

def rebuild():
    """
    Wipe tax_lots and tax_disposals, then reprocess all live spot trades
    chronologically to reconstruct the FIFO ledger.
    """
    conn = sqlite3.connect(DB_PATH)
    init_schema()

    conn.execute("DELETE FROM tax_disposals")
    conn.execute("DELETE FROM tax_lots")
    conn.commit()

    trades = conn.execute(
        "SELECT id, timestamp, pair, side, price, amount, value_eur, fee "
        "FROM trades WHERE mode = 'live' ORDER BY timestamp ASC"
    ).fetchall()

    for trade_id, timestamp, pair, side, price, amount, value_eur, fee in trades:
        asset = _asset(pair)
        fee_eur = _fee_to_eur(side, fee, price, amount)

        if side == "BUY":
            _add_lot(conn, trade_id, asset, pair, timestamp, amount, value_eur + fee_eur)
        elif side == "SELL":
            _dispose_fifo(conn, trade_id, asset, pair, timestamp, amount, value_eur, fee_eur)

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def annual_summary():
    """
    Returns list of dicts, one per year, with FIFO-based gains/losses.
    Gains and losses are reported separately per Finnish Vero requirements.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT
            strftime('%Y', disposed_at)                              AS year,
            SUM(CASE WHEN gain_loss_eur > 0 THEN gain_loss_eur ELSE 0 END) AS gains,
            SUM(CASE WHEN gain_loss_eur < 0 THEN ABS(gain_loss_eur) ELSE 0 END) AS losses,
            SUM(gain_loss_eur)                                       AS net,
            COUNT(DISTINCT trade_id)                                 AS trades,
            COUNT(*)                                                 AS disposal_rows,
            provider
        FROM tax_disposals
        GROUP BY year, provider
        ORDER BY year DESC
    """).fetchall()

    # Also get fees per year from original trades
    fee_rows = conn.execute("""
        SELECT
            strftime('%Y', timestamp) AS year,
            SUM(CASE
                WHEN side = 'BUY' AND fee < amount * 0.01 THEN fee * price
                ELSE fee
            END) AS fee_eur
        FROM trades
        WHERE mode = 'live'
        GROUP BY year
        ORDER BY year DESC
    """).fetchall()
    conn.close()

    fee_by_year = {r[0]: r[1] for r in fee_rows}

    result = []
    for year, gains, losses, net, trades, rows_, provider in rows:
        result.append({
            "year":      year,
            "gains":     round(gains, 2),
            "losses":    round(losses, 2),
            "net":       round(net, 2),
            "trades":    trades,
            "fee_total": round(fee_by_year.get(year, 0), 2),
            "provider":  provider,
        })
    return result


def disposals_for_year(year: str):
    """Returns list of disposal dicts for a given year, for CSV export."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT
            d.asset,
            'sell-for-fiat'             AS type,
            d.lot_acquired_at,
            d.cost_basis_eur,
            d.disposed_at,
            d.proceeds_eur,
            d.gain_loss_eur,
            d.quantity,
            d.exchange,
            d.provider
        FROM tax_disposals d
        WHERE strftime('%Y', d.disposed_at) = ?
        ORDER BY d.disposed_at ASC
    """, (year,)).fetchall()
    conn.close()

    return [
        {
            "asset":            r[0],
            "type":             r[1],
            "acquired_at":      r[2][:10] if r[2] else "unknown",
            "cost_basis_eur":   round(r[3], 4),
            "disposed_at":      r[4][:10],
            "proceeds_eur":     round(r[5], 4),
            "gain_loss_eur":    round(r[6], 4),
            "quantity":         round(r[7], 6),
            "exchange":         r[8],
            "provider":         r[9],
        }
        for r in rows
    ]


def disposals_csv(year: str) -> str:
    """Returns a CSV string of disposals for a year."""
    rows = disposals_for_year(year)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=[
        "asset", "type", "acquired_at", "cost_basis_eur",
        "disposed_at", "proceeds_eur", "gain_loss_eur",
        "quantity", "exchange", "provider",
    ])
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def integrity_check(current_holdings: dict) -> list:
    """
    Compare remaining lots per asset against current bot holdings.
    current_holdings: {asset: quantity} e.g. {'SOL': 2.5}
    Returns list of dicts with match status per asset.
    """
    conn = sqlite3.connect(DB_PATH)
    lot_rows = conn.execute(
        "SELECT asset, SUM(remaining) FROM tax_lots GROUP BY asset"
    ).fetchall()
    conn.close()

    results = []
    seen_assets = set()

    for asset, lot_remaining in lot_rows:
        seen_assets.add(asset)
        held = current_holdings.get(asset, 0.0)
        diff = abs(lot_remaining - held)
        results.append({
            "asset":         asset,
            "lots_remaining": round(lot_remaining, 6),
            "bot_holdings":   round(held, 6),
            "diff":           round(diff, 6),
            "ok":             diff < 0.01,
        })

    # Assets held by bot but not in lots
    for asset, held in current_holdings.items():
        if asset not in seen_assets and held > 0.001:
            results.append({
                "asset":         asset,
                "lots_remaining": 0.0,
                "bot_holdings":   round(held, 6),
                "diff":           round(held, 6),
                "ok":             False,
            })

    return results
