from flask import Flask, jsonify, render_template, request, send_from_directory
from bot import spot_engine as engine, spot_simulator as simulator, db, config
from bot.spot_engine import get_last_tick, get_uptime, get_live_since, manual_buy
from bot.notifier import get_recent_logs
from bot.spot_exchange import get_price
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
        "mode": config.SPOT_MODE,
        "interval": config.SPOT_INTERVAL,
        "stop_check_interval": config.SPOT_STOP_CHECK_INTERVAL,
        "balance": round(state.balance, 2),
        "total_trades": state.total_trades,
        "total_pnl": round(state.total_pnl, 2),
        "total_fees": round(state.total_fees, 4),
        "starting_balance": db.get_starting_balance(config.SPOT_MODE),
        "positions": {
            pair: {
                "entry_price": pos.entry_price,
                "amount": pos.amount,
                "value_eur": round(pos.value_eur, 2),
                "highest_price": pos.peak(),
                "trailing_stop": round(pos.trailing_stop_level(), 2),
                "take_profit": pos.take_profit_price,
                "dca_trigger": round(pos.entry_price * (1 - (config.SPOT_DCA_DROP_PCT + pos.dca_count * config.SPOT_DCA_STEP_PCT)), 2) if pos.dca_count < config.SPOT_DCA_MAX else None,
                "dca_count":   pos.dca_count,
                "dca_max":     config.SPOT_DCA_MAX,
                "break_even": round(pos.entry_price * (1 + config.SPOT_FEE) / (1 - config.SPOT_FEE), 2),
                "current_price": round(get_price(pair), 2),
            }
            for pair, pos in state.positions.items()
        },
    })


@app.route("/api/trades")
def api_trades():
    cols = ["id", "timestamp", "pair", "side", "price", "amount",
            "value_eur", "fee", "mode", "pnl", "notes"]
    rows = db.get_trades(50, mode=config.SPOT_MODE)
    return jsonify([dict(zip(cols, row)) for row in rows])


@app.route("/api/tax")
def api_tax():
    rows = db.get_tax_summary()
    return jsonify([
        {"year": r[0], "gains": r[1], "losses": r[2], "net_pnl": r[3]}
        for r in rows
    ])


@app.route("/api/futures/tax")
def api_futures_tax():
    rows = db.get_tax_summary(mode="futures_live")
    return jsonify([
        {"year": r[0], "gains": r[1], "losses": r[2], "net_pnl": r[3]}
        for r in rows
    ])


@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_trade_stats(config.SPOT_MODE))


@app.route("/api/balance_history")
def api_balance_history():
    days = float(request.args.get("days", 30))
    rows = db.get_balance_history(config.SPOT_MODE, max(days, 0))
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
    for pair in config.SPOT_TRADING_PAIRS:
        try:
            price = get_price(pair)
            def _daily_ema_val(pair: str):
                if not config.SPOT_DAILY_EMA_FILTER:
                    return None
                from bot.candles import get_df as _gdf
                df1d = _gdf(pair, "1d", limit=210)
                if len(df1d) < 201:
                    return None
                return float(df1d["close"].ewm(span=200, adjust=False).mean().iloc[-1])

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
            has_position = pair in state.positions

            if result.signal == Signal.BUY and not has_position:
                commentary = f"RSI hit oversold ({result.rsi:.1f}) and price is above EMA200 — buy signal firing."
            elif result.signal == Signal.SELL and has_position:
                commentary = f"RSI recovered ({result.rsi:.1f}) — sell signal firing."
            elif result.signal == Signal.SELL and not has_position:
                commentary = f"RSI overbought ({result.rsi:.1f}) but no position held — nothing to sell."
            elif not above_trend:
                if has_position:
                    commentary = (
                        f"Price is €{abs(gap):,.2f} below EMA200 — holding position, "
                        f"watching for RSI to reach {config.rsi_overbought_for(pair)} for exit."
                    )
                elif price >= result.ema_trend:
                    commentary = (
                        f"Price is above EMA200 but within the {config.ema_gap_for(pair)*100:.0f}% gap filter "
                        f"(need €{ema_threshold:,.2f}, currently €{price:,.2f}). "
                        f"Waiting for stronger trend confirmation."
                    )
                else:
                    commentary = (
                        f"Price is €{abs(gap):,.2f} below EMA200 — downtrend guard active. "
                        f"No buys until price recovers above €{ema_threshold:,.2f} "
                        f"(EMA200 €{result.ema_trend:,.2f} + {config.ema_gap_for(pair)*100:.0f}% gap)."
                    )
            elif result.rsi >= config.rsi_oversold_for(pair):
                if has_position:
                    commentary = (
                        f"Holding — RSI is {result.rsi:.1f}, watching for recovery above "
                        f"{config.rsi_overbought_for(pair)} to trigger sell."
                    )
                else:
                    commentary = (
                        f"Trend is healthy (price above EMA200 by €{gap:,.2f}), "
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
            out[pair] = {"error": str(e)}
    return jsonify(out)


@app.route("/api/futures/signals")
def api_futures_signals():
    if not config.FUTURES_ENABLED:
        return jsonify({})
    from bot.futures_exchange import get_klines as f_klines, get_mark_price
    from bot.futures_simulator import get_state as fget_state
    out = {}
    state = fget_state()
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
            has_position  = sym in state.positions

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

            pos = state.positions.get(sym)
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
                    "dca_done":          pos.dca_done,
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
            out[sym] = {"error": str(e)}
    return jsonify(out)


@app.route("/api/chart/<pair>")
def api_chart(pair):
    import pandas as pd
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
        _ema_threshold = e * (1 + config.ema_gap_for(pair.upper()))
        if r < config.rsi_oversold_for(pair.upper()) and p >= _ema_threshold:
            buy_prices[i] = round(p, 4)
        elif r < config.rsi_oversold_for(pair.upper()):
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


@app.route("/api/futures/chart/<symbol>")
def api_futures_chart(symbol):
    if not config.FUTURES_ENABLED:
        return jsonify({"labels": [], "prices": [], "ema200": [], "rsi": [], "buys": [], "sells": []})
    import pandas as pd
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

    _rp      = config.futures_rsi_period_for(sym)
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=_rp - 1, min_periods=_rp).mean()
    avg_loss = loss.ewm(com=_rp - 1, min_periods=_rp).mean()
    rsi      = 100 - (100 / (1 + avg_gain / avg_loss))

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

    import zoneinfo as _zi, datetime as _dt
    _tz = _zi.ZoneInfo("Europe/Helsinki")
    sl  = slice(-limit, None)
    labels = (df["open_time"].iloc[sl]
              .dt.tz_convert(_tz)
              .dt.strftime("%m-%d %H:%M").tolist())
    label_set = set(labels)

    actual_buys = []; actual_sells = []; actual_dcas = []
    for row in db.get_trades(200, mode=f"futures_{config.FUTURES_MODE}"):
        _, ts, tpair, side, price, *_ = row
        notes = row[-1] or ""
        if tpair != sym:
            continue
        try:
            t = _dt.datetime.fromisoformat(ts).astimezone(_tz)
            t = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
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


