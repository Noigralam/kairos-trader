import json
import logging
import os
import threading
import time
import datetime
import zoneinfo

from . import config
from .futures_exchange import get_klines, get_mark_price, get_funding_rate, get_next_funding_time
from .candles import sync as _sync_candles
from .strategy import compute_signal, Signal
from .futures_simulator import (
    init as sim_init, get_state, open_long, dca_long, close_long,
    check_stops, apply_funding,
    init_futures_shadows, get_futures_shadows,
)
from .notifier import notify
from . import db as _db

log = logging.getLogger("cryptobot")

INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

_FUTURES_INTERVAL    = "15m"
_STOP_CHECK_INTERVAL = config.FUTURES_STOP_CHECK_INTERVAL

_running  = False
_paused   = False
_thread: threading.Thread = None
_stop_thread: threading.Thread = None
_lock     = threading.Lock()
_last_tick: str = None
_start_time: float = None

# unix ms of next funding settlement per symbol (fetched on startup)
_next_funding: dict[str, int] = {}


def get_last_tick() -> str:
    return _last_tick


def get_uptime() -> int | None:
    if _start_time is None:
        return None
    return int(time.time() - _start_time)


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
            for sh in get_futures_shadows():
                sh.apply_funding(symbol, rate)
        _next_funding[symbol] = get_next_funding_time(symbol)
    except Exception as e:
        log.warning(f"[FUTURES FUNDING] {symbol} fetch failed: {e}")


def _loop():
    global _running, _last_tick

    sim_init()
    init_futures_shadows()
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
        if _paused:
            time.sleep(5)
            continue
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
                    _sync_candles(sym, _FUTURES_INTERVAL)
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

            # Apply funding before stop checks: if accumulated funding tips a position into
            # loss territory, the stop should see the true net PnL, not a pre-funding snapshot.
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
                    if config.FUTURES_MAX_FUNDING_RATE > 0:
                        try:
                            funding = get_funding_rate(sym)
                            if funding > config.FUTURES_MAX_FUNDING_RATE:
                                notify(
                                    f"[FUTURES] {sym} BUY suppressed — funding {funding*100:.4f}%/8h"
                                    f" > max {config.FUTURES_MAX_FUNDING_RATE*100:.4f}%",
                                    discord=False,
                                )
                                continue
                        except Exception as _fe:
                            log.warning(f"[FUTURES] funding rate fetch failed for {sym}: {_fe}")
                    open_long(sym, price)

                elif has_pos:
                    pos  = state.positions[sym]
                    drop = (pos.entry_price - price) / pos.entry_price
                    if not pos.dca_done and drop >= config.FUTURES_DCA_DROP_PCT:
                        dca_long(sym, price)

                # Futures exits are driven by check_stops (take-profit / trailing stop),
                # not RSI overbought. RSI can cross "overbought" many times during a
                # strong trend; the trailing stop locks in profit without cutting too early.

            # Tick futures shadows
            for sh in get_futures_shadows():
                for sym in sh.symbols:
                    if sym in prices and sym in dfs:
                        sh.tick(sym, prices[sym], dfs[sym])
                sh.log_snapshot(prices)

            # Snapshot portfolio value (cash + open position unrealised PnL)
            snap = get_state()
            open_pnl = sum(p.unrealized_pnl(prices[s]) for s, p in snap.positions.items() if s in prices)
            _db.log_balance(round(snap.balance + open_pnl, 2), f"futures_{config.FUTURES_MODE}")

            write_status_snapshot(prices)

        except Exception as e:
            log.error(f"[FUTURES LOOP] {e}", exc_info=True)
            notify(f"[FUTURES ERROR] {e}", discord=False)

        time.sleep(_seconds_until_next_candle(sleep_sec))


