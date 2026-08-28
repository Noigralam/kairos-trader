import hmac
import json
import math
import os
import sqlite3
import time as _time
import datetime
import zoneinfo
from flask import Flask, jsonify, render_template, request, send_from_directory, Response
from bot import spot_engine as engine, spot_simulator as simulator, db, config, tax
from bot import __version__
from bot.spot_engine import manual_buy, manual_close, write_status_snapshot as _write_spot_snapshot
from bot.notifier import get_recent_logs
from bot.spot_exchange import get_price
from bot.candles import get_df
from bot.strategy import compute_signal, Signal, rsi_series

_SPOT_SNAPSHOT_PATH    = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "status_spot.json")
_FUTURES_SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "status_futures.json")
_SNAPSHOT_STALE_SECS   = 300  # warn if snapshot older than 5 min


def _read_snapshot(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _snapshot_stale(snap: dict) -> bool:
    written = snap.get("written_at")
    if not written:
        return True
    try:
        tz = zoneinfo.ZoneInfo("Europe/Helsinki")
        age = (datetime.datetime.now(tz=tz) - datetime.datetime.fromisoformat(written)).total_seconds()
        return age > _SNAPSHOT_STALE_SECS
    except Exception:
        return True

_TZ = zoneinfo.ZoneInfo("Europe/Helsinki")

app = Flask(__name__, static_folder="static")
# Use a persistent secret from env; fall back to a per-process random key.
# Sessions are not actively used today, but setting this prevents Flask from
# emitting a warning and avoids the latent risk of an unset key.
app.secret_key = os.environ.get("WEB_SECRET_KEY") or os.urandom(32)

# ---------------------------------------------------------------------------
# Simple in-memory rate limiter for public GET endpoints that hit Binance
# ---------------------------------------------------------------------------
import collections
_RATE_LIMIT_WINDOW = 60        # seconds
_RATE_LIMIT_MAX    = 20        # requests per window per IP
_rate_counters: dict[str, collections.deque] = collections.defaultdict(collections.deque)

def _rate_limited() -> bool:
    """Return True (and drop the request) if the calling IP exceeds the limit."""
    ip  = request.remote_addr or "unknown"
    now = _time.monotonic()
    dq  = _rate_counters[ip]
    cutoff = now - _RATE_LIMIT_WINDOW
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) >= _RATE_LIMIT_MAX:
        return True
    dq.append(now)
    return False


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon.ico", mimetype="image/x-icon")


@app.route("/")
def index():
    return render_template("index.html", config=config,
                           currency_symbol=config.SPOT_CURRENCY_SYMBOL,
                           dashboard_only=bool(os.environ.get("KAIROS_DASHBOARD_ONLY")))


@app.route("/api/status")
def api_status():
    snap = _read_snapshot(_SPOT_SNAPSHOT_PATH)
    snap["stale"] = _snapshot_stale(snap)
    return jsonify(snap)


@app.route("/api/trades")
def api_trades():
    cols   = ["id", "timestamp", "pair", "side", "price", "amount",
              "value_eur", "fee", "mode", "pnl", "notes"]
    limit  = int(request.args.get("limit", 15))
    page   = max(1, int(request.args.get("page", 1)))
    offset = (page - 1) * limit
    rows   = db.get_trades(limit, mode=config.SPOT_MODE, offset=offset)
    total  = db.get_trade_count(mode=config.SPOT_MODE)
    hold_times = db.get_hold_times(config.SPOT_MODE)
    trades = [dict(zip(cols, row)) for row in rows]
    for t in trades:
        t["hold_h"] = hold_times.get(t["id"])
    return jsonify({
        "trades": trades,
        "total":  total,
        "page":   page,
        "pages":  max(1, math.ceil(total / limit)),
    })


@app.route("/api/trades/csv")
def api_trades_csv():
    cols = ["id", "timestamp", "pair", "side", "price", "amount",
            "value_eur", "fee", "mode", "pnl", "notes"]
    rows = db.get_trades(9999, mode=config.SPOT_MODE)
    lines = [",".join(cols)]
    for row in rows:
        lines.append(",".join("" if v is None else str(v) for v in row))
    return Response("\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=spot_trades.csv"})


def _calc_tax(net_pnl: float) -> float:
    if net_pnl <= 0:
        return 0.0
    taxable = max(0.0, net_pnl - config.TAX_EXEMPT_AMOUNT)
    return min(taxable, config.TAX_BRACKET) * config.TAX_RATE_LOW + max(0, taxable - config.TAX_BRACKET) * config.TAX_RATE_HIGH



@app.route("/api/futures/tax")
def api_futures_tax():
    rows = db.get_tax_summary(mode="futures_live")
    return jsonify([
        {"year": r[0], "gains": r[1], "losses": r[2], "net_pnl": r[3], "tax": round(_calc_tax(r[3]), 2)}
        for r in rows
    ])


@app.route("/api/tax/fifo")
def api_tax_fifo():
    tax.init_schema()
    return jsonify(tax.annual_summary())


@app.route("/api/tax/fifo/csv")
def api_tax_fifo_csv():
    year = request.args.get("year", str(datetime.datetime.now().year))
    tax.init_schema()
    csv_data = tax.disposals_csv(year)
    return Response(csv_data, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=tax_fifo_{year}.csv"})


@app.route("/api/tax/fifo/integrity")
def api_tax_fifo_integrity():
    tax.init_schema()
    snap = _read_snapshot(_SPOT_SNAPSHOT_PATH)
    holdings = {tax._asset(pair): pos["amount"] for pair, pos in snap.get("positions", {}).items()}
    return jsonify(tax.integrity_check(holdings))


@app.route("/api/tax/fifo/rebuild", methods=["POST"])
def api_tax_fifo_rebuild():
    allowed, rate_limited = _check_pin()
    if rate_limited:
        return jsonify({"error": "Too many attempts — IP locked"}), 429
    if config.DASHBOARD_PIN and not allowed:
        return jsonify({"error": "PIN required"}), 403
    tax.rebuild()
    return jsonify({"ok": True, "summary": tax.annual_summary()})


@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_trade_stats(config.SPOT_MODE))


def _spot_config_for_pair(pair: str) -> dict:
    return {
        "rsi_period":    config.rsi_period_for(pair),
        "rsi_oversold":  config.rsi_oversold_for(pair),
        "rsi_overbought":config.rsi_overbought_for(pair),
        "ema_gap_pct":   round(config.ema_gap_for(pair) * 100, 2),
        "dca_drop_pct":  round(config.dca_drop_for(pair) * 100, 2),
        "dca_max":       config.dca_max_for(pair),
        "dca_step_pct":  round(config.SPOT_DCA_STEP_PCT * 100, 2),
        "dca_size_pct":  round(config.SPOT_DCA_SIZE_PCT * 100, 2),
        "take_profit_pct":   round(config.take_profit_for(pair) * 100, 2),
        "trailing_stop_pct": round(config.trailing_stop_for(pair) * 100, 2),
        "profit_floor_pct":  round(config.profit_floor_for(pair) * 100, 2),
        "min_exit_pct":      round(config.min_exit_for(pair) * 100, 2),
    }


@app.route("/api/config")
def api_config():
    pairs = list(config.SPOT_TRADING_PAIRS)
    return jsonify({
        "mode":              config.SPOT_MODE,
        "interval":          config.SPOT_INTERVAL,
        "pairs":             pairs,
        "fee_pct":           round(config.SPOT_FEE * 100, 3),
        "daily_ema_filter":  config.SPOT_DAILY_EMA_FILTER,
        "stop_check_interval": config.SPOT_STOP_CHECK_INTERVAL,
        "per_pair":          {p: _spot_config_for_pair(p) for p in pairs},
    })


@app.route("/api/shadow/<name>/config")
def api_shadow_config(name):
    s = _get_shadow(name)
    if s is None:
        return jsonify({"error": "not found"}), 404
    pairs = s.pairs or list(config.SPOT_TRADING_PAIRS)
    defaults = _spot_config_for_pair(pairs[0]) if pairs else {}
    ov = s.overrides

    def _ov(spot_key, default):
        return ov.get(spot_key, default)

    per_pair = {}
    for p in pairs:
        base = _spot_config_for_pair(p)
        per_pair[p] = {
            "rsi_period":        int(_ov("spot_rsi_period",     base["rsi_period"])),
            "rsi_oversold":      int(_ov("spot_rsi_oversold",   base["rsi_oversold"])),
            "rsi_overbought":    int(_ov("spot_rsi_overbought", base["rsi_overbought"])),
            "ema_gap_pct":       round(float(_ov("spot_ema_gap_pct",        base["ema_gap_pct"] / 100)) * 100, 2),
            "dca_drop_pct":      round(float(_ov("spot_dca_drop_pct",       base["dca_drop_pct"] / 100)) * 100, 2),
            "dca_max":           int(_ov("spot_dca_max",                     base["dca_max"])),
            "dca_step_pct":      round(float(_ov("spot_dca_step_pct",        base["dca_step_pct"] / 100)) * 100, 2),
            "dca_size_pct":      round(float(_ov("spot_dca_size_pct",        base["dca_size_pct"] / 100)) * 100, 2),
            "take_profit_pct":   round(float(_ov("spot_take_profit_pct",     base["take_profit_pct"] / 100)) * 100, 2),
            "trailing_stop_pct": round(float(_ov("spot_trailing_stop_pct",   base["trailing_stop_pct"] / 100)) * 100, 2),
            "profit_floor_pct":  round(float(_ov("spot_profit_floor_pct",    base["profit_floor_pct"] / 100)) * 100, 2),
            "min_exit_pct":      round(float(_ov("spot_min_exit_profit_pct", base["min_exit_pct"] / 100)) * 100, 2),
        }

    extra = {}
    if "spot_reentry_drop_pct" in ov:
        extra["reentry_drop_pct"] = round(float(ov["spot_reentry_drop_pct"]) * 100, 2)
    if "spot_stop_cooldown_candles" in ov:
        extra["stop_cooldown_candles"] = int(ov["spot_stop_cooldown_candles"])  # key now matches config

    return jsonify({
        "name":     s.name,
        "interval": s.interval,
        "pairs":    pairs,
        "overrides": ov,
        "per_pair": per_pair,
        "extra":    extra,
    })