@app.route("/api/log")
def api_log():
    return jsonify(get_recent_logs())


@app.route("/api/futures/balance_history")
def api_futures_balance_history():
    if not config.FUTURES_ENABLED:
        return jsonify([])
    days = float(request.args.get("days", 30))
    rows = db.get_balance_history(f"futures_{config.FUTURES_MODE}", max(days, 0))
    import zoneinfo as _zi, datetime as _dt
    tz = _zi.ZoneInfo("Europe/Helsinki")
    out = []
    for ts, bal in rows:
        try:
            t = _dt.datetime.fromisoformat(ts).astimezone(tz).strftime("%m-%d %H:%M")
        except Exception:
            t = ts
        out.append({"t": t, "balance": bal})
    return jsonify(out)


@app.route("/api/futures/trades")
def api_futures_trades():
    if not config.FUTURES_ENABLED:
        return jsonify([])
    cols = ["id", "timestamp", "pair", "side", "price", "amount",
            "value_eur", "fee", "mode", "pnl", "notes"]
    rows = db.get_trades(50, mode=f"futures_{config.FUTURES_MODE}")
    return jsonify([dict(zip(cols, row)) for row in rows])


@app.route("/api/futures/stats")
def api_futures_stats():
    if not config.FUTURES_ENABLED:
        return jsonify(None)
    return jsonify(db.get_trade_stats(f"futures_{config.FUTURES_MODE}"))


@app.route("/api/futures/status")
def api_futures_status():
    if not config.FUTURES_ENABLED:
        return jsonify({"enabled": False})
    from bot.futures_simulator import get_state as fget_state
    from bot.futures_engine import get_last_tick as f_last_tick, get_uptime as f_get_uptime, is_running as f_running, is_paused as f_paused
    from bot.futures_exchange import get_mark_price
    state = fget_state()
    positions_out = {}
    for sym, pos in state.positions.items():
        try:
            mark = get_mark_price(sym)
        except Exception:
            mark = pos.entry_price
        positions_out[sym] = {
            "side":              pos.side,
            "entry_price":       round(pos.entry_price, 4),
            "amount":            pos.amount,
            "margin":            round(pos.margin, 2),
            "leverage":          pos.leverage,
            "mark_price":        round(mark, 4),
            "unrealized_pnl":    round(pos.unrealized_pnl(mark), 4),
            "pnl_pct":           round(pos.pnl_pct(mark) * 100, 2),
            "liquidation_price": round(pos.liquidation_price(), 4),
            "take_profit_price": round(pos.take_profit_price, 4),
            "trailing_stop":     round(pos.trailing_stop_level(), 4),
            "dca_done":          pos.dca_done,
            "dca_drop":          config.FUTURES_DCA_DROP_PCT,
            "funding_paid":      round(pos.funding_paid, 4),
        }
    return jsonify({
        "enabled":              True,
        "running":              f_running(),
        "paused":               f_paused(),
        "mode":                 config.FUTURES_MODE,
        "stop_check_interval":  config.FUTURES_STOP_CHECK_INTERVAL,
        "leverage":      config.FUTURES_LEVERAGE,
        "last_tick":     f_last_tick(),
        "uptime":        f_get_uptime(),
        "balance":           round(state.balance, 2),
        "starting_balance":  db.get_starting_balance(f"futures_{config.FUTURES_MODE}"),
        "total_trades":      state.total_trades,
        "total_pnl":         round(state.total_pnl, 4),
        "total_fees":        round(state.total_fees, 4),
        "total_funding":     round(state.total_funding, 4),
        "positions":         positions_out,
    })


@app.route("/api/futures/control", methods=["POST"])
def api_futures_control():
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
    elif action == "manual_open" and symbol:
        size_pct = float(data.get("size_pct", config.FUTURES_POSITION_SIZE_PCT))
        futures_engine.manual_open(symbol, size_pct)
    elif action == "manual_close" and symbol:
        futures_engine.manual_close(symbol)
    return jsonify({"running": futures_engine.is_running(), "paused": futures_engine.is_paused()})


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