def _stop_loop():
    """Fast loop: fetch mark prices and run stop checks every _STOP_CHECK_INTERVAL seconds.
    Skips candle fetching and signal computation — pure risk management only."""
    log.info(f"[FUTURES] Stop-check loop started ({_STOP_CHECK_INTERVAL}s interval)")
    while _running:
        time.sleep(_STOP_CHECK_INTERVAL)
        if not _running or _paused:
            continue
        try:
            state = get_state()
            shadow_has_positions = any(sh.positions for sh in get_futures_shadows())
            if not state.positions and not shadow_has_positions:
                continue
            prices = {}
            syms_needed = set(state.positions)
            for sh in get_futures_shadows():
                syms_needed.update(sh.positions)
            for sym in syms_needed:
                try:
                    prices[sym] = get_mark_price(sym)
                except Exception as e:
                    log.warning(f"[FUTURES STOP-CHECK] {sym} price fetch failed: {e}")
            if prices:
                for sym in list(state.positions):
                    if sym in prices:
                        _maybe_apply_funding(sym, prices[sym])
                check_stops(prices)
                for sh in get_futures_shadows():
                    sh.check_stops(prices)
                write_status_snapshot(prices)
        except Exception as e:
            log.error(f"[FUTURES STOP-CHECK] {e}", exc_info=True)


_STATUS_SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "status_futures.json")


def write_status_snapshot(prices: dict | None = None):
    """Write a futures status snapshot to disk so the dashboard can run in a separate process."""
    try:
        state = get_state()
        tz = zoneinfo.ZoneInfo("Europe/Helsinki")
        now_iso = datetime.datetime.now(tz=tz).isoformat()

        if prices is None:
            prices = {}
            for sym in state.positions:
                try:
                    prices[sym] = get_mark_price(sym)
                except Exception:
                    pass

        positions_out = {}
        for sym, pos in state.positions.items():
            cur = prices.get(sym, pos.entry_price)
            positions_out[sym] = {
                "side":            pos.side,
                "entry_price":     pos.entry_price,
                "amount":          pos.amount,
                "margin":          round(pos.margin, 2),
                "leverage":        pos.leverage,
                "notional":        round(pos.notional, 2),
                "take_profit":     pos.take_profit_price,
                "trailing_stop":   round(pos.trailing_stop_level(), 2),
                "liquidation":     round(pos.liquidation_price(), 2),
                "highest_price":   pos.highest_price,
                "funding_paid":    round(pos.funding_paid, 4),
                "unrealized_pnl":  round(pos.unrealized_pnl(cur), 2),
                "pnl_pct":         round(pos.pnl_pct(cur) * 100, 2),
                "current_price":   round(cur, 4),
            }

        starting = _db.get_starting_balance(f"futures_{config.FUTURES_MODE}")
        snap = {
            "version":          __import__("bot").__version__,
            "mode":             config.FUTURES_MODE,
            "status":           "running" if _running and not _paused else ("paused" if _paused else "stopped"),
            "last_tick":        _last_tick,
            "started_at":       datetime.datetime.fromtimestamp(_start_time, tz=tz).isoformat() if _start_time else None,
            "interval":         _FUTURES_INTERVAL,
            "balance":          round(state.balance, 2),
            "starting_balance": starting,
            "total_trades":     state.total_trades,
            "total_pnl":        round(state.total_pnl, 2),
            "total_fees":       round(state.total_fees, 4),
            "total_funding":    round(state.total_funding, 4),
            "positions":        positions_out,
            "written_at":       now_iso,
        }
        tmp = _STATUS_SNAPSHOT_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f, indent=2)
        os.replace(tmp, _STATUS_SNAPSHOT_PATH)
    except Exception as e:
        log.warning(f"[SNAPSHOT] Failed to write futures status snapshot: {e}")


def start():
    global _running, _thread, _stop_thread, _start_time
    with _lock:
        if _running:
            return
        _running = True
        _start_time = time.time()
    _thread      = threading.Thread(target=_loop,       daemon=True, name="futures-engine")
    _stop_thread = threading.Thread(target=_stop_loop,  daemon=True, name="futures-stop-check")
    _thread.start()
    _stop_thread.start()


def stop():
    global _running, _paused
    with _lock:
        _running = False
        _paused  = False
    notify("[FUTURES] Engine stopped.", discord=False)


def pause():
    global _paused
    with _lock:
        _paused = True
    notify("[FUTURES] Engine paused.", discord=False)


def resume():
    global _paused
    with _lock:
        _paused = False
    notify("[FUTURES] Engine resumed.", discord=False)


def is_running() -> bool:
    return _running


def is_paused() -> bool:
    return _paused


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
