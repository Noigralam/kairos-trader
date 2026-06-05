import collections
import datetime
import io
import json
import logging
import os
import time
import zoneinfo
import requests
from . import config

_TZ  = zoneinfo.ZoneInfo("Europe/Helsinki")
log  = logging.getLogger("cryptobot")
_log_buffer: collections.deque = collections.deque(maxlen=50)

# ── persistent tick message IDs ────────────────────────────────────────────────
_IDS_PATH      = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "discord_ids.json")
_MAX_TICK_MSGS = 5


def _load_ids() -> list:
    try:
        with open(_IDS_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save_ids(ids: list):
    try:
        os.makedirs(os.path.dirname(_IDS_PATH), exist_ok=True)
        with open(_IDS_PATH, "w") as f:
            json.dump(ids, f)
    except Exception:
        pass


_tick_ids: list = _load_ids()


def _webhook_id_token():
    parts = config.DISCORD_WEBHOOK_URL.rstrip("/").split("/")
    return parts[-2], parts[-1]


def _delete_message(msg_id: str):
    if not config.DISCORD_WEBHOOK_URL:
        return
    wid, token = _webhook_id_token()
    try:
        requests.delete(
            f"https://discord.com/api/webhooks/{wid}/{token}/messages/{msg_id}",
            timeout=5,
        )
    except Exception:
        pass


def _prune_tick_messages():
    """Delete oldest tick messages until only _MAX_TICK_MSGS-1 remain (making room for the next one)."""
    global _tick_ids
    while len(_tick_ids) >= _MAX_TICK_MSGS:
        _delete_message(_tick_ids.pop(0))
    _save_ids(_tick_ids)


# ── Discord send helpers ───────────────────────────────────────────────────────

def _send(payload: dict, files=None) -> str | None:
    if not config.DISCORD_WEBHOOK_URL:
        return None
    try:
        url = config.DISCORD_WEBHOOK_URL + "?wait=true"
        if files:
            resp = requests.post(
                url,
                data={"payload_json": json.dumps(payload)},
                files=files,
                timeout=10,
            )
        else:
            resp = requests.post(url, json=payload, timeout=5)
        if resp.ok:
            return resp.json().get("id")
    except Exception:
        pass
    return None


def _send_tick(payload: dict, chart_buf: io.BytesIO | None = None):
    """Send tick message, track ID, prune old ones."""
    _prune_tick_messages()
    files = None
    if chart_buf is not None:
        chart_buf.seek(0)
        files = {"file": ("chart.png", chart_buf, "image/png")}
        if payload.get("embeds"):
            payload["embeds"][-1]["image"] = {"url": "attachment://chart.png"}
    msg_id = _send(payload, files)
    if msg_id:
        _tick_ids.append(msg_id)
        _save_ids(_tick_ids)


# ── Fear & Greed ───────────────────────────────────────────────────────────────

_fng_cache: dict = {"value": None, "label": None, "fetched": 0}


def get_fng() -> tuple:
    if time.time() - _fng_cache["fetched"] > 3600:
        try:
            import urllib.request
            with urllib.request.urlopen("https://api.alternative.me/fng/", timeout=5) as r:
                d = json.loads(r.read())["data"][0]
            _fng_cache.update({
                "value":   int(d["value"]),
                "label":   d["value_classification"],
                "fetched": time.time(),
            })
        except Exception:
            pass
    return _fng_cache["value"], _fng_cache["label"]


def _utcnow_iso() -> str:
    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat()


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=_TZ)


# ── internal log buffer ────────────────────────────────────────────────────────

def _terminal(message: str):
    log.info(message)
    _log_buffer.append({"time": _now().strftime("%Y-%m-%dT%H:%M:%S"), "msg": message})


def get_recent_logs() -> list:
    return list(reversed(_log_buffer))


# ── public notify functions ────────────────────────────────────────────────────

def notify(message: str, discord: bool = True):
    _terminal(message)
    if discord:
        _send({"content": message})


