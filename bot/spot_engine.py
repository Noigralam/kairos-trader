import datetime
import json
import logging
import os
import threading
import time
import zoneinfo
from enum import Enum
from . import config

log = logging.getLogger("cryptobot")
from .spot_exchange import get_price
from .strategy import compute_signal, Signal
from .candles import initial_sync, sync as sync_candles, get_df
from .spot_simulator import open_position, close_position, partial_close_position, dca_position, get_state, check_stops, manual_add, init_shadows, get_shadows
from .notifier import notify, notify_tick, bot_status_alert, build_chart, daily_summary
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
_last_tick_time: float = None  # epoch of last successful tick
_last_summary_date = None      # date of last daily summary
_start_time: float = None      # epoch time when bot last started
_missed_tick_alerted: bool = False  # avoid spamming missed-tick alerts
_api_error: str | None = None  # set when Binance API is unreachable, cleared on success


def _arrow(cur: float, prev: float | None) -> str:
    if prev is None or cur == prev:
        return ""
    return " ▲" if cur > prev else " ▼"


def get_status() -> str:
    return _status.value


def get_last_tick() -> str:
    return _last_tick


def get_api_error() -> str | None:
    return _api_error


def _set_status(new_status: BotStatus):
    global _status
    with _lock:
        _status = new_status
    bot_status_alert(new_status.value)


def _seconds_until_next_candle(sleep_sec: int) -> int:
    now = datetime.datetime.now()
    elapsed = (now.minute * 60 + now.second) % sleep_sec
    # +15s grace period: Binance takes a few seconds to close and publish a candle.
    # Without it we'd fetch the previous (already-stored) candle and skip the new one.
    return sleep_sec - elapsed + 15


def _daily_ema(pair: str) -> float | None:
    if not config.SPOT_DAILY_EMA_FILTER:
        return None
    df1d = get_df(pair, "1d", limit=210)
    if len(df1d) < 201:
        return None
    return float(df1d["close"].ewm(span=200, adjust=False).mean().iloc[-1])


