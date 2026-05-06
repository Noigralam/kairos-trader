import logging
import requests
from . import config

log = logging.getLogger("cryptobot")


def _discord(message: str):
    if not config.DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(config.DISCORD_WEBHOOK_URL, json={"content": message}, timeout=5)
    except Exception:
        pass


def _terminal(message: str):
    log.info(message)


def notify(message: str, discord: bool = True):
    _terminal(message)
    if discord:
        _discord(message)


def trade_alert(side: str, pair: str, price: float, amount: float, value_eur: float, pnl: float = None):
    tag = "BUY" if side == "BUY" else "SELL"
    msg = f"**[{tag}]** {pair} @ €{price:.2f} | {amount:.6f} units | €{value_eur:.2f}"
    if pnl is not None:
        msg += f" | PnL: €{pnl:+.2f}"
    notify(msg)


def stop_loss_alert(pair: str, price: float, loss_eur: float):
    notify(f"**[STOP-LOSS]** {pair} hit @ €{price:.2f} | Loss: €{loss_eur:.2f}")


def bot_status_alert(status: str):
    notify(f"**[BOT]** Status → {status.upper()}")