def notify_tick(pairs: list, prices: dict, results: dict, state, chart_buf=None):
    """Build and send a Discord embed tick summary."""
    from .strategy import Signal

    tick_time = _now().strftime("%H:%M")
    fng_val, fng_lbl = get_fng()

    # embed color: green=buying, red=selling, blue=hold
    signals = [results[p].signal for p in pairs if p in results]
    if any(s == Signal.BUY for s in signals):
        color = 0x3fb950
    elif any(s == Signal.SELL for s in signals):
        color = 0xf85149
    else:
        color = 0x58a6ff

    fields = []
    log_parts = []

    for pair in pairs:
        result = results.get(pair)
        if not result:
            continue
        price  = prices.get(pair, 0)
        pos    = state.positions.get(pair)
        gap    = price - result.ema_trend
        above  = gap >= 0
        ob     = config.rsi_overbought_for(pair)

        if result.rsi < config.SPOT_RSI_OVERSOLD:
            rsi_zone = "🟢 oversold"
        elif result.rsi > ob:
            rsi_zone = "🔴 overbought"
        else:
            rsi_zone = "neutral"

        trend = f"{'↑' if above else '↓'} {'above' if above else 'below'} EMA200 €{abs(gap):,.2f}"

        if result.signal == Signal.BUY and not pos:
            action = "**🟢 BUYING**"
        elif result.signal == Signal.SELL and pos:
            action = "**🔴 SELLING**"
        elif not above:
            action = "⏸ HOLD — downtrend"
        elif result.rsi >= config.SPOT_RSI_OVERSOLD:
            action = "⏸ HOLD — wait RSI"
        else:
            action = "⏸ HOLD"

        val = f"€{price:,.2f}\nRSI({config.rsi_period_for(pair)}): {result.rsi:.1f} {rsi_zone}\n{trend}\n{action}"
        if pos:
            pct = (price - pos.entry_price) / pos.entry_price * 100
            val += f"\nEntry €{pos.entry_price:,.2f} ({pct:+.1f}%)"

        fields.append({"name": pair, "value": val, "inline": True})
        log_parts.append(f"{pair} RSI={result.rsi:.1f} → {result.signal.value}")

    footer_parts = [
        f"Balance: €{state.balance:.2f}",
        f"PnL: €{state.total_pnl:+.2f}",
        f"Trades: {state.total_trades}",
        f"Fees: €{state.total_fees:.4f}",
    ]
    if fng_val is not None:
        footer_parts.append(f"F&G: {fng_val} — {fng_lbl}")

    embed = {
        "title":     f"📊 TICK — {tick_time}",
        "color":     color,
        "fields":    fields,
        "footer":    {"text": " | ".join(footer_parts)},
        "timestamp": _utcnow_iso(),
    }

    _terminal(f"[TICK] {tick_time} — " + "  |  ".join(log_parts))
    _send_tick({"embeds": [embed]}, chart_buf)


def extreme_alert(pair: str, condition: str, rsi: float, price: float, ema: float):
    """Extreme condition alert — never deleted from channel."""
    if "oversold" in condition.lower():
        color, emoji = 0x3fb950, "🚨🟢"
    elif "overbought" in condition.lower():
        color, emoji = 0xf85149, "🚨🔴"
    elif "above" in condition.lower():
        color, emoji = 0x3fb950, "📈"
    else:
        color, emoji = 0xf85149, "📉"

    content = "@everyone" if config.SPOT_MODE == "live" else ""
    embed = {
        "title":       f"{emoji} EXTREME — {pair}",
        "description": condition,
        "color":       color,
        "fields": [
            {"name": "Price",  "value": f"€{price:,.2f}", "inline": True},
            {"name": "RSI",    "value": f"{rsi:.1f}",     "inline": True},
            {"name": "EMA200", "value": f"€{ema:,.2f}",   "inline": True},
        ],
        "timestamp": _utcnow_iso(),
    }
    _send({"content": content, "embeds": [embed]})
    _terminal(f"[EXTREME] {pair} — {condition}")


def daily_summary(state, pairs: list, prices: dict, results: dict):
    """Daily performance summary embed."""
    from . import db as _db
    stats    = _db.get_trade_stats(config.SPOT_MODE)
    fng_val, fng_lbl = get_fng()
    open_val = sum(prices.get(p, 0) * pos.amount for p, pos in state.positions.items())
    total    = state.balance + open_val

    fields = [
        {"name": "Cash balance",   "value": f"€{state.balance:,.2f}", "inline": True},
        {"name": "Open positions", "value": f"€{open_val:,.2f}",      "inline": True},
        {"name": "Total value",    "value": f"€{total:,.2f}",         "inline": True},
        {"name": "Realized PnL",   "value": f"€{state.total_pnl:+.2f}", "inline": True},
        {"name": "Trades",         "value": str(state.total_trades),  "inline": True},
        {"name": "Fees",           "value": f"€{state.total_fees:.4f}", "inline": True},
    ]
    if stats:
        fields += [
            {"name": "Win rate",     "value": f"{stats['win_rate']}%  ({stats['wins']}W / {stats['losses']}L)", "inline": True},
            {"name": "Avg PnL",      "value": f"€{stats['avg_pnl']:+.2f}", "inline": True},
            {"name": "Best / Worst", "value": f"€{stats['best']:+.2f} / €{stats['worst']:+.2f}", "inline": True},
        ]
    if fng_val is not None:
        fields.append({"name": "Fear & Greed", "value": f"{fng_val} — {fng_lbl}", "inline": True})

    embed = {
        "title":     f"📅 Daily Summary — {_now().strftime('%d.%m.%Y')}",
        "color":     0x3fb950 if state.total_pnl >= 0 else 0xf85149,
        "fields":    fields,
        "timestamp": _utcnow_iso(),
    }
    _send({"embeds": [embed]})
    _terminal("[DAILY SUMMARY] sent to Discord")