def _loop():
    notify(f"Bot started | mode={config.SPOT_MODE} | pairs={', '.join(config.SPOT_TRADING_PAIRS)} | interval={config.SPOT_INTERVAL}")
    sleep_sec = INTERVAL_SECONDS.get(config.SPOT_INTERVAL, 3600)

    all_pairs = list(config.SPOT_TRADING_PAIRS)
    for s in get_shadows():
        if s.pairs:
            for p in s.pairs:
                if p not in all_pairs:
                    all_pairs.append(p)

    # collect all (pair, interval) combos needed by shadows with non-default intervals
    shadow_intervals: list[tuple[str, str]] = []
    for s in get_shadows():
        if s.interval != config.SPOT_INTERVAL:
            for p in (s.pairs or config.SPOT_TRADING_PAIRS):
                if (p, s.interval) not in shadow_intervals:
                    shadow_intervals.append((p, s.interval))

    for pair in all_pairs:
        initial_sync(pair, config.SPOT_INTERVAL)
    for pair, interval in shadow_intervals:
        initial_sync(pair, interval)

    # align to the next candle boundary before first tick
    wait = _seconds_until_next_candle(sleep_sec)
    log.info(f"Waiting {wait}s to align to next {config.SPOT_INTERVAL} candle boundary")
    time.sleep(wait)

    while _status != BotStatus.STOPPED:
        if _status == BotStatus.PAUSED:
            time.sleep(5)
            continue

        try:
            global _last_tick, _last_tick_time, _missed_tick_alerted, _api_error

            # Retry candle sync + price fetch up to 3 times before giving up on this tick
            prices = None
            for _attempt in range(3):
                try:
                    for pair in all_pairs:
                        sync_candles(pair, config.SPOT_INTERVAL)
                        if config.SPOT_DAILY_EMA_FILTER:
                            sync_candles(pair, "1d")
                    for pair, interval in shadow_intervals:
                        sync_candles(pair, interval)
                    prices = {pair: get_price(pair) for pair in all_pairs}
                    _api_error = None  # clear on success
                    break
                except Exception as _fetch_err:
                    if _attempt < 2:
                        log.warning(f"[TICK] Data fetch failed (attempt {_attempt+1}/3): {_fetch_err} — retrying in 30s")
                        time.sleep(30)
                    else:
                        _api_error = str(_fetch_err)
                        raise

            _last_tick = datetime.datetime.now(tz=zoneinfo.ZoneInfo("Europe/Helsinki")).strftime("%H:%M")
            _last_tick_time = time.time()
            _missed_tick_alerted = False
            notify(
                "[TICK] Prices — " + "  |  ".join(f"{p} €{v:,.2f}" for p, v in prices.items()),
                discord=False,
            )

            state = get_state()
            results = {pair: compute_signal(get_df(pair, config.SPOT_INTERVAL),
                                            rsi_period=config.rsi_period_for(pair),
                                            rsi_oversold=config.rsi_oversold_for(pair),
                                            rsi_overbought=config.rsi_overbought_for(pair),
                                            ema_gap=config.ema_gap_for(pair),
                                            daily_ema=_daily_ema(pair),
                                            vol_period=config.vol_period_for(pair),
                                            vol_mult=config.vol_mult_for(pair))
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

            # Daily summary at first tick of a new day
            global _last_summary_date
            today = datetime.date.today()
            if _last_summary_date is not None and today != _last_summary_date:
                daily_summary(state, config.SPOT_TRADING_PAIRS, prices, results)
            _last_summary_date = today

            # Discord tick — only when there's something to act on or monitor
            _has_signal = any(r.signal in (Signal.BUY, Signal.SELL) for r in results.values())
            _has_position = bool(state.positions)
            if _has_signal or _has_position:
                chart_buf = build_chart(config.SPOT_TRADING_PAIRS, prices, results)
                notify_tick(config.SPOT_TRADING_PAIRS, prices, results, state, chart_buf)

            # Check stop-loss and take-profit on every tick
            check_stops(prices)

            # Execute signals — never sell below entry price
            for pair, result in results.items():
                has_position = pair in state.positions
                if result.signal == Signal.BUY and not has_position:
                    open_position(pair, prices[pair], prices=prices)
                elif has_position:
                    pos = state.positions[pair]
                    drop = (pos.entry_price - prices[pair]) / pos.entry_price
                    next_drop = config.dca_drop_for(pair) + pos.dca_count * config.SPOT_DCA_STEP_PCT
                    if pos.dca_count < config.dca_max_for(pair) and drop >= next_drop:
                        prev_count = pos.dca_count
                        dca_position(pair, prices[pair], prices=prices)
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

            # tick all shadow simulators with the same candle data
            for shadow in get_shadows():
                tick_pairs = shadow.pairs if shadow.pairs else config.SPOT_TRADING_PAIRS
                for pair in tick_pairs:
                    shadow.tick(pair, prices)

            # snapshot portfolio value (cash + open position mark-to-market)
            snap_state = get_state()
            open_val   = sum(prices[p] * pos.amount for p, pos in snap_state.positions.items() if p in prices)
            _db.log_balance(round(snap_state.balance + open_val, 2), config.SPOT_MODE)

            write_status_snapshot(prices)

        except Exception as e:
            log.error(e, exc_info=True)
            notify(f"⚠️ Tick failed — {e}")

        time.sleep(_seconds_until_next_candle(sleep_sec))


def _stop_loop():
    """Fast loop: fetch current prices and run stop checks every SPOT_STOP_CHECK_INTERVAL seconds.
    Runs between candles so trailing stops and take-profits fire without waiting for the next tick."""
    log.info(f"[SPOT] Stop-check loop started ({config.SPOT_STOP_CHECK_INTERVAL}s interval)")
    while _status != BotStatus.STOPPED:
        time.sleep(config.SPOT_STOP_CHECK_INTERVAL)
        if _status != BotStatus.RUNNING:
            continue

        # Missed-tick detection: alert once if no successful tick in >2× the candle interval
        global _missed_tick_alerted
        if _last_tick_time is not None and not _missed_tick_alerted:
            overdue = time.time() - _last_tick_time
            threshold = INTERVAL_SECONDS.get(config.SPOT_INTERVAL, 3600) * 2
            if overdue > threshold:
                notify(f"⚠️ Missed tick — no successful candle processed for {overdue/60:.0f} min (expected every {threshold//60:.0f} min)")
                _missed_tick_alerted = True

        try:
            state = get_state()
            if not state.positions:
                continue
            pairs_needed = set(state.positions)
            for shadow in get_shadows():
                pairs_needed.update(shadow.positions)
            prices = {}
            for pair in pairs_needed:
                try:
                    prices[pair] = get_price(pair)
                except Exception as e:
                    log.warning(f"[SPOT STOP-CHECK] {pair} price fetch failed: {e}")
            if prices:
                check_stops(prices)
                for shadow in get_shadows():
                    shadow.check_stops(prices)
                write_status_snapshot(prices)
        except Exception as e:
            log.error(f"[SPOT STOP-CHECK] {e}", exc_info=True)


_STATUS_SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "status_spot.json")


