import collections
import datetime
import io
import logging
import zoneinfo
import requests
from . import config

_TZ = zoneinfo.ZoneInfo("Europe/Helsinki")


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=_TZ)

log = logging.getLogger("cryptobot")

_log_buffer: collections.deque = collections.deque(maxlen=50)
_tick_message_ids: collections.deque = collections.deque(maxlen=5)


def _webhook_id_token():
    """Parse webhook ID and token from the webhook URL."""
    parts = config.DISCORD_WEBHOOK_URL.rstrip("/").split("/")
    return parts[-2], parts[-1]


def _delete_old_tick_messages():
    """Keep only the last 5 tick messages — delete anything beyond that."""
    if len(_tick_message_ids) < 5:
        return
    wid, token = _webhook_id_token()
    oldest_id = _tick_message_ids[0]
    try:
        requests.delete(
            f"https://discord.com/api/webhooks/{wid}/{token}/messages/{oldest_id}",
            timeout=5,
        )
    except Exception:
        pass


def _discord(message: str):
    if not config.DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(config.DISCORD_WEBHOOK_URL, json={"content": message}, timeout=5)
    except Exception:
        pass


def _discord_tick(message: str, chart_buf: io.BytesIO | None = None):
    """Send a tick message, track its ID, and delete old ones."""
    if not config.DISCORD_WEBHOOK_URL:
        return
    try:
        _delete_old_tick_messages()
        url = config.DISCORD_WEBHOOK_URL + "?wait=true"
        if chart_buf is not None:
            chart_buf.seek(0)
            resp = requests.post(
                url,
                data={"payload_json": __import__("json").dumps({"content": message})},
                files={"file": ("chart.png", chart_buf, "image/png")},
                timeout=10,
            )
        else:
            resp = requests.post(url, json={"content": message}, timeout=5)
        if resp.ok:
            _tick_message_ids.append(resp.json()["id"])
    except Exception:
        pass


def _terminal(message: str):
    log.info(message)
    _log_buffer.append({
        "time": _now().strftime("%Y-%m-%dT%H:%M:%S"),
        "msg": message,
    })


def get_recent_logs() -> list:
    return list(reversed(_log_buffer))


def notify(message: str, discord: bool = True):
    _terminal(message)
    if discord:
        _discord(message)


def notify_tick(message: str, chart_buf: io.BytesIO | None = None):
    """Send a tick summary to Discord with optional chart image, tracking message IDs."""
    _terminal(message)
    _discord_tick(message, chart_buf)


def trade_alert(side: str, pair: str, price: float, amount: float, value_eur: float,
                pnl: float = None, fee: float = None):
    tag = side
    msg = f"**[{tag}]** {pair} @ €{price:.2f} | {amount:.6f} units | €{value_eur:.2f}"
    if fee is not None:
        msg += f" | fee: €{fee:.3f}"
    if pnl is not None:
        msg += f" | PnL: €{pnl:+.2f}"
    notify(msg)


def trailing_stop_alert(pair: str, price: float, pnl: float):
    notify(f"**[TRAILING STOP]** {pair} exited @ €{price:.2f} | PnL: €{pnl:+.2f}")


def bot_status_alert(status: str):
    notify(f"**[BOT]** Status → {status.upper()}")


def build_chart(pairs: list, prices: dict, results: dict) -> io.BytesIO | None:
    """Generate a small price+EMA200 chart for all pairs and return as PNG buffer."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from .candles import get_df
        import pandas as pd

        n = len(pairs)
        fig, axes = plt.subplots(n, 1, figsize=(8, 3 * n), squeeze=False)
        fig.patch.set_facecolor("#0d1117")

        for ax, pair in zip(axes[:, 0], pairs):
            ax.set_facecolor("#161b22")
            for spine in ax.spines.values():
                spine.set_edgecolor("#30363d")
            ax.tick_params(colors="#8b949e", labelsize=8)

            df = get_df(pair, config.INTERVAL, limit=210 + 96)
            if df.empty:
                continue
            close = df["close"]
            ema = close.ewm(span=200, adjust=False).mean()
            times = df["open_time"].dt.to_pydatetime()

            ax.plot(times, close.values, color="#58a6ff", linewidth=1, label="Price")
            ax.plot(times, ema.values, color="#d29922", linewidth=1,
                    linestyle="--", label="EMA200")

            result = results.get(pair)
            if result:
                gap = prices[pair] - result.ema_trend
                trend = f"{'above' if gap >= 0 else 'below'} EMA200 by €{abs(gap):,.2f}"
                ax.set_title(
                    f"{pair}  €{prices[pair]:,.2f}  |  RSI {result.rsi:.1f}  |  {trend}",
                    color="#e6edf3", fontsize=9, pad=6,
                )

            ax.legend(fontsize=7, facecolor="#161b22", labelcolor="#8b949e",
                      edgecolor="#30363d", loc="upper left")
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
            fig.autofmt_xdate(rotation=30, ha="right")
            ax.yaxis.set_tick_params(labelcolor="#8b949e")

        fig.tight_layout(pad=1.5)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf
    except Exception as e:
        log.warning(f"Chart generation failed: {e}")
        return None
