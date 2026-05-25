import logging
import threading
import time
from enum import Enum
from . import config

log = logging.getLogger("cryptobot")
from .exchange import get_klines, get_price
from .strategy import compute_signal, Signal
from .candles import initial_sync, sync as sync_candles, get_df
from .simulator import open_position, close_position, dca_position, get_state, check_stops, manual_add
from .notifier import notify, notify_tick, bot_status_alert, build_chart

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
_last_tick: str = None
_prev_tick: dict = {}   # pair -> {price, rsi, ema200}


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


def _loop():
    notify(f"Bot started | mode={config.MODE} | pairs={', '.join(config.TRADING_PAIRS)} | interval={config.INTERVAL}")
    sleep_sec = INTERVAL_SECONDS.get(config.INTERVAL, 3600)

    for pair in config.TRADING_PAIRS:
        initial_sync(pair, config.INTERVAL)

    while _status != BotStatus.STOPPED:
        if _status == BotStatus.PAUSED:
            time.sleep(5)
            continue

        try:
            import datetime, zoneinfo
            global _last_tick
            _last_tick = datetime.datetime.now(tz=zoneinfo.ZoneInfo("Europe/Helsinki")).strftime("%H:%M")
            for pair in config.TRADING_PAIRS:
                sync_candles(pair, config.INTERVAL)

            prices = {pair: get_price(pair) for pair in config.TRADING_PAIRS}
            notify(
                "[TICK] Prices — " + "  |  ".join(f"{p} €{v:,.2f}" for p, v in prices.items()),
                discord=False,
            )

            state = get_state()
            results = {pair: compute_signal(get_df(pair, config.INTERVAL),
                                            rsi_period=config.rsi_period_for(pair),
                                            rsi_oversold=config.RSI_OVERSOLD,
                                            rsi_overbought=config.RSI_OVERBOUGHT)
                       for pair in config.TRADING_PAIRS}

            # Per-pair detail: file + web buffer only
            for pair, result in results.items():
                pos_flag = "IN" if pair in state.positions else "OUT"
                trend_flag = "↑" if float(get_price(pair)) > result.ema_trend else "↓"
                notify(
                    f"[TICK] {pair} ({pos_flag})  RSI={result.rsi:.1f}  "
                    f"EMA200={result.ema_trend:.2f} {trend_flag}  "
                    f"→ {result.signal.value}: {result.reason}",
                    discord=False,
                )

            # Discord summary — one message per tick
            import datetime as _dt, zoneinfo as _zi
            tick_time = _dt.datetime.now(tz=_zi.ZoneInfo("Europe/Helsinki")).strftime("%H:%M")

            pair_blocks = []
            for pair, result in results.items():
                price   = prices[pair]
                gap     = price - result.ema_trend
                has_pos = pair in state.positions
                pos     = state.positions.get(pair)
                prev    = _prev_tick.get(pair, {})

                price_arrow = _arrow(price,            prev.get("price"))
                rsi_arrow   = _arrow(result.rsi,       prev.get("rsi"))
                ema_arrow   = _arrow(result.ema_trend, prev.get("ema200"))
                _prev_tick[pair] = {"price": price, "rsi": result.rsi, "ema200": result.ema_trend}

                if result.rsi < config.RSI_OVERSOLD:
                    rsi_zone = "oversold"
                elif result.rsi > config.RSI_OVERBOUGHT:
                    rsi_zone = "overbought"
                else:
                    rsi_zone = "neutral"

                trend_line = (f"↑ €{gap:,.2f} above EMA200{ema_arrow}"
                              if gap >= 0 else f"↓ €{abs(gap):,.2f} below EMA200{ema_arrow}")

                if result.signal.value == "BUY" and not has_pos:
                    action = "**BUYING**"
                elif result.signal.value == "SELL" and has_pos:
                    action = "**SELLING**"
                elif result.signal.value == "SELL" and not has_pos:
                    action = "HOLD — overbought, no position"
                elif gap < 0:
                    action = "HOLD — waiting for price above EMA200"
                else:
                    action = "HOLD — waiting for RSI below 30"

                block = (
                    f"**{pair}**  {'`IN`' if has_pos else '`—`'}\n"
                    f"Price  : €{price:,.2f}{price_arrow}\n"
                    f"Trend  : {trend_line}\n"
                    f"RSI    : {result.rsi:.1f}{rsi_arrow}  ({rsi_zone})\n"
                )
                if pos:
                    pct = (price - pos.entry_price) / pos.entry_price * 100
                    block += (
                        f"Entry  : €{pos.entry_price:,.2f}  ({pct:+.1f}%)\n"
                        f"Stop   : €{pos.trailing_stop_level():,.2f}\n"
                        f"TP     : €{pos.take_profit_price:,.2f}\n"
                    )
                block += f"→ {action}"
                pair_blocks.append(block)

            footer = (f"```\n"
                      f"PnL    : €{state.total_pnl:+.2f}\n"
                      f"Fees   : €{state.total_fees:.4f}\n"
                      f"Trades : {state.total_trades}\n"
                      f"```")

            message = f"**[TICK]** {tick_time}\n\n" + "\n\n".join(pair_blocks) + "\n\n" + footer
            chart_buf = build_chart(config.TRADING_PAIRS, prices, results)
            notify_tick(message, chart_buf)

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
                    if not pos.dca_done and drop >= config.DCA_DROP_PCT and result.rsi < config.DCA_RSI_THRESHOLD:
                        dca_position(pair, prices[pair])
                        if pos.dca_done:
                            notify(f"[DCA] {pair} down {drop*100:.1f}% from entry, RSI={result.rsi:.1f} — averaged down", discord=False)
                if result.signal == Signal.SELL and has_position:
                    pos = state.positions[pair]
                    min_exit = pos.entry_price * (1 + config.MIN_EXIT_PROFIT_PCT)
                    if prices[pair] >= min_exit:
                        close_position(pair, prices[pair], reason="signal")
                    else:
                        notify(f"[HOLD] {pair} SELL signal suppressed — price €{prices[pair]:,.2f} below min exit €{min_exit:,.2f} (entry €{pos.entry_price:,.2f} +{config.MIN_EXIT_PROFIT_PCT*100:.1f}%)", discord=False)

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


def manual_buy(pair: str, size_pct: float):
    """Manually buy — opens or adds to an existing position."""
    try:
        price = get_price(pair)
        manual_add(pair, price, size_pct)
        notify(f"[MANUAL BUY] {pair} @ €{price:.2f} ({size_pct*100:.0f}% of balance)")
    except Exception as e:
        notify(f"[MANUAL BUY ERROR] {e}")
