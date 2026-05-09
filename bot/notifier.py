import collections
import datetime
import logging
import requests
from . import config

log = logging.getLogger("cryptobot")

_log_buffer: collections.deque = collections.deque(maxlen=50)


def _discord(message: str):
    if not config.DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(config.DISCORD_WEBHOOK_URL, json={"content": message}, timeout=5)
    except Exception:
        pass


def _terminal(message: str):
    log.info(message)
    _log_buffer.append({
        "time": datetime.datetime.utcnow().strftime("%H:%M:%S"),
        "msg": message,
    })


def get_recent_logs() -> list:
    return list(reversed(_log_buffer))


def notify(message: str, discord: bool = True):
    _terminal(message)
    if discord:
        _discord(message)


def trade_alert(side: str, pair: str, price: float, amount: float, value_eur: float,
                pnl: float = None, fee: float = None):
    tag = "BUY" if side == "BUY" else "SELL"
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
