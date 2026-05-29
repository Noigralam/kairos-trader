from flask import Flask, jsonify, render_template, request, send_from_directory
from bot import engine, simulator, db, config
from bot.engine import get_last_tick, get_uptime, get_live_since, manual_buy
from bot.notifier import get_recent_logs
from bot.exchange import get_price
from bot.candles import get_df
from bot.strategy import compute_signal, Signal

app = Flask(__name__, static_folder="static")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon.ico", mimetype="image/x-icon")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    state = simulator.get_state()
    return jsonify({
        "status": engine.get_status(),
        "last_tick": get_last_tick(),
        "uptime": get_uptime(),
        "live_since": get_live_since(),
        "mode": config.MODE,
        "balance": round(state.balance, 2),
        "total_trades": state.total_trades,
        "total_pnl": round(state.total_pnl, 2),
        "total_fees": round(state.total_fees, 4),
        "positions": {
            pair: {
                "entry_price": pos.entry_price,
                "amount": pos.amount,
                "value_eur": round(pos.value_eur, 2),
                "highest_price": pos.peak(),
                "trailing_stop": round(pos.trailing_stop_level(), 2),
                "take_profit": pos.take_profit_price,
                "dca_trigger": round(pos.entry_price * (1 - config.DCA_DROP_PCT), 2) if not pos.dca_done else None,
                "break_even": round(pos.entry_price * (1 + config.BINANCE_FEE) / (1 - config.BINANCE_FEE), 2),
                "current_price": round(get_price(pair), 2),
            }
            for pair, pos in state.positions.items()
        },
    })


@app.route("/api/trades")
def api_trades():
    cols = ["id", "timestamp", "pair", "side", "price", "amount",
            "value_eur", "fee", "mode", "pnl", "notes"]
    rows = db.get_trades(50, mode=config.MODE)
    return jsonify([dict(zip(cols, row)) for row in rows])


@app.route("/api/tax")
def api_tax():
    rows = db.get_tax_summary()
    return jsonify([
        {"year": r[0], "gains": r[1], "losses": r[2], "net_pnl": r[3]}
        for r in rows
    ])


@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_trade_stats(config.MODE))


@app.route("/api/balance_history")
def api_balance_history():
    days = float(request.args.get("days", 30))
    rows = db.get_balance_history(config.MODE, max(days, 0))
    import zoneinfo as _zi
    tz = _zi.ZoneInfo("Europe/Helsinki")
    import datetime as _dt
    out = []
    for ts, bal in rows:
        try:
            t = _dt.datetime.fromisoformat(ts).astimezone(tz).strftime("%m-%d %H:%M")
        except Exception:
            t = ts
        out.append({"t": t, "balance": bal})
    return jsonify(out)


@app.route("/api/fng")
def api_fng():
    from bot.notifier import get_fng
    value, label = get_fng()
    return jsonify({"value": value, "label": label})


@app.route("/api/signals")
def api_signals():
    out = {}
    state = simulator.get_state()
    for pair in config.TRADING_PAIRS:
        try:
            price = get_price(pair)
            result = compute_signal(get_df(pair, config.INTERVAL),
                                    rsi_period=config.rsi_period_for(pair),
                                    rsi_oversold=config.RSI_OVERSOLD,
                                    rsi_overbought=config.rsi_overbought_for(pair))
            gap = price - result.ema_trend
            above_trend = gap >= 0
            has_position = pair in state.positions

            if result.signal == Signal.BUY and not has_position:
                commentary = f"RSI hit oversold ({result.rsi:.1f}) and price is above EMA200 — buy signal firing."
            elif result.signal == Signal.SELL and has_position:
                commentary = f"RSI recovered ({result.rsi:.1f}) — sell signal firing."
            elif result.signal == Signal.SELL and not has_position:
                commentary = f"RSI overbought ({result.rsi:.1f}) but no position held — nothing to sell."
            elif not above_trend:
                commentary = (
                    f"Price is €{abs(gap):,.2f} below EMA200 — downtrend guard active. "
                    f"No buys until price recovers above €{result.ema_trend:,.2f}."
                )
            elif result.rsi >= config.RSI_OVERSOLD:
                commentary = (
                    f"Trend is healthy (price above EMA200 by €{gap:,.2f}), "
                    f"but RSI is {result.rsi:.1f} — waiting for a dip below {config.RSI_OVERSOLD}."
                )
            else:
                commentary = result.reason

            out[pair] = {
                "price": round(price, 2),
                "rsi": round(result.rsi, 1),
                "rsi_period": config.rsi_period_for(pair),
                "rsi_overbought": config.rsi_overbought_for(pair),
                "ema200": round(result.ema_trend, 2),
                "above_trend": above_trend,
                "gap": round(gap, 2),
                "signal": result.signal.value,
                "reason": result.reason,
                "commentary": commentary,
                "has_position": has_position,
            }
        except Exception as e:
            out[pair] = {"error": str(e)}
    return jsonify(out)


