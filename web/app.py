from flask import Flask, jsonify, render_template, request, send_from_directory
from bot import engine, simulator, db, config
from bot.engine import get_last_tick
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
            }
            for pair, pos in state.positions.items()
        },
    })


@app.route("/api/trades")
def api_trades():
    cols = ["id", "timestamp", "pair", "side", "price", "amount",
            "value_eur", "fee", "mode", "pnl", "notes"]
    rows = db.get_trades(50)
    return jsonify([dict(zip(cols, row)) for row in rows])


@app.route("/api/tax")
def api_tax():
    rows = db.get_tax_summary()
    return jsonify([
        {"year": r[0], "gains": r[1], "losses": r[2], "net_pnl": r[3]}
        for r in rows
    ])


@app.route("/api/signals")
def api_signals():
    out = {}
    state = simulator.get_state()
    for pair in config.TRADING_PAIRS:
        try:
            price = get_price(pair)
            result = compute_signal(get_df(pair, config.INTERVAL))
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
            elif result.rsi >= 30:
                commentary = (
                    f"Trend is healthy (price above EMA200 by €{gap:,.2f}), "
                    f"but RSI is {result.rsi:.1f} — waiting for a dip below 30."
                )
            else:
                commentary = result.reason

            out[pair] = {
                "price": round(price, 2),
                "rsi": round(result.rsi, 1),
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

    return jsonify({"status": engine.get_status()})


def run(host: str = None, port: int = None):
    app.run(
        host=host or config.WEB_HOST,
        port=port or config.WEB_PORT,
        debug=False,
        use_reloader=False,
    )
