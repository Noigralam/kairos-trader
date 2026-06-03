import logging
import threading
import time
import datetime
import zoneinfo

from . import config
from .futures_exchange import get_klines, get_mark_price, get_funding_rate, get_next_funding_time
from .strategy import compute_signal, Signal
from .futures_simulator import (
    init as sim_init, get_state, open_long, dca_long, close_long,
    check_stops, apply_funding,
)
from .notifier import notify

log = logging.getLogger("cryptobot")

INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

_FUTURES_INTERVAL = "15m"

_running  = False
_thread: threading.Thread = None
_lock     = threading.Lock()
_last_tick: str = None

# unix ms of next funding settlement per symbol (fetched on startup)
_next_funding: dict[str, int] = {}


def get_last_tick() -> str:
    return _last_tick


def _seconds_until_next_candle(sleep_sec: int) -> int:
    now = datetime.datetime.now()
    elapsed = (now.minute * 60 + now.second) % sleep_sec
    return sleep_sec - elapsed + 15


def _maybe_apply_funding(symbol: str, price: float) -> None:
    """Apply funding if we just passed a settlement boundary."""
    global _next_funding
    now_ms = int(time.time() * 1000)
    if _next_funding.get(symbol, 0) > now_ms:
        return
    try:
        rate = get_funding_rate(symbol)
        if rate != 0.0:
            apply_funding(symbol, rate)
        _next_funding[symbol] = get_next_funding_time(symbol)
    except Exception as e:
        log.warning(f"[FUTURES FUNDING] {symbol} fetch failed: {e}")


def _loop():
    global _running, _last_tick

    sim_init()
    notify(
        f"[FUTURES] Engine started — mode={config.FUTURES_MODE}  "
        f"pairs={', '.join(config.FUTURES_TRADING_PAIRS)}  "
        f"leverage={config.FUTURES_LEVERAGE}x  interval={_FUTURES_INTERVAL}"
    )

    sleep_sec = INTERVAL_SECONDS.get(_FUTURES_INTERVAL, 900)

    # Pre-fill next funding times
    for sym in config.FUTURES_TRADING_PAIRS:
        try:
            _next_funding[sym] = get_next_funding_time(sym)
        except Exception:
            _next_funding[sym] = 0

    # Align to next candle boundary
    wait = _seconds_until_next_candle(sleep_sec)
    log.info(f"[FUTURES] Waiting {wait}s to align to next {_FUTURES_INTERVAL} candle boundary")
    time.sleep(wait)

    while _running:
        try:
            _last_tick = datetime.datetime.now(
                tz=zoneinfo.ZoneInfo("Europe/Helsinki")
            ).strftime("%H:%M")

            # Fetch mark prices and candles for all pairs
            prices = {}
            dfs    = {}
            for sym in config.FUTURES_TRADING_PAIRS:
                try:
                    prices[sym] = get_mark_price(sym)
                    dfs[sym]    = get_klines(sym, interval=_FUTURES_INTERVAL, limit=250)
                except Exception as e:
                    log.warning(f"[FUTURES TICK] {sym} data fetch failed: {e}")

            if not prices:
                time.sleep(_seconds_until_next_candle(sleep_sec))
                continue

            notify(
                "[FUTURES TICK] " + "  |  ".join(
                    f"{s} ${v:,.4f}" for s, v in prices.items()
                ),
                discord=False,
            )

            # Compute signals
            results = {}
            for sym, df in dfs.items():
                results[sym] = compute_signal(
                    df,
                    rsi_period=config.futures_rsi_period_for(sym),
                    rsi_oversold=config.futures_rsi_oversold_for(sym),
                    rsi_overbought=config.futures_rsi_overbought_for(sym),
                    ema_gap=config.futures_ema_gap_for(sym),
                )
                state = get_state()
                pos_flag = "IN" if sym in state.positions else "OUT"
                notify(
                    f"[FUTURES TICK] {sym} ({pos_flag})  RSI={results[sym].rsi:.1f}  "
                    f"→ {results[sym].signal.value}: {results[sym].reason}",
                    discord=False,
                )

            # Funding check (before stop checks so cost is accounted)
            for sym in list(get_state().positions):
                if sym in prices:
                    _maybe_apply_funding(sym, prices[sym])

            # Stop / take-profit / trailing-stop / liquidation checks
            check_stops(prices)

            # Execute entry / DCA signals
            state = get_state()
            for sym, result in results.items():
                if sym not in prices:
                    continue
                price       = prices[sym]
                has_pos     = sym in state.positions

                if result.signal == Signal.BUY and not has_pos:
                    open_long(sym, price)

                elif has_pos:
                    pos  = state.positions[sym]
                    drop = (pos.entry_price - price) / pos.entry_price
                    if not pos.dca_done and drop >= config.FUTURES_DCA_DROP_PCT:
                        dca_long(sym, price)

                # No explicit SELL signal path — exits are handled by check_stops
                # (take-profit and trailing stop). A SELL from the RSI strategy on
                # futures means overbought, but we let the trailing stop lock in
                # profit rather than exiting at the first overbought RSI tick.

        except Exception as e:
            log.error(f"[FUTURES LOOP] {e}", exc_info=True)
            notify(f"[FUTURES ERROR] {e}", discord=False)

        time.sleep(_seconds_until_next_candle(sleep_sec))


def start():
    global _running, _thread
    with _lock:
        if _running:
            return
        _running = True
    _thread = threading.Thread(target=_loop, daemon=True, name="futures-engine")
    _thread.start()


def stop():
    global _running
    with _lock:
        _running = False
    notify("[FUTURES] Engine stopped.", discord=False)


def is_running() -> bool:
    return _running


def manual_open(symbol: str, size_pct: float = None):
    """Manually open a long on a futures pair."""
    try:
        price = get_mark_price(symbol)
        open_long(symbol, price, size_pct=size_pct)
        notify(f"[FUTURES MANUAL BUY] {symbol} @ ${price:,.4f}")
    except Exception as e:
        notify(f"[FUTURES MANUAL BUY ERROR] {e}")


def manual_close(symbol: str):
    """Manually close an open futures long."""
    try:
        price = get_mark_price(symbol)
        close_long(symbol, price, reason="manual_override")
        notify(f"[FUTURES MANUAL CLOSE] {symbol} @ ${price:,.4f}")
    except Exception as e:
        notify(f"[FUTURES MANUAL CLOSE ERROR] {e}")