def write_status_snapshot(prices: dict | None = None):
    """Write a status snapshot to disk so the dashboard can run in a separate process."""
    try:
        state = get_state()
        tz = zoneinfo.ZoneInfo("Europe/Helsinki")
        now_iso = datetime.datetime.now(tz=tz).isoformat()

        if prices is None:
            # best-effort: fetch current prices for open positions only
            prices = {}
            for pair in state.positions:
                try:
                    prices[pair] = get_price(pair)
                except Exception:
                    pass

        positions_out = {}
        for pair, pos in state.positions.items():
            cur = prices.get(pair, pos.value_eur / pos.amount if pos.amount else 0)
            positions_out[pair] = {
                "entry_price":    pos.entry_price,
                "amount":         pos.amount,
                "value_eur":      round(pos.value_eur, 2),
                "highest_price":  pos.highest_price,
                "trailing_stop":  round(pos.trailing_stop_level(), 2),
                "take_profit":    pos.take_profit_price,
                "dca_trigger":    round(pos.entry_price * (1 - (config.dca_drop_for(pair) + pos.dca_count * config.SPOT_DCA_STEP_PCT)), 2) if pos.dca_count < config.dca_max_for(pair) else None,
                "dca_count":      pos.dca_count,
                "dca_max":        config.dca_max_for(pair),
                "break_even":     round(pos.entry_price * (1 + config.SPOT_FEE) / (1 - config.SPOT_FEE), 2),
                "current_price":  round(cur, 2),
            }

        starting = config.SPOT_INVESTED if config.SPOT_INVESTED > 0 else _db.get_starting_balance(config.SPOT_MODE)
        snap = {
            "version":          __import__("bot").__version__,
            "mode":             config.SPOT_MODE,
            "status":           _status.value,
            "api_error":        _api_error,
            "last_tick":        _last_tick,
            "live_since":       state.live_since,
            "started_at":       datetime.datetime.fromtimestamp(_start_time, tz=tz).isoformat() if _start_time else None,
            "interval":         config.SPOT_INTERVAL,
            "stop_check_interval": config.SPOT_STOP_CHECK_INTERVAL,
            "balance":          round(state.balance, 2),
            "starting_balance": starting,
            "total_trades":     state.total_trades,
            "total_pnl":        round(state.total_pnl, 2),
            "total_fees":       round(state.total_fees, 4),
            "positions":        positions_out,
            "written_at":       now_iso,
        }
        tmp = _STATUS_SNAPSHOT_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f, indent=2)
        os.replace(tmp, _STATUS_SNAPSHOT_PATH)
    except Exception as e:
        log.warning(f"[SNAPSHOT] Failed to write status snapshot: {e}")


def _ensure_live_since():
    from . import spot_simulator as _sim
    if config.SPOT_MODE == "live" and get_state().live_since is None:
        get_state().live_since = datetime.datetime.now(tz=zoneinfo.ZoneInfo("Europe/Helsinki")).isoformat()
        _sim._save()


def get_live_since() -> str | None:
    return get_state().live_since


def get_uptime() -> int | None:
    if _start_time is None:
        return None
    return int(time.time() - _start_time)


def start():
    global _thread, _stop_thread, _start_time
    if _status == BotStatus.RUNNING:
        return
    _ensure_live_since()
    init_shadows()
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