@app.route("/api/balance_history")
def api_balance_history():
    days = float(request.args.get("days", 30))
    rows = db.get_balance_history(config.SPOT_MODE, max(days, 0))
    out = []
    for ts, bal in rows:
        try:
            t = datetime.datetime.fromisoformat(ts).astimezone(_TZ).strftime("%d.%m. %H:%M")
        except Exception:
            t = ts
        out.append({"t": t, "balance": bal})
    return jsonify(out)


@app.route("/api/fng")
def api_fng():
    from bot.notifier import get_fng
    value, label = get_fng()
    return jsonify({"value": value, "label": label})


def _daily_ema_val(pair: str):
    if not config.SPOT_DAILY_EMA_FILTER:
        return None
    df1d = get_df(pair, "1d", limit=210)
    if len(df1d) < 201:
        return None
    return float(df1d["close"].ewm(span=200, adjust=False).mean().iloc[-1])


@app.route("/api/signals")
def api_signals():
    if _rate_limited():
        return jsonify({"error": "rate limited"}), 429
    out = {}
    snap = _read_snapshot(_SPOT_SNAPSHOT_PATH)
    snap_positions = snap.get("positions", {})
    for pair in config.SPOT_TRADING_PAIRS:
        try:
            price = get_price(pair)
            daily_ema_val = _daily_ema_val(pair)
            result = compute_signal(get_df(pair, config.SPOT_INTERVAL),
                                    rsi_period=config.rsi_period_for(pair),
                                    rsi_oversold=config.rsi_oversold_for(pair),
                                    rsi_overbought=config.rsi_overbought_for(pair),
                                    ema_gap=config.ema_gap_for(pair),
                                    daily_ema=daily_ema_val)
            gap = price - result.ema_trend
            ema_threshold = result.ema_trend * (1 + config.ema_gap_for(pair))
            above_trend = price >= ema_threshold
            has_position = pair in snap_positions

            if result.signal == Signal.BUY and not has_position:
                commentary = f"RSI hit oversold ({result.rsi:.1f}) and price is above EMA200 — buy signal firing."
            elif result.signal == Signal.SELL and has_position:
                commentary = f"RSI recovered ({result.rsi:.1f}) — sell signal firing."
            elif result.signal == Signal.SELL and not has_position:
                commentary = f"RSI overbought ({result.rsi:.1f}) but no position held — nothing to sell."
            elif not above_trend:
                if has_position:
                    commentary = (
                        f"Price is {config.SPOT_CURRENCY_SYMBOL}{abs(gap):,.2f} below EMA200 — holding position, "
                        f"watching for RSI to reach {config.rsi_overbought_for(pair)} for exit."
                    )
                elif price >= result.ema_trend:
                    commentary = (
                        f"Price is above EMA200 but within the {config.ema_gap_for(pair)*100:.0f}% gap filter "
                        f"(need {config.SPOT_CURRENCY_SYMBOL}{ema_threshold:,.2f}, currently {config.SPOT_CURRENCY_SYMBOL}{price:,.2f}). "
                        f"Waiting for stronger trend confirmation."
                    )
                else:
                    commentary = (
                        f"Price is {config.SPOT_CURRENCY_SYMBOL}{abs(gap):,.2f} below EMA200 — downtrend guard active. "
                        f"No buys until price recovers above {config.SPOT_CURRENCY_SYMBOL}{ema_threshold:,.2f} "
                        f"(EMA200 {config.SPOT_CURRENCY_SYMBOL}{result.ema_trend:,.2f} + {config.ema_gap_for(pair)*100:.0f}% gap)."
                    )
            elif result.rsi >= config.rsi_oversold_for(pair):
                if has_position:
                    commentary = (
                        f"Holding — RSI is {result.rsi:.1f}, watching for recovery above "
                        f"{config.rsi_overbought_for(pair)} to trigger sell."
                    )
                else:
                    commentary = (
                        f"Trend is healthy (price above EMA200 by {config.SPOT_CURRENCY_SYMBOL}{gap:,.2f}), "
                        f"but RSI is {result.rsi:.1f} — waiting for a dip below {config.rsi_oversold_for(pair)}."
                    )
            else:
                commentary = result.reason

            out[pair] = {
                "price": round(price, 2),
                "rsi": round(result.rsi, 1),
                "rsi_period": config.rsi_period_for(pair),
                "rsi_oversold":  config.rsi_oversold_for(pair),
                "rsi_overbought": config.rsi_overbought_for(pair),
                "ema200": round(result.ema_trend, 2),
                "ema_threshold": round(ema_threshold, 2),
                "daily_ema200": round(daily_ema_val, 2) if daily_ema_val else None,
                "above_trend": above_trend,
                "gap": round(gap, 2),
                "signal": result.signal.value,
                "reason": result.reason,
                "commentary": commentary,
                "has_position": has_position,
            }
        except Exception as e:
            out[pair] = {"error": config.scrub_err(e)}
    return jsonify(out)


class _MainSpotShim:
    """Wraps the spot status snapshot so it can appear as the baseline in spot shadow ranking."""
    name     = "MAIN"
    overrides: dict = {}
    is_main  = True

    def __init__(self, snap: dict):
        self._snap    = snap
        self.pairs    = list(config.SPOT_TRADING_PAIRS)
        self.interval = config.SPOT_INTERVAL
        self._db_mode = config.SPOT_MODE

    @property
    def balance(self):       return self._snap.get("balance", 0.0)
    @property
    def total_trades(self):  return self._snap.get("total_trades", 0)
    @property
    def total_pnl(self):     return self._snap.get("total_pnl", 0.0)
    @property
    def total_fees(self):    return self._snap.get("total_fees", 0.0)
    @property
    def positions(self):     return self._snap.get("positions", {})

    def _o(self, key, default):
        return default

    def _trailing_stop_level(self, pos):
        return pos.trailing_stop_level(
            trail_pct=config.trailing_stop_for(pos.pair),
            floor_pct=config.profit_floor_for(pos.pair),
        )



class _MainFuturesShim:
    """Wraps the futures status snapshot so it can appear as the baseline in futures shadow ranking."""
    name         = "MAIN"
    overrides: dict = {}
    is_main      = True

    def __init__(self, snap: dict):
        self._snap    = snap
        self.symbols  = list(config.FUTURES_TRADING_PAIRS)
        self._db_mode = f"futures_{config.FUTURES_MODE}"
        self._starting = db.get_starting_balance(self._db_mode) or config.FUTURES_SIMULATION_BALANCE

    def _o(self, key, default):
        return default

    @property
    def balance(self):        return self._snap.get("balance", 0.0)
    @property
    def total_trades(self):   return self._snap.get("total_trades", 0)
    @property
    def total_pnl(self):      return self._snap.get("total_pnl", 0.0)
    @property
    def total_fees(self):     return self._snap.get("total_fees", 0.0)
    @property
    def total_funding(self):  return self._snap.get("total_funding", 0.0)
    @property
    def positions(self):      return self._snap.get("positions", {})


@app.route("/api/shadows")
def api_shadows():
    from bot.spot_simulator import get_spot_shadows
    rows = [{"name": s.name, "overrides": s.overrides} for s in get_spot_shadows()]
    if config.SPOT_MODE == "simulation":
        rows.insert(0, {"name": "MAIN", "overrides": {}, "is_main": True})
    return jsonify(rows)


@app.route("/api/shadows/ranking")
def api_shadows_ranking():
    from bot.spot_simulator import get_spot_shadows
    from bot.spot_exchange import get_price

    snap = _read_snapshot(_SPOT_SNAPSHOT_PATH)
    snap_balance = snap.get("balance", 0.0)
    snap_positions = snap.get("positions", {})

    live_prices = {}
    for p in config.SPOT_TRADING_PAIRS:
        try:
            live_prices[p] = get_price(p)
        except Exception:
            pass
    live_open_val  = sum(live_prices.get(p, pos["value_eur"]) * pos["amount"] for p, pos in snap_positions.items())
    live_portfolio = snap_balance + live_open_val
    if config.SPOT_MODE == "live":
        live_starting   = snap.get("starting_balance") or db.get_starting_balance(config.SPOT_MODE)
        live_return_pct = round((live_portfolio / live_starting - 1) * 100, 2) if live_starting and live_starting > 0 else None
    else:
        live_starting   = config.SPOT_SIMULATION_BALANCE
        live_return_pct = round((live_portfolio / live_starting - 1) * 100, 2) if live_starting > 0 else None

    def _shadow_row(s, is_main=False):
        prices = {}
        for p in (s.pairs or config.SPOT_TRADING_PAIRS):
            try:
                prices[p] = get_price(p)
            except Exception:
                pass
        open_val  = sum(prices.get(p, pos.value_eur) * pos.amount for p, pos in s.positions.items())
        portfolio = s.balance + open_val
        starting  = live_starting if is_main else s.starting_balance
        ret       = round((portfolio / starting - 1) * 100, 2) if starting > 0 else 0
        stats     = db.get_trade_stats(s._db_mode)
        if not is_main and live_return_pct is not None and hasattr(s, "set_live_baseline"):
            s.set_live_baseline(live_return_pct)
        baseline = getattr(s, "live_baseline_pct", None)
        vs_live = round(ret - (live_return_pct - baseline), 2) if (not is_main and live_return_pct is not None and baseline is not None) else None
        return {
            "name":             s.name,
            "is_main":          is_main,
            "pairs":            s.pairs or config.SPOT_TRADING_PAIRS,
            "portfolio_value":  round(portfolio, 2),
            "starting_balance": starting,
            "return_pct":       ret,
            "pnl":              round(s.total_pnl, 2),
            "trades":           s.total_trades,
            "win_rate":         stats.get("win_rate", 0) if stats else 0,
            "vs_live":          vs_live,
        }

    rows = []
    if config.SPOT_MODE == "simulation":
        rows.append(_shadow_row(_MainSpotShim(snap), is_main=True))
    for s in get_spot_shadows():
        rows.append(_shadow_row(s))
    rows.sort(key=lambda x: (not x["is_main"], -x["return_pct"]))
    return jsonify({"live_return_pct": live_return_pct, "shadows": rows})


