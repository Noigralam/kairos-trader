import logging
import os
import threading
import time
from enum import Enum
from . import config

log = logging.getLogger("cryptobot")
from .spot_exchange import get_klines, get_price
from .strategy import compute_signal, Signal
from .candles import initial_sync, sync as sync_candles, get_df
from .spot_simulator import open_position, close_position, dca_position, get_state, check_stops, manual_add
from .notifier import notify, notify_tick, bot_status_alert, build_chart, extreme_alert, daily_summary
from . import db as _db

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
_stop_thread: threading.Thread = None
_lock = threading.Lock()
_last_tick: str = None
_prev_tick: dict = {}          # pair -> {price, rsi, ema200, above_ema}
_last_summary_date = None      # date of last daily summary
_start_time: float = None      # epoch time when bot last started


def _arrow(cur: float, prev: float | None) -> str:
    if prev is None or cur == prev:
        return ""
    return " ▲" if cur > prev else " ▼"


def get_status() -> str:
    return _status.value


def get_last_tick() -> str:
    return _last_tick


def _set_status(new_status: BotStatus):
    global _status
    with _lock:
        _status = new_status
    bot_status_alert(new_status.value)


def _seconds_until_next_candle(sleep_sec: int) -> int:
    import datetime
    now = datetime.datetime.now()
    elapsed = (now.minute * 60 + now.second) % sleep_sec
    # +15s grace period: Binance takes a few seconds to close and publish a candle.
    # Without it we'd fetch the previous (already-stored) candle and skip the new one.
    return sleep_sec - elapsed + 15


def _loop():
    notify(f"Bot started | mode={config.SPOT_MODE} | pairs={', '.join(config.SPOT_TRADING_PAIRS)} | interval={config.SPOT_INTERVAL}")
    sleep_sec = INTERVAL_SECONDS.get(config.SPOT_INTERVAL, 3600)

    for pair in config.SPOT_TRADING_PAIRS:
        initial_sync(pair, config.SPOT_INTERVAL)

    # align to the next candle boundary before first tick
    wait = _seconds_until_next_candle(sleep_sec)
    log.info(f"Waiting {wait}s to align to next {config.SPOT_INTERVAL} candle boundary")
    time.sleep(wait)

    while _status != BotStatus.STOPPED:
        if _status == BotStatus.PAUSED:
            time.sleep(5)
            continue

        try:
            import datetime, zoneinfo
            global _last_tick
            _last_tick = datetime.datetime.now(tz=zoneinfo.ZoneInfo("Europe/Helsinki")).strftime("%H:%M")
            for pair in config.SPOT_TRADING_PAIRS:
                sync_candles(pair, config.SPOT_INTERVAL)
                if config.SPOT_DAILY_EMA_FILTER:
                    sync_candles(pair, "1d")

            prices = {pair: get_price(pair) for pair in config.SPOT_TRADING_PAIRS}
            notify(
                "[TICK] Prices — " + "  |  ".join(f"{p} €{v:,.2f}" for p, v in prices.items()),
                discord=False,
            )

            def _daily_ema(pair: str) -> float | None:
                if not config.SPOT_DAILY_EMA_FILTER:
                    return None
                df1d = get_df(pair, "1d", limit=210)
                if len(df1d) < 201:
                    return None
                return float(df1d["close"].ewm(span=200, adjust=False).mean().iloc[-1])

            state = get_state()
            results = {pair: compute_signal(get_df(pair, config.SPOT_INTERVAL),
                                            rsi_period=config.rsi_period_for(pair),
                                            rsi_oversold=config.rsi_oversold_for(pair),
                                            rsi_overbought=config.rsi_overbought_for(pair),
                                            ema_gap=config.ema_gap_for(pair),
                                            daily_ema=_daily_ema(pair))
                       for pair in config.SPOT_TRADING_PAIRS}

            # Per-pair detail: log + web buffer only
            for pair, result in results.items():
                pos_flag   = "IN" if pair in state.positions else "OUT"
                trend_flag = "↑" if prices[pair] > result.ema_trend else "↓"
                notify(
                    f"[TICK] {pair} ({pos_flag})  RSI={result.rsi:.1f}  "
                    f"EMA200={result.ema_trend:.2f} {trend_flag}  "
                    f"→ {result.signal.value}: {result.reason}",
                    discord=False,
                )

            # Extreme condition alerts (not deleted from channel)
            for pair, result in results.items():
                price    = prices[pair]
                prev     = _prev_tick.get(pair, {})
                above    = price > result.ema_trend
                was_above = prev.get("above_ema")

                if result.rsi < 20:
                    extreme_alert(pair, f"RSI extremely oversold at {result.rsi:.1f}", result.rsi, price, result.ema_trend)
                elif result.rsi > 85:
                    extreme_alert(pair, f"RSI extremely overbought at {result.rsi:.1f}", result.rsi, price, result.ema_trend)

                if was_above is not None:
                    if not was_above and above:
                        extreme_alert(pair, "Price crossed ABOVE EMA200 — bullish trend change", result.rsi, price, result.ema_trend)
                    elif was_above and not above:
                        extreme_alert(pair, "Price crossed BELOW EMA200 — bearish trend change", result.rsi, price, result.ema_trend)

                _prev_tick[pair] = {"price": price, "rsi": result.rsi, "ema200": result.ema_trend, "above_ema": above}

            # Daily summary at first tick of a new day
            import datetime as _dt
            global _last_summary_date
            today = _dt.date.today()
            if _last_summary_date is not None and today != _last_summary_date:
                daily_summary(state, config.SPOT_TRADING_PAIRS, prices, results)
            _last_summary_date = today

            # Discord tick embed
            chart_buf = build_chart(config.SPOT_TRADING_PAIRS, prices, results)
            notify_tick(config.SPOT_TRADING_PAIRS, prices, results, state, chart_buf)

            # Check stop-loss and take-profit on every tick
            check_stops(prices)

            # Execute signals — never sell below entry price
            for pair, result in results.items():
                has_position = pair in state.positions
                if result.signal == Signal.BUY and not has_position:
                    open_position(pair, prices[pair])
                elif has_position:
                    pos = state.positions[pair]
                    drop = (pos.entry_price - prices[pair]) / pos.entry_price
                    next_drop = config.SPOT_DCA_DROP_PCT + pos.dca_count * config.SPOT_DCA_STEP_PCT
                    if pos.dca_count < config.SPOT_DCA_MAX and drop >= next_drop:
                        prev_count = pos.dca_count
                        dca_position(pair, prices[pair])
                        if pos.dca_count > prev_count:
                            notify(f"[DCA {pos.dca_count}/{config.SPOT_DCA_MAX}] {pair} down {drop*100:.1f}% from entry, RSI={result.rsi:.1f} — averaged down", discord=False)
                if result.signal == Signal.SELL and has_position:
                    pos = state.positions[pair]
                    min_exit_pct = config.min_exit_for(pair)
                    min_exit = pos.entry_price * (1 + min_exit_pct)
                    if prices[pair] >= min_exit:
                        close_position(pair, prices[pair], reason="signal")
                    else:
                        notify(f"[HOLD] {pair} SELL signal suppressed — price €{prices[pair]:,.2f} below min exit €{min_exit:,.2f} (entry €{pos.entry_price:,.2f} +{min_exit_pct*100:.1f}%)", discord=False)

            # snapshot portfolio value (cash + open position mark-to-market)
            snap_state = get_state()
            open_val   = sum(prices[p] * pos.amount for p, pos in snap_state.positions.items() if p in prices)
            _db.log_balance(round(snap_state.balance + open_val, 2), config.SPOT_MODE)

        except Exception as e:
            log.error(e, exc_info=True)
            notify(f"[ERROR] {e}", discord=False)

        time.sleep(_seconds_until_next_candle(sleep_sec))