@app.route("/api/chart/<pair>")
def api_chart(pair):
    import pandas as pd
    span_candles = {"4h": 16, "1d": 96, "3d": 288, "1w": 672}
    span  = request.args.get("span", "1d")
    limit = span_candles.get(span, 96)

    df = get_df(pair.upper(), config.INTERVAL, limit=limit + 210)
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
    _rp      = config.rsi_period_for(pair.upper())
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=_rp - 1, min_periods=_rp).mean()
    avg_loss = loss.ewm(com=_rp - 1, min_periods=_rp).mean()
    rs       = avg_gain / avg_loss
    rsi      = 100 - (100 / (1 + rs))

    # signal markers — apply strategy logic across each candle
    buy_prices    = [None] * len(df)
    sell_prices   = [None] * len(df)
    blocked_buys  = [None] * len(df)
    for i in range(210, len(df)):
        r   = rsi.iloc[i]
        e   = ema.iloc[i]
        p   = close.iloc[i]
        if r < config.RSI_OVERSOLD and p > e:
            buy_prices[i] = round(p, 4)
        elif r < config.RSI_OVERSOLD and p <= e:
            blocked_buys[i] = round(p, 4)
        elif r > config.rsi_overbought_for(pair.upper()):
            sell_prices[i] = round(p, 4)

    # slice to requested span — convert to Helsinki time for display
    import zoneinfo as _zi
    sl = slice(-limit, None)
    labels = (df["open_time"].iloc[sl]
              .dt.tz_convert(_zi.ZoneInfo("Europe/Helsinki"))
              .dt.strftime("%m-%d %H:%M").tolist())
    label_set = set(labels)

    # actual executed trades for this pair — map to chart labels
    import datetime as _dt, zoneinfo as _zi
    _tz = _zi.ZoneInfo("Europe/Helsinki")
    _interval_mins = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    _candle_mins = _interval_mins.get(config.INTERVAL, 15)
    actual_buys  = []
    actual_sells = []
    actual_dcas  = []
    for row in db.get_trades(200, mode=config.MODE):
        _, ts, tpair, side, price, *_ = row
        notes = row[-1] or ""
        if tpair != pair.upper():
            continue
        try:
            t = _dt.datetime.fromisoformat(ts).astimezone(_tz)
            # floor to candle boundary so label matches chart x-axis
            t = t.replace(minute=(t.minute // _candle_mins) * _candle_mins, second=0, microsecond=0)
            label = t.strftime("%m-%d %H:%M")
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


@app.route("/api/log")
def api_log():
    return jsonify(get_recent_logs())


@app.route("/api/control", methods=["POST"])
def api_control():
    data = request.get_json(force=True)
    action = data.get("action")
    pair = data.get("pair")

    if action == "start":
        engine.start()
    elif action == "pause":
        engine.pause()
    elif action == "resume":
        engine.resume()
    elif action == "stop":
        engine.stop()
    elif action == "override" and pair:
        engine.override_close(pair)
    elif action == "manual_buy" and pair:
        size_pct = float(data.get("size_pct", 0.5))
        manual_buy(pair, size_pct)

    return jsonify({"status": engine.get_status()})


def run(host: str = None, port: int = None):
    app.run(
        host=host or config.WEB_HOST,
        port=port or config.WEB_PORT,
        debug=False,
        use_reloader=False,
    )