@app.route("/api/shadows/pnl_history")
def api_shadows_pnl_history():
    from bot.spot_simulator import get_spot_shadows, get_state
    days = float(request.args.get("days", 30))
    out  = {}

    def _series(db_mode, starting):
        rows = db.get_balance_history(db_mode, max(days, 0))
        result = []
        # Prepend a synthetic origin point when history predates starting_balance
        # (happens when a shadow was created before a balance reset, or history was
        # cleared but the first real log entry reflects an already-deployed position)
        if rows and rows[0][1] < starting * 0.95:
            first_ts, _ = rows[0]
            try:
                dt0 = datetime.datetime.fromisoformat(first_ts).astimezone(_TZ) - datetime.timedelta(minutes=1)
                result.append({"t": dt0.strftime("%d.%m. %H:%M"), "sk": dt0.timestamp(), "pnl": 0.0, "pct": 0.0})
            except Exception:
                pass
        for ts, bal in rows:
            try:
                dt = datetime.datetime.fromisoformat(ts).astimezone(_TZ)
                t  = dt.strftime("%d.%m. %H:%M")
                sk = dt.timestamp()
            except Exception:
                t = ts; sk = 0
            pnl = bal - starting
            result.append({"t": t, "sk": sk, "pnl": round(pnl, 2), "pct": round(pnl / starting * 100, 3) if starting else 0})
        return result

    if config.SPOT_MODE == "simulation":
        out["MAIN"] = _series(config.SPOT_MODE, config.SPOT_SIMULATION_BALANCE)
    for s in get_spot_shadows():
        out[s.name] = _series(s._db_mode, s.starting_balance)
    return jsonify(out)


def _get_shadow(name: str):
    from bot.spot_simulator import get_spot_shadows, get_state
    if name.upper() == "MAIN" and config.SPOT_MODE == "simulation":
        return _MainSpotShim(get_state())
    return next((s for s in get_spot_shadows() if s.name.upper() == name.upper()), None)


@app.route("/api/shadow/<name>/status")
def api_shadow_status(name):
    s = _get_shadow(name)
    if s is None:
        return jsonify({"error": "not found"}), 404
    prices = {}
    for pair in (s.pairs or config.SPOT_TRADING_PAIRS):
        try:
            prices[pair] = get_price(pair)
        except Exception:
            pass
    open_val = sum(prices.get(p, pos.value_eur) * pos.amount for p, pos in s.positions.items())
    starting = s.starting_balance
    fee      = config.SPOT_FEE
    positions_out = {}
    for pair, pos in s.positions.items():
        price = prices.get(pair, pos.entry_price)
        positions_out[pair] = {
            "entry_price":   pos.entry_price,
            "amount":        pos.amount,
            "value_eur":     round(pos.value_eur, 2),
            "highest_price": pos.peak(),
            "trailing_stop": round(s._trailing_stop_level(pos), 2),
            "take_profit":   round(pos.take_profit_price, 2),
            "break_even":    round(pos.entry_price * (1 + fee) / (1 - fee), 2),
            "dca_count":     pos.dca_count,
            "dca_max":       s._o("spot_dca_max", config.SPOT_DCA_MAX),
            "dca_trigger":   round(pos.entry_price * (1 - (s._o("spot_dca_drop_pct", config.SPOT_DCA_DROP_PCT) + pos.dca_count * s._o("spot_dca_step_pct", config.SPOT_DCA_STEP_PCT))), 2)
                             if pos.dca_count < s._o("spot_dca_max", config.SPOT_DCA_MAX) else None,
            "current_price": round(price, 2),
        }
    conn = sqlite3.connect(db.DB_PATH)
    row  = conn.execute(
        "SELECT MIN(timestamp) FROM balance_history WHERE mode = ?", (s._db_mode,)
    ).fetchone()
    conn.close()
    started_at = row[0] if row and row[0] else None

    return jsonify({
        "name":             s.name,
        "overrides":        s.overrides,
        "balance":          round(s.balance, 2),
        "total_trades":     s.total_trades,
        "total_pnl":        round(s.total_pnl, 2),
        "total_fees":       round(s.total_fees, 4),
        "portfolio_value":  round(s.balance + open_val, 2),
        "starting_balance": starting,
        "positions":        positions_out,
        "started_at":       started_at,
    })


@app.route("/api/shadow/<name>/trades")
def api_shadow_trades(name):
    s = _get_shadow(name)
    if s is None:
        return jsonify({"trades": [], "total": 0, "page": 1, "pages": 1})
    cols   = ["id", "timestamp", "pair", "side", "price", "amount", "value_eur", "fee", "mode", "pnl", "notes"]
    limit  = int(request.args.get("limit", 15))
    page   = max(1, int(request.args.get("page", 1)))
    offset = (page - 1) * limit
    rows   = db.get_trades(limit, mode=s._db_mode, offset=offset)
    total  = db.get_trade_count(mode=s._db_mode)
    hold_times = db.get_hold_times(s._db_mode)
    trades = [dict(zip(cols, row)) for row in rows]
    for t in trades:
        t["hold_h"] = hold_times.get(t["id"])
    return jsonify({
        "trades": trades,
        "total":  total,
        "page":   page,
        "pages":  max(1, math.ceil(total / limit)),
    })


@app.route("/api/shadow/<name>/trades/csv")
def api_shadow_trades_csv(name):
    s = _get_shadow(name)
    if s is None:
        return Response("not found", status=404)
    cols = ["id", "timestamp", "pair", "side", "price", "amount", "value_eur", "fee", "mode", "pnl", "notes"]
    rows = db.get_trades(9999, mode=s._db_mode)
    lines = [",".join(cols)]
    for row in rows:
        lines.append(",".join("" if v is None else str(v) for v in row))
    return Response("\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=shadow_{name.lower()}_trades.csv"})


@app.route("/api/shadow/<name>/balance_history")
def api_shadow_balance_history(name):
    s = _get_shadow(name)
    if s is None:
        return jsonify([])
    days = float(request.args.get("days", 30))
    rows = db.get_balance_history(s._db_mode, max(days, 0))
    out = []
    for ts, bal in rows:
        try:
            t = datetime.datetime.fromisoformat(ts).astimezone(_TZ).strftime("%d.%m. %H:%M")
        except Exception:
            t = ts
        out.append({"t": t, "balance": bal})
    return jsonify(out)


@app.route("/api/shadow/<name>/stats")
def api_shadow_stats(name):
    s = _get_shadow(name)
    if s is None:
        return jsonify({})
    return jsonify(db.get_trade_stats(s._db_mode))


@app.route("/api/shadow/<name>/signals")
def api_shadow_signals(name):
    if _rate_limited():
        return jsonify({"error": "rate limited"}), 429
    s = _get_shadow(name)
    if s is None:
        return jsonify({"error": "not found"}), 404
    out = {}
    for pair in (s.pairs or config.SPOT_TRADING_PAIRS):
        try:
            price  = get_price(pair)
            rsi_period   = s._o("spot_rsi_period",     config.rsi_period_for(pair))
            rsi_oversold = s._o("spot_rsi_oversold",   config.rsi_oversold_for(pair))
            rsi_ob       = s._o("spot_rsi_overbought", config.rsi_overbought_for(pair))
            ema_gap      = s._o("spot_ema_gap_pct",    config.ema_gap_for(pair))
            result = compute_signal(get_df(pair, s.interval),
                                    rsi_period=rsi_period, rsi_oversold=rsi_oversold,
                                    rsi_overbought=rsi_ob, ema_gap=ema_gap)
            ema_threshold = result.ema_trend * (1 + ema_gap)
            above_trend   = price >= ema_threshold
            gap           = price - result.ema_trend
            has_position  = pair in s.positions

            if result.signal == Signal.BUY and not has_position:
                commentary = f"RSI hit oversold ({result.rsi:.1f}) and price is above EMA200 — buy signal."
            elif result.signal == Signal.SELL and has_position:
                commentary = f"RSI recovered ({result.rsi:.1f}) — sell signal."
            elif not above_trend:
                commentary = (f"Price is {config.SPOT_CURRENCY_SYMBOL}{abs(gap):,.2f} below EMA200 — downtrend guard active."
                              if price < result.ema_trend else
                              f"Price above EMA200 but within {ema_gap*100:.0f}% gap filter.")
            elif result.rsi >= rsi_oversold:
                commentary = (f"Holding — RSI {result.rsi:.1f}, watching for {rsi_ob}." if has_position
                              else f"Trend healthy, RSI {result.rsi:.1f} — waiting for dip below {rsi_oversold}.")
            else:
                commentary = result.reason

            pos_data = None
            if pair in s.positions:
                pos  = s.positions[pair]
                fee  = config.SPOT_FEE
                pos_data = {
                    "entry_price":   pos.entry_price,
                    "amount":        pos.amount,
                    "value_eur":     round(pos.value_eur, 2),
                    "highest_price": pos.peak(),
                    "trailing_stop": round(s._trailing_stop_level(pos), 2),
                    "take_profit":   round(pos.take_profit_price, 2),
                    "break_even":    round(pos.entry_price * (1 + fee) / (1 - fee), 2),
                    "dca_count":     pos.dca_count,
                    "dca_max":       s._o("spot_dca_max",     config.SPOT_DCA_MAX),
                    "dca_trigger":   round(pos.entry_price * (1 - (s._o("spot_dca_drop_pct", config.SPOT_DCA_DROP_PCT) + pos.dca_count * s._o("spot_dca_step_pct", config.SPOT_DCA_STEP_PCT))), 2)
                                     if pos.dca_count < s._o("spot_dca_max", config.SPOT_DCA_MAX) else None,
                    "current_price": round(price, 2),
                }

            out[pair] = {
                "price":          round(price, 2),
                "rsi":            round(result.rsi, 1),
                "rsi_period":     rsi_period,
                "rsi_oversold":   rsi_oversold,
                "rsi_overbought": rsi_ob,
                "ema200":         round(result.ema_trend, 2),
                "ema_threshold":  round(ema_threshold, 2),
                "above_trend":    above_trend,
                "gap":            round(gap, 2),
                "signal":         result.signal.value,
                "commentary":     commentary,
                "has_position":   has_position,
                "position":       pos_data,
            }
        except Exception as e:
            out[pair] = {"error": config.scrub_err(e)}
    return jsonify(out)