def _stop_loop():
    """Fast loop: fetch current prices and run stop checks every SPOT_STOP_CHECK_INTERVAL seconds.
    Runs between candles so trailing stops and take-profits fire without waiting for the next tick."""
    log.info(f"[SPOT] Stop-check loop started ({config.SPOT_STOP_CHECK_INTERVAL}s interval)")
    while _status != BotStatus.STOPPED:
        time.sleep(config.SPOT_STOP_CHECK_INTERVAL)
        if _status != BotStatus.RUNNING:
            continue
        try:
            state = get_state()
            if not state.positions:
                continue
            prices = {}
            for pair in list(state.positions):
                try:
                    prices[pair] = get_price(pair)
                except Exception as e:
                    log.warning(f"[SPOT STOP-CHECK] {pair} price fetch failed: {e}")
            if prices:
                check_stops(prices)
        except Exception as e:
            log.error(f"[SPOT STOP-CHECK] {e}", exc_info=True)


_LIVE_SINCE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "spot_live_since.txt")


def _ensure_live_since():
    if config.SPOT_MODE == "live" and not os.path.exists(_LIVE_SINCE_PATH):
        import datetime, zoneinfo
        ts = datetime.datetime.now(tz=zoneinfo.ZoneInfo("Europe/Helsinki")).isoformat()
        with open(_LIVE_SINCE_PATH, "w") as f:
            f.write(ts)


def get_live_since() -> str | None:
    try:
        with open(_LIVE_SINCE_PATH) as f:
            return f.read().strip()
    except Exception:
        return None


def get_uptime() -> int | None:
    if _start_time is None:
        return None
    return int(time.time() - _start_time)


def start():
    global _thread, _stop_thread, _start_time
    if _status == BotStatus.RUNNING:
        return
    _ensure_live_since()
    _start_time = time.time()
    _set_status(BotStatus.RUNNING)
    _thread      = threading.Thread(target=_loop,       daemon=True, name="spot-engine")
    _stop_thread = threading.Thread(target=_stop_loop,  daemon=True, name="spot-stop-check")
    _thread.start()
    _stop_thread.start()


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


def manual_buy(pair: str, size_pct: float):
    """Manually buy — opens or adds to an existing position."""
    try:
        price = get_price(pair)
        manual_add(pair, price, size_pct)
        notify(f"[MANUAL BUY] {pair} @ €{price:.2f} ({size_pct*100:.0f}% of balance)")
    except Exception as e:
        notify(f"[MANUAL BUY ERROR] {e}")
