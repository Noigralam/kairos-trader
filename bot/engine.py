import logging
import threading
import time
from enum import Enum
from . import config

log = logging.getLogger("cryptobot")
from .exchange import get_klines, get_price
from .strategy import compute_signal, Signal
from .simulator import open_position, close_position, get_state, check_stops
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
            notify(
                "[TICK] Prices — " + "  |  ".join(f"{p} €{v:,.2f}" for p, v in prices.items()),
                discord=False,
            )

            state = get_state()
            results = {pair: compute_signal(get_klines(pair)) for pair in config.TRADING_PAIRS}

            # Per-pair detail: file + web buffer only
            for pair, result in results.items():
                pos_flag = "IN" if pair in state.positions else "OUT"
                ema_cmp = "↑" if result.ema_fast > result.ema_slow else "↓"
                notify(
                    f"[TICK] {pair} ({pos_flag})  RSI={result.rsi:.1f}  "
                    f"EMA9={result.ema_fast:.2f} {ema_cmp} EMA21={result.ema_slow:.2f}  "
                    f"→ {result.signal.value}: {result.reason}",
                    discord=False,
                )

            # Compact Discord summary (one message per tick)
            price_header = "  ".join(f"{p} €{prices[p]:,.0f}" for p in config.TRADING_PAIRS)
            signal_lines = []
            for pair, result in results.items():
                pos_flag = "IN" if pair in state.positions else "—"
                signal_lines.append(
                    f"`{pair}` RSI={result.rsi:.1f} → **{result.signal.value}** — {result.reason}  `({pos_flag})`"
                )
            footer = f"PnL: €{state.total_pnl:+.2f}  |  Fees paid: €{state.total_fees:.4f}  |  Trades: {state.total_trades}"
            notify("**[TICK]** " + price_header + "\n" + "\n".join(signal_lines) + "\n" + footer)

            # Check stop-loss and take-profit on every tick
            check_stops(prices)

            # Execute signals — never sell below entry price
            for pair, result in results.items():
                has_position = pair in state.positions
                if result.signal == Signal.BUY and not has_position:
                    open_position(pair, prices[pair])
                elif result.signal == Signal.SELL and has_position:
                    if prices[pair] > state.positions[pair].entry_price:
                        close_position(pair, prices[pair], reason="signal")
                    else:
                        notify(f"[HOLD] {pair} SELL signal suppressed — price €{prices[pair]:,.2f} below entry €{state.positions[pair].entry_price:,.2f}", discord=False)

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
