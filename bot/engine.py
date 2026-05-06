import logging
import threading
import time
from enum import Enum
from . import config

log = logging.getLogger("cryptobot")
from .exchange import get_klines, get_price
from .strategy import compute_signal, Signal
from .simulator import open_position, close_position, check_stops, get_state
from .notifier import notify, bot_status_alert

INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


class BotStatus(Enum):
    RUNNING = "running"
    PAUSED  = "paused"
    STOPPED = "stopped"


_status = BotStatus.STOPPED
_thread: threading.Thread = None
_lock = threading.Lock()


def get_status() -> str:
    return _status.value


def _set_status(new_status: BotStatus):
    global _status
    with _lock:
        _status = new_status
    bot_status_alert(new_status.value)


def _loop():
    notify(f"Bot started | mode={config.MODE} | pairs={', '.join(config.TRADING_PAIRS)} | interval={config.INTERVAL}")
    sleep_sec = INTERVAL_SECONDS.get(config.INTERVAL, 3600)

    while _status != BotStatus.STOPPED:
        if _status == BotStatus.PAUSED:
            time.sleep(5)
            continue

        try:
            prices = {pair: get_price(pair) for pair in config.TRADING_PAIRS}
            check_stops(prices)

            state = get_state()
            for pair in config.TRADING_PAIRS:
                df = get_klines(pair)
                result = compute_signal(df)
                has_position = pair in state.positions

                if result.signal == Signal.BUY and not has_position:
                    open_position(pair, prices[pair])
                elif result.signal == Signal.SELL and has_position:
                    close_position(pair, prices[pair], reason="signal")

        except Exception as e:
            log.error(e, exc_info=True)
            notify(f"[ERROR] {e}", discord=False)

        time.sleep(sleep_sec)


def start():
    global _thread
    if _status == BotStatus.RUNNING:
        return
    _set_status(BotStatus.RUNNING)
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()


def pause():
    if _status == BotStatus.RUNNING:
        _set_status(BotStatus.PAUSED)


def resume():
    if _status == BotStatus.PAUSED:
        _set_status(BotStatus.RUNNING)


def stop():
    global _status
    with _lock:
        _status = BotStatus.STOPPED
    bot_status_alert("stopped")


def override_close(pair: str):
    """Manually close an open position regardless of current signal."""
    try:
        price = get_price(pair)
        close_position(pair, price, reason="manual_override")
        notify(f"[OVERRIDE] Closed {pair} @ €{price:.2f}")
    except Exception as e:
        notify(f"[OVERRIDE ERROR] {e}")