@app.route("/api/shadow/<name>/chart/<pair>")
def api_shadow_chart(name, pair):
    if _rate_limited():
        return jsonify({"error": "rate limited"}), 429
    s = _get_shadow(name)
    if s is None:
        return jsonify({"error": "not found"}), 404
    span_candles = {"4h": 16, "1d": 96, "3d": 288, "1w": 672, "2w": 1344, "1m": 2880}
    span  = request.args.get("span", "1d")
    limit = span_candles.get(span, 96)
    pair  = pair.upper()

    df = get_df(pair, config.SPOT_INTERVAL, limit=limit + 210)
    if df.empty:
        return jsonify({"labels": [], "prices": [], "ema200": [], "rsi": [], "buys": [], "sells": []})

    close = df["close"]
    ema   = close.ewm(span=200, adjust=False).mean()

    _bb_period = 20
    bb_mid   = close.rolling(_bb_period).mean()
    bb_std   = close.rolling(_bb_period).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std

    _rp      = s._o("spot_rsi_period",     config.rsi_period_for(pair))
    _ros     = s._o("spot_rsi_oversold",   config.rsi_oversold_for(pair))
    _rob     = s._o("spot_rsi_overbought", config.rsi_overbought_for(pair))
    _egap    = s._o("spot_ema_gap_pct",    config.ema_gap_for(pair))
    rsi_s    = rsi_series(close, _rp)

    buy_prices = [None] * len(df); sell_prices = [None] * len(df); blocked_buys = [None] * len(df)
    for i in range(210, len(df)):
        r = rsi_s.iloc[i]; e = ema.iloc[i]; p = close.iloc[i]
        if r < _ros and p >= e * (1 + _egap):
            buy_prices[i] = round(p, 4)
        elif r < _ros:
            blocked_buys[i] = round(p, 4)
        elif r > _rob:
            sell_prices[i] = round(p, 4)

    sl  = slice(-limit, None)
    labels = (df["open_time"].iloc[sl]
              .dt.tz_convert(_TZ)
              .dt.strftime("%d.%m. %H:%M").tolist())
    label_set = set(labels)

    _interval_mins = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    _candle_mins   = _interval_mins.get(config.SPOT_INTERVAL, 15)
    actual_buys = []; actual_sells = []; actual_dcas = []
    for row in db.get_trades(200, mode=s._db_mode):
        _, ts, tpair, side, price, *_ = row
        notes = row[-1] or ""
        if tpair != pair:
            continue
        try:
            t = datetime.datetime.fromisoformat(ts).astimezone(_TZ)
            t = t.replace(minute=(t.minute // _candle_mins) * _candle_mins, second=0, microsecond=0)
            label = t.strftime("%d.%m. %H:%M")
        except Exception:
            continue
        if label not in label_set:
            continue
        pt = {"x": label, "y": round(price, 4)}
        if side == "BUY" and "dca" in notes.lower():
            actual_dcas.append(pt)
        elif side == "BUY":
            actual_buys.append(pt)
        elif side == "SELL":
            actual_sells.append(pt)

    vol_up     = (close >= df["open"]).iloc[sl]
    vol_colors = ['#3fb95066' if u else '#f8514966' for u in vol_up]
    ohlc = [{"x": lbl, "o": round(o, 4), "h": round(h, 4), "l": round(l, 4), "c": round(c, 4)}
            for lbl, o, h, l, c in zip(labels, df["open"].iloc[sl].tolist(), df["high"].iloc[sl].tolist(),
                                        df["low"].iloc[sl].tolist(), close.iloc[sl].tolist())]
    return jsonify({
        "labels":       labels,
        "prices":       close.iloc[sl].round(4).tolist(),
        "ema200":       ema.iloc[sl].round(4).tolist(),
        "bb_upper":     bb_upper.iloc[sl].round(4).tolist(),
        "bb_lower":     bb_lower.iloc[sl].round(4).tolist(),
        "ohlc":         ohlc,
        "volume":       df["volume"].iloc[sl].round(4).tolist(),
        "vol_colors":   vol_colors,
        "rsi":          rsi_s.iloc[sl].round(2).tolist(),
        "rsi_oversold": _ros,
        "rsi_overbought": _rob,
        "buys":         buy_prices[-limit:],
        "sells":        sell_prices[-limit:],
        "blocked_buys": blocked_buys[-limit:],
        "actual_buys":  actual_buys,
        "actual_sells": actual_sells,
        "actual_dcas":  actual_dcas,
    })


def _futures_config_for_sym(sym: str) -> dict:
    return {
        "rsi_period":        config.futures_rsi_period_for(sym),
        "rsi_oversold":      config.futures_rsi_oversold_for(sym),
        "rsi_overbought":    config.futures_rsi_overbought_for(sym),
        "ema_gap_pct":       round(config.futures_ema_gap_for(sym) * 100, 2),
        "leverage":          config.FUTURES_LEVERAGE,
        "position_size_pct": round(config.FUTURES_POSITION_SIZE_PCT * 100, 2),
        "dca_drop_pct":      round(config.FUTURES_DCA_DROP_PCT * 100, 2),
        "dca_size_pct":      round(config.FUTURES_DCA_SIZE_PCT * 100, 2),
        "take_profit_pct":   round(config.FUTURES_TAKE_PROFIT_PCT * 100, 2),
        "trailing_stop_pct": round(config.FUTURES_TRAILING_STOP_PCT * 100, 2),
        "profit_floor_pct":  round(config.FUTURES_PROFIT_FLOOR_PCT * 100, 2),
        "min_exit_pct":      round(config.FUTURES_MIN_EXIT_PROFIT_PCT * 100, 2),
        "max_drawdown_pct":  round(config.FUTURES_MAX_DRAWDOWN_PCT * 100, 2),
        "max_funding_rate":  config.FUTURES_MAX_FUNDING_RATE,
    }


@app.route("/api/futures/config")
def api_futures_config():
    if not config.FUTURES_ENABLED:
        return jsonify({"enabled": False})
    syms = list(config.FUTURES_TRADING_PAIRS)
    return jsonify({
        "enabled":             True,
        "mode":                config.FUTURES_MODE,
        "interval":            "15m",
        "symbols":             syms,
        "fee_pct":             round(config.FUTURES_FEE * 100, 4),
        "margin_type":         config.FUTURES_MARGIN_TYPE,
        "stop_check_interval": config.FUTURES_STOP_CHECK_INTERVAL,
        "per_sym":             {s: _futures_config_for_sym(s) for s in syms},
    })


@app.route("/api/futures/shadow/<name>/config")
def api_futures_shadow_config(name):
    if not config.FUTURES_ENABLED:
        return jsonify({"error": "futures disabled"}), 404
    from bot.futures_simulator import get_futures_shadows
    sh = next((s for s in get_futures_shadows() if s.name.upper() == name.upper()), None)
    if sh is None:
        return jsonify({"error": "not found"}), 404
    syms = sh.symbols
    ov   = sh.overrides

    def _ov(key, default):
        return ov.get(key, default)

    per_sym = {}
    for s in syms:
        base = _futures_config_for_sym(s)
        per_sym[s] = {
            "rsi_period":        int(_ov("futures_rsi_period",     base["rsi_period"])),
            "rsi_oversold":      int(_ov("futures_rsi_oversold",   base["rsi_oversold"])),
            "rsi_overbought":    int(_ov("futures_rsi_overbought", base["rsi_overbought"])),
            "ema_gap_pct":       round(float(_ov("futures_ema_gap_pct",          base["ema_gap_pct"] / 100)) * 100, 2),
            "leverage":          int(_ov("futures_leverage",                      base["leverage"])),
            "position_size_pct": round(float(_ov("futures_position_size_pct",    base["position_size_pct"] / 100)) * 100, 2),
            "dca_drop_pct":      base["dca_drop_pct"],
            "dca_size_pct":      base["dca_size_pct"],
            "take_profit_pct":   base["take_profit_pct"],
            "trailing_stop_pct": base["trailing_stop_pct"],
            "profit_floor_pct":  base["profit_floor_pct"],
            "min_exit_pct":      base["min_exit_pct"],
            "max_drawdown_pct":  base["max_drawdown_pct"],
            "max_funding_rate":  float(_ov("futures_max_funding_rate", base["max_funding_rate"])),
        }

    return jsonify({
        "name":     sh.name,
        "interval": "15m",
        "symbols":  syms,
        "overrides": ov,
        "per_sym":  per_sym,
    })


@app.route("/api/futures/shadow/<name>/signals")
def api_futures_shadow_signals(name):
    if _rate_limited():
        return jsonify({"error": "rate limited"}), 429
    sh = _get_futures_shadow(name)
    if sh is None:
        return jsonify({"error": "not found"}), 404
    from bot.futures_exchange import get_mark_price
    from bot.candles import get_df as _get_df
    out = {}
    for sym in sh.symbols:
        try:
            price        = get_mark_price(sym)
            rsi_period   = sh._o("futures_rsi_period",     config.futures_rsi_period_for(sym))
            rsi_oversold = sh._o("futures_rsi_oversold",   config.futures_rsi_oversold_for(sym))
            rsi_ob       = sh._o("futures_rsi_overbought", config.futures_rsi_overbought_for(sym))
            ema_gap      = sh._o("futures_ema_gap_pct",    config.futures_ema_gap_for(sym))
            df           = _get_df(sym, "15m", limit=250)
            result       = compute_signal(df, rsi_period=rsi_period, rsi_oversold=rsi_oversold,
                                          rsi_overbought=rsi_ob, ema_gap=ema_gap)
            gap           = price - result.ema_trend
            ema_threshold = result.ema_trend * (1 + ema_gap)
            above_trend   = price >= ema_threshold
            has_position  = sym in sh.positions

            if result.signal == Signal.BUY and not has_position:
                commentary = f"RSI hit oversold ({result.rsi:.1f}) and price is above EMA200 — long signal firing."
            elif result.signal == Signal.SELL and has_position:
                commentary = f"RSI recovered ({result.rsi:.1f}) — trailing stop will manage exit."
            elif result.signal == Signal.SELL and not has_position:
                commentary = f"RSI overbought ({result.rsi:.1f}) but no position — nothing to do."
            elif not above_trend:
                commentary = (f"Price is ${abs(gap):,.2f} below EMA200 — downtrend guard active."
                              if price < result.ema_trend else
                              f"Price above EMA200 but within {ema_gap*100:.0f}% gap filter.")
            elif result.rsi >= rsi_oversold:
                commentary = (f"Holding long — RSI {result.rsi:.1f}, watching for {rsi_ob}." if has_position
                              else f"Trend healthy, RSI {result.rsi:.1f} — waiting for dip below {rsi_oversold}.")
            else:
                commentary = result.reason

            pos      = sh.positions.get(sym)
            pos_data = None
            if pos:
                pos_data = {
                    "entry_price":       round(pos.entry_price, 4),
                    "mark_price":        round(price, 4),
                    "amount":            pos.amount,
                    "margin":            round(pos.margin, 2),
                    "leverage":          pos.leverage,
                    "unrealized_pnl":    round(pos.unrealized_pnl(price), 4),
                    "pnl_pct":           round(pos.pnl_pct(price) * 100, 2),
                    "liquidation_price": round(pos.liquidation_price(), 4),
                    "trailing_stop":     round(pos.trailing_stop_level(), 4),
                    "take_profit_price": round(pos.take_profit_price, 4),
                    "highest_price":     round(pos.highest_price, 4),
                    "dca_count":         pos.dca_count,
                }

            out[sym] = {
                "price":          round(price, 4),
                "rsi":            round(result.rsi, 1),
                "rsi_period":     rsi_period,
                "rsi_oversold":   rsi_oversold,
                "rsi_overbought": rsi_ob,
                "ema200":         round(result.ema_trend, 4),
                "ema_threshold":  round(ema_threshold, 4),
                "above_trend":    above_trend,
                "gap":            round(gap, 4),
                "signal":         result.signal.value,
                "commentary":     commentary,
                "has_position":   has_position,
                "position":       pos_data,
            }
        except Exception as e:
            out[sym] = {"error": config.scrub_err(e)}
    return jsonify(out)


@app.route("/api/futures/shadow/<name>/chart/<symbol>")
def api_futures_shadow_chart(name, symbol):
    if _rate_limited():
        return jsonify({"error": "rate limited"}), 429
    sh = _get_futures_shadow(name)
    if sh is None:
        return jsonify({"error": "not found"}), 404
    from bot.candles import get_df as _get_df
    span_candles = {"4h": 16, "1d": 96, "3d": 288, "1w": 672, "2w": 1344, "1m": 2880}
    span  = request.args.get("span", "1d")
    limit = span_candles.get(span, 96)
    sym   = symbol.upper()

    df = _get_df(sym, "15m", limit=limit + 210)
    if df.empty:
        return jsonify({"labels": [], "prices": [], "ema200": [], "rsi": [], "buys": [], "sells": []})

    close = df["close"]
    ema   = close.ewm(span=200, adjust=False).mean()

    _bb_period = 20
    bb_mid   = close.rolling(_bb_period).mean()
    bb_std   = close.rolling(_bb_period).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std

    _rp   = sh._o("futures_rsi_period",     config.futures_rsi_period_for(sym))
    _ros  = sh._o("futures_rsi_oversold",   config.futures_rsi_oversold_for(sym))
    _rob  = sh._o("futures_rsi_overbought", config.futures_rsi_overbought_for(sym))
    _egap = sh._o("futures_ema_gap_pct",    config.futures_ema_gap_for(sym))
    rsi   = rsi_series(close, _rp)

    buy_prices   = [None] * len(df)
    sell_prices  = [None] * len(df)
    blocked_buys = [None] * len(df)
    for i in range(210, len(df)):
        r = rsi.iloc[i]; e = ema.iloc[i]; p = close.iloc[i]
        if r < _ros and p >= e * (1 + _egap):
            buy_prices[i] = round(p, 4)
        elif r < _ros:
            blocked_buys[i] = round(p, 4)
        elif r > _rob:
            sell_prices[i] = round(p, 4)

    sl  = slice(-limit, None)
    labels = (df["open_time"].iloc[sl]
              .dt.tz_convert(_TZ)
              .dt.strftime("%d.%m. %H:%M").tolist())
    label_set = set(labels)

    actual_buys = []; actual_sells = []; actual_dcas = []
    for row in db.get_trades(200, mode=sh._db_mode):
        _, ts, tpair, side, price, *_ = row
        notes = row[-1] or ""
        if tpair != sym:
            continue
        try:
            t = datetime.datetime.fromisoformat(ts).astimezone(_TZ)
            t = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
            label = t.strftime("%d.%m. %H:%M")
        except Exception:
            continue
        if label not in label_set:
            continue
        pt = {"x": label, "y": round(price, 4)}
        if side == "BUY" and "dca" in notes.lower():
            actual_dcas.append(pt)
        elif side == "BUY":
            actual_buys.append(pt)
        elif side == "SELL":
            actual_sells.append(pt)

    vol_up     = (close >= df["open"]).iloc[sl]
    vol_colors = ['#3fb95066' if u else '#f8514966' for u in vol_up]
    ohlc = [{"x": lbl, "o": round(o, 4), "h": round(h, 4), "l": round(l, 4), "c": round(c, 4)}
            for lbl, o, h, l, c in zip(
                labels,
                df["open"].iloc[sl].tolist(), df["high"].iloc[sl].tolist(),
                df["low"].iloc[sl].tolist(),  close.iloc[sl].tolist())]

    return jsonify({
        "labels":       labels,
        "prices":       close.iloc[sl].round(4).tolist(),
        "ema200":       ema.iloc[sl].round(4).tolist(),
        "bb_upper":     bb_upper.iloc[sl].round(4).tolist(),
        "bb_lower":     bb_lower.iloc[sl].round(4).tolist(),
        "ohlc":         ohlc,
        "volume":       df["volume"].iloc[sl].round(4).tolist(),
        "vol_colors":   vol_colors,
        "rsi":          rsi.iloc[sl].round(2).tolist(),
        "buys":         buy_prices[-limit:],
        "sells":        sell_prices[-limit:],
        "blocked_buys": blocked_buys[-limit:],
        "actual_buys":  actual_buys,
        "actual_sells": actual_sells,
        "actual_dcas":  actual_dcas,
    })


@app.route("/api/futures/signals")
def api_futures_signals():
    if _rate_limited():
        return jsonify({"error": "rate limited"}), 429
    if not config.FUTURES_ENABLED:
        return jsonify({})
    from bot.futures_exchange import get_klines as f_klines, get_mark_price
    out = {}
    f_snap = _read_snapshot(_FUTURES_SNAPSHOT_PATH)
    f_snap_positions = f_snap.get("positions", {})
    for sym in config.FUTURES_TRADING_PAIRS:
        try:
            price  = get_mark_price(sym)
            df     = f_klines(sym, limit=250)
            result = compute_signal(
                df,
                rsi_period=config.futures_rsi_period_for(sym),
                rsi_oversold=config.futures_rsi_oversold_for(sym),
                rsi_overbought=config.futures_rsi_overbought_for(sym),
                ema_gap=config.futures_ema_gap_for(sym),
            )
            gap           = price - result.ema_trend
            ema_threshold = result.ema_trend * (1 + config.futures_ema_gap_for(sym))
            above_trend   = price >= ema_threshold
            has_position  = sym in f_snap_positions

            if result.signal == Signal.BUY and not has_position:
                commentary = f"RSI hit oversold ({result.rsi:.1f}) and price is above EMA200 — long signal firing."
            elif result.signal == Signal.SELL and has_position:
                commentary = f"RSI recovered ({result.rsi:.1f}) — trailing stop will manage exit."
            elif result.signal == Signal.SELL and not has_position:
                commentary = f"RSI overbought ({result.rsi:.1f}) but no position — nothing to do."
            elif not above_trend:
                if has_position:
                    commentary = (
                        f"Price is ${abs(gap):,.2f} below EMA200 — holding, "
                        f"watching for RSI to reach {config.futures_rsi_overbought_for(sym)} for exit."
                    )
                elif price >= result.ema_trend:
                    commentary = (
                        f"Price above EMA200 but within {config.futures_ema_gap_for(sym)*100:.0f}% gap filter "
                        f"(need ${ema_threshold:,.2f}, currently ${price:,.2f})."
                    )
                else:
                    commentary = (
                        f"Price is ${abs(gap):,.2f} below EMA200 — downtrend guard active. "
                        f"No longs until price recovers above ${ema_threshold:,.2f}."
                    )
            elif result.rsi >= config.futures_rsi_oversold_for(sym):
                if has_position:
                    commentary = (
                        f"Holding long — RSI {result.rsi:.1f}, watching for recovery above "
                        f"{config.futures_rsi_overbought_for(sym)} to arm trailing stop exit."
                    )
                else:
                    commentary = (
                        f"Trend healthy (${gap:,.2f} above EMA200), "
                        f"RSI {result.rsi:.1f} — waiting for dip below {config.futures_rsi_oversold_for(sym)}."
                    )
            else:
                commentary = result.reason

            pos = f_snap_positions.get(sym)
            pos_data = None
            if pos:
                pos_data = {
                    "entry_price":       pos.get("entry_price"),
                    "mark_price":        round(price, 4),
                    "amount":            pos.get("amount"),
                    "margin":            pos.get("margin"),
                    "leverage":          pos.get("leverage"),
                    "unrealized_pnl":    pos.get("unrealized_pnl"),
                    "pnl_pct":           pos.get("pnl_pct"),
                    "liquidation_price": pos.get("liquidation"),
                    "trailing_stop":     pos.get("trailing_stop"),
                    "take_profit_price": pos.get("take_profit"),
                    "highest_price":     pos.get("highest_price"),
                    "dca_count":         pos.get("dca_count"),
                }

            out[sym] = {
                "price":         round(price, 4),
                "rsi":           round(result.rsi, 1),
                "rsi_period":    config.futures_rsi_period_for(sym),
                "rsi_oversold":  config.futures_rsi_oversold_for(sym),
                "rsi_overbought":config.futures_rsi_overbought_for(sym),
                "ema200":        round(result.ema_trend, 4),
                "ema_threshold": round(ema_threshold, 4),
                "above_trend":   above_trend,
                "gap":           round(gap, 4),
                "signal":        result.signal.value,
                "commentary":    commentary,
                "has_position":  has_position,
                "position":      pos_data,
            }
        except Exception as e:
            out[sym] = {"error": config.scrub_err(e)}
    return jsonify(out)


@app.route("/api/chart/<pair>")
def api_chart(pair):
    if _rate_limited():
        return jsonify({"error": "rate limited"}), 429
    span_candles = {"4h": 16, "1d": 96, "3d": 288, "1w": 672, "2w": 1344, "1m": 2880}
    span  = request.args.get("span", "1d")
    limit = span_candles.get(span, 96)

    df = get_df(pair.upper(), config.SPOT_INTERVAL, limit=limit + 210)
    if df.empty:
        return jsonify({"labels": [], "prices": [], "ema200": [], "rsi": [], "buys": [], "sells": []})

    close = df["close"]

    # EMA200
    ema = close.ewm(span=200, adjust=False).mean()

    # Bollinger Bands (20-period SMA ± 2σ)
    _bb_period = 20
    bb_mid   = close.rolling(_bb_period).mean()
    bb_std   = close.rolling(_bb_period).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std

    # RSI
    _rp = config.rsi_period_for(pair.upper())
    rsi = rsi_series(close, _rp)

    # signal markers — apply strategy logic across each candle
    buy_prices    = [None] * len(df)
    sell_prices   = [None] * len(df)
    blocked_buys  = [None] * len(df)
    for i in range(210, len(df)):
        r   = rsi.iloc[i]
        e   = ema.iloc[i]
        p   = close.iloc[i]
        _ema_threshold = e * (1 + config.ema_gap_for(pair.upper()))
        if r < config.rsi_oversold_for(pair.upper()) and p >= _ema_threshold:
            buy_prices[i] = round(p, 4)
        elif r < config.rsi_oversold_for(pair.upper()):
            blocked_buys[i] = round(p, 4)
        elif r > config.rsi_overbought_for(pair.upper()):
            sell_prices[i] = round(p, 4)

    # slice to requested span — convert to Helsinki time for display
    sl = slice(-limit, None)
    labels = (df["open_time"].iloc[sl]
              .dt.tz_convert(_TZ)
              .dt.strftime("%d.%m. %H:%M").tolist())
    label_set = set(labels)

    # actual executed trades for this pair — map to chart labels
    _interval_mins = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    _candle_mins = _interval_mins.get(config.SPOT_INTERVAL, 15)
    actual_buys  = []
    actual_sells = []
    actual_dcas  = []
    for row in db.get_trades(200, mode=config.SPOT_MODE):
        _, ts, tpair, side, price, *_ = row
        notes = row[-1] or ""
        if tpair != pair.upper():
            continue
        try:
            t = datetime.datetime.fromisoformat(ts).astimezone(_TZ)
            t = t.replace(minute=(t.minute // _candle_mins) * _candle_mins, second=0, microsecond=0)
            label = t.strftime("%d.%m. %H:%M")
        except Exception:
            continue
        if label not in label_set:
            continue
        pt = {"x": label, "y": round(price, 4)}
        if side == "BUY" and "dca" in notes.lower():
            actual_dcas.append(pt)
        elif side == "BUY":
            actual_buys.append(pt)
        elif side == "SELL":
            actual_sells.append(pt)

    volume   = df["volume"]
    vol_up   = (close >= df["open"]).iloc[sl]
    vol_colors = ['#3fb95066' if u else '#f8514966' for u in vol_up]

    ohlc = [{"x": lbl, "o": round(o, 4), "h": round(h, 4), "l": round(l, 4), "c": round(c, 4)}
            for lbl, o, h, l, c in zip(
                labels,
                df["open"].iloc[sl].tolist(),
                df["high"].iloc[sl].tolist(),
                df["low"].iloc[sl].tolist(),
                close.iloc[sl].tolist()
            )]

    return jsonify({
        "labels":        labels,
        "prices":        close.iloc[sl].round(4).tolist(),
        "ema200":        ema.iloc[sl].round(4).tolist(),
        "bb_upper":      bb_upper.iloc[sl].round(4).tolist(),
        "bb_lower":      bb_lower.iloc[sl].round(4).tolist(),
        "ohlc":          ohlc,
        "volume":        volume.iloc[sl].round(4).tolist(),
        "vol_colors":    vol_colors,
        "rsi":           rsi.iloc[sl].round(2).tolist(),
        "buys":          buy_prices[-limit:],
        "sells":         sell_prices[-limit:],
        "blocked_buys":  blocked_buys[-limit:],
        "actual_buys":   actual_buys,
        "actual_sells":  actual_sells,
        "actual_dcas":   actual_dcas,
    })


@app.route("/api/futures/chart/<symbol>")
def api_futures_chart(symbol):
    if _rate_limited():
        return jsonify({"error": "rate limited"}), 429
    if not config.FUTURES_ENABLED:
        return jsonify({"labels": [], "prices": [], "ema200": [], "rsi": [], "buys": [], "sells": []})
    from bot.candles import get_df as _get_df
    span_candles = {"4h": 16, "1d": 96, "3d": 288, "1w": 672, "2w": 1344, "1m": 2880}
    span  = request.args.get("span", "1d")
    limit = span_candles.get(span, 96)
    sym   = symbol.upper()

    df = _get_df(sym, "15m", limit=limit + 210)
    if df.empty:
        return jsonify({"labels": [], "prices": [], "ema200": [], "rsi": [], "buys": [], "sells": []})

    close = df["close"]
    ema   = close.ewm(span=200, adjust=False).mean()

    _bb_period = 20
    bb_mid   = close.rolling(_bb_period).mean()
    bb_std   = close.rolling(_bb_period).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std

    _rp = config.futures_rsi_period_for(sym)
    rsi = rsi_series(close, _rp)

    buy_prices   = [None] * len(df)
    sell_prices  = [None] * len(df)
    blocked_buys = [None] * len(df)
    _ema_gap     = config.futures_ema_gap_for(sym)
    for i in range(210, len(df)):
        r = rsi.iloc[i]; e = ema.iloc[i]; p = close.iloc[i]
        if r < config.futures_rsi_oversold_for(sym) and p >= e * (1 + _ema_gap):
            buy_prices[i] = round(p, 4)
        elif r < config.futures_rsi_oversold_for(sym):
            blocked_buys[i] = round(p, 4)
        elif r > config.futures_rsi_overbought_for(sym):
            sell_prices[i] = round(p, 4)

    sl  = slice(-limit, None)
    labels = (df["open_time"].iloc[sl]
              .dt.tz_convert(_TZ)
              .dt.strftime("%d.%m. %H:%M").tolist())
    label_set = set(labels)

    actual_buys = []; actual_sells = []; actual_dcas = []
    for row in db.get_trades(200, mode=f"futures_{config.FUTURES_MODE}"):
        _, ts, tpair, side, price, *_ = row
        notes = row[-1] or ""
        if tpair != sym:
            continue
        try:
            t = datetime.datetime.fromisoformat(ts).astimezone(_TZ)
            t = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
            label = t.strftime("%d.%m. %H:%M")
        except Exception:
            continue
        if label not in label_set:
            continue
        pt = {"x": label, "y": round(price, 4)}
        if side == "BUY" and "dca" in notes.lower():
            actual_dcas.append(pt)
        elif side == "BUY":
            actual_buys.append(pt)
        elif side == "SELL":
            actual_sells.append(pt)

    vol_up     = (close >= df["open"]).iloc[sl]
    vol_colors = ['#3fb95066' if u else '#f8514966' for u in vol_up]
    ohlc = [{"x": lbl, "o": round(o, 4), "h": round(h, 4), "l": round(l, 4), "c": round(c, 4)}
            for lbl, o, h, l, c in zip(
                labels,
                df["open"].iloc[sl].tolist(), df["high"].iloc[sl].tolist(),
                df["low"].iloc[sl].tolist(),  close.iloc[sl].tolist())]

    return jsonify({
        "labels":       labels,
        "prices":       close.iloc[sl].round(4).tolist(),
        "ema200":       ema.iloc[sl].round(4).tolist(),
        "bb_upper":     bb_upper.iloc[sl].round(4).tolist(),
        "bb_lower":     bb_lower.iloc[sl].round(4).tolist(),
        "ohlc":         ohlc,
        "volume":       df["volume"].iloc[sl].round(4).tolist(),
        "vol_colors":   vol_colors,
        "rsi":          rsi.iloc[sl].round(2).tolist(),
        "buys":         buy_prices[-limit:],
        "sells":        sell_prices[-limit:],
        "blocked_buys": blocked_buys[-limit:],
        "actual_buys":  actual_buys,
        "actual_sells": actual_sells,
        "actual_dcas":  actual_dcas,
    })


_ENGINE_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "kairos.log")


def _tail_log(path: str, n: int = 150, chunk: int = 8192) -> list[str]:
    """Read the last `n` lines of a file without loading it all into memory."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            buf  = b""
            pos  = size
            while len(buf.splitlines()) <= n and pos > 0:
                pos = max(pos - chunk, 0)
                f.seek(pos)
                buf = f.read(size - pos)
            lines = buf.decode("utf-8", errors="replace").splitlines()
            return lines[-n:]
    except Exception:
        return []


@app.route("/api/log")
def api_log():
    if os.environ.get("KAIROS_DASHBOARD_ONLY"):
        out = []
        for l in _tail_log(_ENGINE_LOG_PATH):
            # format: "YYYY-MM-DD HH:MM:SS  vX.X.X  LEVEL  message"
            parts = l.split("  ", 3)
            if len(parts) >= 4:
                out.append({"time": parts[0].replace(" ", "T"), "msg": parts[3]})
            elif l:
                out.append({"time": "", "msg": l})
        return jsonify(list(reversed(out)))
    return jsonify(get_recent_logs())


@app.route("/api/futures/balance_history")
def api_futures_balance_history():
    if not config.FUTURES_ENABLED:
        return jsonify([])
    days = float(request.args.get("days", 30))
    rows = db.get_balance_history(f"futures_{config.FUTURES_MODE}", max(days, 0))
    out = []
    for ts, bal in rows:
        try:
            t = datetime.datetime.fromisoformat(ts).astimezone(_TZ).strftime("%d.%m. %H:%M")
        except Exception:
            t = ts
        out.append({"t": t, "balance": bal})
    return jsonify(out)


@app.route("/api/futures/trades")
def api_futures_trades():
    if not config.FUTURES_ENABLED:
        return jsonify({"trades": [], "total": 0, "page": 1, "pages": 1})
    cols   = ["id", "timestamp", "pair", "side", "price", "amount",
              "value_eur", "fee", "mode", "pnl", "notes"]
    limit  = int(request.args.get("limit", 15))
    page   = max(1, int(request.args.get("page", 1)))
    offset = (page - 1) * limit
    rows   = db.get_trades(limit, mode=f"futures_{config.FUTURES_MODE}", offset=offset)
    total  = db.get_trade_count(mode=f"futures_{config.FUTURES_MODE}")
    return jsonify({
        "trades": [dict(zip(cols, row)) for row in rows],
        "total":  total,
        "page":   page,
        "pages":  max(1, math.ceil(total / limit)),
    })


@app.route("/api/futures/trades/csv")
def api_futures_trades_csv():
    if not config.FUTURES_ENABLED:
        return Response("not found", status=404)
    cols = ["id", "timestamp", "pair", "side", "price", "amount",
            "value_eur", "fee", "mode", "pnl", "notes"]
    rows = db.get_trades(9999, mode=f"futures_{config.FUTURES_MODE}")
    lines = [",".join(cols)]
    for row in rows:
        lines.append(",".join("" if v is None else str(v) for v in row))
    return Response("\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=futures_trades.csv"})


@app.route("/api/futures/stats")
def api_futures_stats():
    if not config.FUTURES_ENABLED:
        return jsonify(None)
    return jsonify(db.get_trade_stats(f"futures_{config.FUTURES_MODE}"))


@app.route("/api/futures/shadows")
def api_futures_shadows():
    if not config.FUTURES_ENABLED:
        return jsonify([])
    if _rate_limited():
        return jsonify({"error": "rate limited"}), 429
    from bot.futures_simulator import get_futures_shadows
    from bot.futures_exchange import get_mark_price as _gmp
    main_shim = _MainFuturesShim(_read_snapshot(_FUTURES_SNAPSHOT_PATH))
    shims = list(get_futures_shadows())

    def _portfolio(sh):
        prices = {}
        for sym in sh.symbols:
            try:
                prices[sym] = _gmp(sym)
            except Exception:
                pass
        open_pnl = sum(p.unrealized_pnl(prices[s]) for s, p in sh.positions.items() if s in prices)
        return sh.balance + open_pnl

    main_portfolio = _portfolio(main_shim)
    main_ret = round((main_portfolio / main_shim._starting - 1) * 100, 2) if main_shim._starting > 0 else 0

    rows = []
    for sh in shims:
        portfolio = _portfolio(sh)
        starting  = sh._starting
        ret       = round((portfolio / starting - 1) * 100, 2) if starting > 0 else 0
        is_main   = getattr(sh, "is_main", False)
        if not is_main and hasattr(sh, "set_main_baseline"):
            sh.set_main_baseline(main_ret)
        baseline  = getattr(sh, "main_baseline_pct", None)
        vs_main   = round(ret - (main_ret - baseline), 2) if (not is_main and baseline is not None) else None
        rows.append({
            "name":       sh.name,
            "is_main":    is_main,
            "overrides":  sh.overrides,
            "symbols":    sh.symbols,
            "balance":    round(sh.balance, 2),
            "portfolio":  round(portfolio, 2),
            "starting":   starting,
            "return_pct": ret,
            "vs_main":    vs_main,
            "pnl":        round(sh.total_pnl, 4),
            "funding":    round(sh.total_funding, 4),
            "trades":     sh.total_trades,
        })
    rows.sort(key=lambda x: (not x["is_main"], -x["return_pct"]))
    return jsonify(rows)


def _get_futures_shadow(name: str):
    from bot.futures_simulator import get_futures_shadows
    if name.upper() == "MAIN":
        return _MainFuturesShim(_read_snapshot(_FUTURES_SNAPSHOT_PATH))
    return next((s for s in get_futures_shadows() if s.name.upper() == name.upper()), None)


@app.route("/api/futures/shadow/<name>/status")
def api_futures_shadow_status(name):
    if not config.FUTURES_ENABLED:
        return jsonify({"error": "disabled"}), 404
    sh = _get_futures_shadow(name)
    if sh is None:
        return jsonify({"error": "not found"}), 404
    from bot.futures_exchange import get_mark_price as _gmp
    prices = {}
    for sym in sh.symbols:
        try:
            prices[sym] = _gmp(sym)
        except Exception:
            pass
    open_pnl = sum(p.unrealized_pnl(prices[s]) for s, p in sh.positions.items() if s in prices)
    starting = sh._starting
    import sqlite3 as _sq3
    conn = _sq3.connect(db.DB_PATH)
    row  = conn.execute("SELECT MIN(timestamp) FROM balance_history WHERE mode = ?", (sh._db_mode,)).fetchone()
    conn.close()
    return jsonify({
        "name":          sh.name,
        "is_main":       getattr(sh, "is_main", False),
        "overrides":     sh.overrides,
        "symbols":       sh.symbols,
        "balance":       round(sh.balance, 2),
        "total_trades":  sh.total_trades,
        "total_pnl":     round(sh.total_pnl, 4),
        "total_fees":    round(sh.total_fees, 4),
        "total_funding": round(sh.total_funding, 4),
        "portfolio":     round(sh.balance + open_pnl, 2),
        "starting":      starting,
        "started_at":    row[0] if row and row[0] else None,
    })


@app.route("/api/futures/shadow/<name>/trades")
def api_futures_shadow_trades(name):
    if not config.FUTURES_ENABLED:
        return jsonify({"trades": [], "total": 0, "page": 1, "pages": 1})
    sh = _get_futures_shadow(name)
    if sh is None:
        return jsonify({"trades": [], "total": 0, "page": 1, "pages": 1})
    cols   = ["id", "timestamp", "pair", "side", "price", "amount", "value_eur", "fee", "mode", "pnl", "notes"]
    limit  = int(request.args.get("limit", 15))
    page   = max(1, int(request.args.get("page", 1)))
    offset = (page - 1) * limit
    rows   = db.get_trades(limit, mode=sh._db_mode, offset=offset)
    total  = db.get_trade_count(mode=sh._db_mode)
    pages  = max(1, math.ceil(total / limit))
    hold_times = db.get_hold_times(sh._db_mode)
    trades = []
    for row in rows:
        d = dict(zip(cols, row))
        try:
            d["timestamp"] = datetime.datetime.fromisoformat(d["timestamp"]).astimezone(_TZ).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        d["hold_h"] = hold_times.get(d["id"])
        trades.append(d)
    return jsonify({"trades": trades, "total": total, "page": page, "pages": pages})


@app.route("/api/futures/shadow/<name>/trades/csv")
def api_futures_shadow_trades_csv(name):
    if not config.FUTURES_ENABLED:
        return Response("", mimetype="text/csv")
    sh = _get_futures_shadow(name)
    if sh is None:
        return Response("", mimetype="text/csv")
    cols = ["id", "timestamp", "pair", "side", "price", "amount", "value_eur", "fee", "mode", "pnl", "notes"]
    rows = db.get_trades(9999, mode=sh._db_mode)
    lines = [",".join(cols)]
    for row in rows:
        lines.append(",".join("" if v is None else str(v) for v in row))
    return Response("\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=futures_shadow_{name.lower()}_trades.csv"})


@app.route("/api/futures/shadow/<name>/stats")
def api_futures_shadow_stats(name):
    if not config.FUTURES_ENABLED:
        return jsonify(None)
    sh = _get_futures_shadow(name)
    if sh is None:
        return jsonify(None)
    return jsonify(db.get_trade_stats(sh._db_mode))


@app.route("/api/futures/shadow/<name>/balance_history")
def api_futures_shadow_balance_history(name):
    if not config.FUTURES_ENABLED:
        return jsonify([])
    sh = _get_futures_shadow(name)
    if sh is None:
        return jsonify([])
    days = float(request.args.get("days", 30))
    rows = db.get_balance_history(sh._db_mode, max(days, 0))
    out  = []
    for ts, bal in rows:
        try:
            t = datetime.datetime.fromisoformat(ts).astimezone(_TZ).strftime("%d.%m. %H:%M")
        except Exception:
            t = ts
        out.append({"t": t, "balance": round(bal, 2)})
    return jsonify(out)


@app.route("/api/futures/shadows/pnl_history")
def api_futures_shadows_pnl_history():
    if not config.FUTURES_ENABLED:
        return jsonify({})
    from bot.futures_simulator import get_futures_shadows
    days = float(request.args.get("days", 30))
    out  = {}

    def _series(db_mode, starting):
        rows = db.get_balance_history(db_mode, max(days, 0))
        result = []
        if rows and rows[0][1] < starting * 0.95:
            first_ts, _ = rows[0]
            try:
                dt0 = datetime.datetime.fromisoformat(first_ts).astimezone(_TZ) - datetime.timedelta(minutes=1)
                result.append({"t": dt0.strftime("%d.%m. %H:%M"), "sk": dt0.timestamp(), "pnl": 0.0, "pct": 0.0})
            except Exception:
                pass
        for ts, bal in rows:
            try:
                dt = datetime.datetime.fromisoformat(ts).astimezone(_TZ)
                t  = dt.strftime("%d.%m. %H:%M")
                sk = dt.timestamp()
            except Exception:
                t = ts; sk = 0
            pnl = bal - starting
            result.append({"t": t, "sk": sk, "pnl": round(pnl, 4), "pct": round(pnl / starting * 100, 3) if starting else 0})
        return result

    if config.FUTURES_MODE == "simulation":
        out["MAIN"] = _series(f"futures_{config.FUTURES_MODE}", config.FUTURES_SIMULATION_BALANCE)
    for sh in get_futures_shadows():
        out[sh.name] = _series(sh._db_mode, sh._starting)
    return jsonify(out)


@app.route("/api/futures/status")
def api_futures_status():
    if not config.FUTURES_ENABLED:
        return jsonify({"enabled": False})
    snap = _read_snapshot(_FUTURES_SNAPSHOT_PATH)
    snap["enabled"] = True
    snap["stale"] = _snapshot_stale(snap)
    snap["dca_drop"] = config.FUTURES_DCA_DROP_PCT
    snap["stop_check_interval"] = config.FUTURES_STOP_CHECK_INTERVAL
    snap["leverage"] = config.FUTURES_LEVERAGE
    snap["running"] = snap.get("status") == "running"
    snap["paused"]  = snap.get("status") == "paused"
    # rename trailing_stop → trailing_stop, take_profit → take_profit_price for dashboard compat
    for pos in snap.get("positions", {}).values():
        if "take_profit" in pos and "take_profit_price" not in pos:
            pos["take_profit_price"] = pos["take_profit"]
        if "current_price" in pos and "mark_price" not in pos:
            pos["mark_price"] = pos["current_price"]
    return jsonify(snap)


import json as _json
_PIN_MAX_ATTEMPTS   = 5
_PIN_LOCK_DURATION  = 3600  # seconds; auto-expire lockout after 1 hour
_PIN_FAILURES_PATH  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "pin_lockouts.json")


def _load_lockouts() -> dict:
    try:
        with open(_PIN_FAILURES_PATH) as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_lockouts(data: dict):
    os.makedirs(os.path.dirname(_PIN_FAILURES_PATH), exist_ok=True)
    tmp = _PIN_FAILURES_PATH + ".tmp"
    with open(tmp, "w") as f:
        _json.dump(data, f)
    os.replace(tmp, _PIN_FAILURES_PATH)


def _client_ip() -> str:
    """Return the client IP. Trusts X-Forwarded-For only when TRUSTED_PROXY env var is set,
    otherwise falls back to remote_addr to avoid header spoofing bypasses."""
    if os.environ.get("TRUSTED_PROXY"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _check_pin() -> tuple[bool, bool]:
    """Returns (allowed, rate_limited). Lockouts auto-expire after _PIN_LOCK_DURATION."""
    if not config.DASHBOARD_PIN:
        return True, False
    ip       = _client_ip()
    now      = _time.time()
    lockouts = _load_lockouts()
    entry    = lockouts.get(ip, {"attempts": 0, "locked": False, "locked_at": 0})

    # Auto-expire stale lockouts
    if entry.get("locked") and (now - float(entry.get("locked_at", 0)) > _PIN_LOCK_DURATION):
        entry = {"attempts": 0, "locked": False, "locked_at": 0}
        lockouts.pop(ip, None)
        _save_lockouts(lockouts)

    if entry.get("locked"):
        return False, True

    correct = hmac.compare_digest(
        request.headers.get("X-Dashboard-Pin", ""),
        config.DASHBOARD_PIN,
    )
    if not correct:
        entry["attempts"] = entry.get("attempts", 0) + 1
        if entry["attempts"] >= _PIN_MAX_ATTEMPTS:
            entry["locked"]    = True
            entry["locked_at"] = now
        lockouts[ip] = entry
        _save_lockouts(lockouts)
    else:
        lockouts.pop(ip, None)  # clear on success
        _save_lockouts(lockouts)
    return correct, entry.get("locked", False)


@app.route("/api/futures/control", methods=["POST"])
def api_futures_control():
    ok, limited = _check_pin()
    if limited:
        return jsonify({"error": "too many attempts — IP locked, run unlock_pin.sh on server"}), 429
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    if os.environ.get("KAIROS_DASHBOARD_ONLY"):
        return jsonify({"error": "engine runs in a separate process — control not available via dashboard"}), 503
    if not config.FUTURES_ENABLED:
        return jsonify({"error": "futures not enabled"}), 400
    from bot import futures_engine
    data   = request.get_json(force=True)
    action = data.get("action")
    symbol = data.get("symbol", "").upper()
    if action == "start":
        futures_engine.start()
    elif action == "pause":
        futures_engine.pause()
    elif action == "resume":
        futures_engine.resume()
    elif action == "stop":
        futures_engine.stop()
    elif action == "manual_buy" and symbol:
        size_pct = float(data.get("size_pct", config.FUTURES_POSITION_SIZE_PCT))
        futures_engine.manual_buy(symbol, size_pct)
    elif action == "manual_close" and symbol:
        futures_engine.manual_close(symbol)
    return jsonify({"status": futures_engine.get_status()})


@app.route("/api/control", methods=["POST"])
def api_control():
    ok, limited = _check_pin()
    if limited:
        return jsonify({"error": "too many attempts — IP locked, run unlock_pin.sh on server"}), 429
    if not ok:
        return jsonify({"error": "unauthorized"}), 403
    if os.environ.get("KAIROS_DASHBOARD_ONLY"):
        return jsonify({"error": "engine runs in a separate process — control not available via dashboard"}), 503
    data = request.get_json(force=True)
    action = data.get("action")
    pair = data.get("pair")

    if action == "ping":
        return jsonify({"ok": True})
    if action == "start":
        engine.start()
    elif action == "pause":
        engine.pause()
    elif action == "resume":
        engine.resume()
    elif action == "stop":
        engine.stop()
    elif action == "manual_close" and pair:
        manual_close(pair)
    elif action == "manual_buy" and pair:
        size_pct = float(data.get("size_pct", 0.5))
        manual_buy(pair, size_pct)
    elif action == "sync_position" and pair:
        from bot.spot_simulator import sync_position_from_binance
        result = sync_position_from_binance(pair)
        return jsonify({"status": engine.get_status(), "sync": result})

    return jsonify({"status": engine.get_status()})


def run(host: str = None, port: int = None):
    import logging
    _log = logging.getLogger("cryptobot")

    ssl_context = None
    if config.WEB_SSL_CERT and config.WEB_SSL_KEY:
        if os.path.exists(config.WEB_SSL_CERT) and os.path.exists(config.WEB_SSL_KEY):
            ssl_context = (config.WEB_SSL_CERT, config.WEB_SSL_KEY)
            _log.info(f"HTTPS enabled — cert={config.WEB_SSL_CERT}")
        else:
            _log.warning(
                f"WEB_SSL_CERT/WEB_SSL_KEY set but files not found "
                f"({config.WEB_SSL_CERT}, {config.WEB_SSL_KEY}) — falling back to HTTP"
            )

    if config.DASHBOARD_PIN and len(config.DASHBOARD_PIN) < 6:
        _log.warning(
            "DASHBOARD_PIN is set but shorter than 6 characters — "
            "use a longer PIN to reduce brute-force risk"
        )
    elif not config.DASHBOARD_PIN:
        _log.warning("DASHBOARD_PIN is not set — control endpoints are unprotected")

    if not ssl_context and config.DASHBOARD_PIN:
        _log.warning("HTTPS is not enabled — PIN is transmitted in cleartext")

    if config.WEB_HOST != "127.0.0.1" and not config.DASHBOARD_PIN:
        _log.critical(
            "FATAL: WEB_HOST is set to a public address (%s) but DASHBOARD_PIN is empty. "
            "Control endpoints would be fully unauthenticated. "
            "Set DASHBOARD_PIN or bind to 127.0.0.1.",
            config.WEB_HOST,
        )
        raise SystemExit(1)

    app.run(
        host=host or config.WEB_HOST,
        port=port or config.WEB_PORT,
        debug=False,
        use_reloader=False,
        ssl_context=ssl_context,
    )