def trade_alert(side: str, pair: str, price: float, amount: float, value_eur: float,
                pnl: float = None, fee: float = None):
    color = 0x3fb950 if side == "BUY" else 0xf85149
    emoji = "🟢" if side == "BUY" else "🔴"
    fields = [
        {"name": "Price",  "value": f"€{price:,.2f}",   "inline": True},
        {"name": "Amount", "value": f"{amount:.6f}",     "inline": True},
        {"name": "Value",  "value": f"€{value_eur:,.2f}", "inline": True},
    ]
    if fee  is not None:
        fields.append({"name": "Fee", "value": f"€{fee:.4f}", "inline": True})
    if pnl  is not None:
        fields.append({"name": "PnL", "value": f"€{pnl:+.2f}", "inline": True})

    content = "@everyone" if config.SPOT_MODE == "live" else ""
    embed = {
        "title":     f"{emoji} {side} — {pair}",
        "color":     color,
        "fields":    fields,
        "timestamp": _utcnow_iso(),
    }
    _send({"content": content, "embeds": [embed]})
    _terminal(f"[{side}] {pair} @ €{price:.2f} | {amount:.6f} | €{value_eur:.2f}"
              + (f" | PnL: €{pnl:+.2f}" if pnl is not None else ""))


def trailing_stop_alert(pair: str, price: float, pnl: float):
    content = "@everyone" if config.SPOT_MODE == "live" else ""
    embed = {
        "title":     f"🛑 TRAILING STOP — {pair}",
        "color":     0x3fb950 if pnl >= 0 else 0xf85149,
        "fields": [
            {"name": "Exit price", "value": f"€{price:,.2f}", "inline": True},
            {"name": "PnL",        "value": f"€{pnl:+.2f}",  "inline": True},
        ],
        "timestamp": _utcnow_iso(),
    }
    _send({"content": content, "embeds": [embed]})
    _terminal(f"[TRAILING STOP] {pair} @ €{price:.2f} | PnL: €{pnl:+.2f}")


def bot_status_alert(status: str):
    emoji = {"running": "▶️", "paused": "⏸", "stopped": "⏹"}.get(status, "ℹ️")
    _send({"content": f"{emoji} **Bot** → {status.upper()}"})
    _terminal(f"[BOT] {status.upper()}")


def build_chart(pairs: list, prices: dict, results: dict) -> io.BytesIO | None:
    """Generate a price+EMA200 chart for all pairs and return as PNG buffer."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from .spot_candles import get_df

        n = len(pairs)
        fig, axes = plt.subplots(n, 1, figsize=(8, 3 * n), squeeze=False)
        fig.patch.set_facecolor("#0d1117")

        for ax, pair in zip(axes[:, 0], pairs):
            ax.set_facecolor("#161b22")
            for spine in ax.spines.values():
                spine.set_edgecolor("#30363d")
            ax.tick_params(colors="#8b949e", labelsize=8)

            df = get_df(pair, config.SPOT_INTERVAL, limit=210 + 96)
            if df.empty:
                continue
            close  = df["close"]
            ema    = close.ewm(span=200, adjust=False).mean()
            times  = df["open_time"].dt.to_pydatetime()

            ax.plot(times, close.values, color="#58a6ff", linewidth=1, label="Price")
            ax.plot(times, ema.values,   color="#d29922", linewidth=1,
                    linestyle="--", label="EMA200")

            result = results.get(pair)
            if result:
                gap   = prices[pair] - result.ema_trend
                trend = f"{'above' if gap >= 0 else 'below'} EMA200 by €{abs(gap):,.2f}"
                ax.set_title(
                    f"{pair}  €{prices[pair]:,.2f}  |  RSI {result.rsi:.1f}  |  {trend}",
                    color="#e6edf3", fontsize=9, pad=6,
                )

            ax.legend(fontsize=7, facecolor="#161b22", labelcolor="#8b949e",
                      edgecolor="#30363d", loc="upper left")
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
            fig.autofmt_xdate(rotation=30, ha="right")

        fig.tight_layout(pad=1.5)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf
    except Exception as e:
        log.warning(f"Chart generation failed: {e}")
        return None
