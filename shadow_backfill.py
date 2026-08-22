"""
Backfill balance_history and trades for ALL spot and futures shadow profiles.

Erases existing shadow data, then replays historical candles from LIVE_START
(first live spot balance_history row) to the latest completed candle (~now).

Safe to re-run with the bot running: uses WAL mode + busy_timeout so writes
queue behind live bot activity rather than crashing on lock.
"""
import sqlite3
import datetime
import zoneinfo
import numpy as np
from dotenv import load_dotenv
load_dotenv()

from bot import db, config
from bot.candles import get_df
from bot.spot_risk import Position, apply_dca as spot_apply_dca, update_peak as spot_update_peak, calc_pnl as spot_calc_pnl
from bot.futures_risk import (
    FuturesPosition, apply_dca as fut_apply_dca, update_peak as fut_update_peak, calc_pnl as fut_calc_pnl,
    check_take_profit, check_trailing_stop, check_liquidation,
)
from bot.spot_simulator import get_shadows, init_shadows
from bot.futures_simulator import get_futures_shadows, init_futures_shadows
from bot.strategy import compute_signal

_TZ             = zoneinfo.ZoneInfo("Europe/Helsinki")
WARMUP          = 210
SPOT_FEE        = config.SPOT_FEE
FUTURES_FEE     = config.FUTURES_FEE
CANDLES_PER_DAY = 96   # 15m candles


# ── helpers ────────────────────────────────────────────────────────────────────

def _precompute(df, rsi_period, ema_span=200):
    close = df["close"]
    ema   = close.ewm(span=ema_span, adjust=False).mean()
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    avg_l = loss.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    rsi   = 100 - (100 / (1 + avg_g / avg_l))
    return rsi.values, ema.values, close.values, df["high"].values, df["low"].values


def _candle_dt(df, i):
    t = df["open_time"].iloc[i]
    if hasattr(t, "to_pydatetime"):
        t = t.to_pydatetime()
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t


def _ts_str(dt):
    return dt.astimezone(_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _spot_params(s):
    o = s.overrides
    g = lambda k, d: o.get(k, d)
    return dict(
        balance         = float(g("spot_balance",              config.SPOT_SIMULATION_BALANCE)),
        pos_pct         = float(g("spot_position_size_pct",    config.SPOT_POSITION_SIZE_PCT)),
        rsi_period      = int(  g("spot_rsi_period",           config.SPOT_RSI_PERIOD)),
        rsi_buy         = int(  g("spot_rsi_oversold",         config.SPOT_RSI_OVERSOLD)),
        rsi_sell        = int(  g("spot_rsi_overbought",       config.SPOT_RSI_OVERBOUGHT)),
        ema_gap         = float(g("spot_ema_gap_pct",          config.SPOT_EMA_GAP_PCT)),
        tp_pct          = float(g("spot_take_profit_pct",      config.SPOT_TAKE_PROFIT_PCT)),
        trail_pct       = float(g("spot_trailing_stop_pct",    config.SPOT_TRAILING_STOP_PCT)),
        floor_pct       = float(g("spot_profit_floor_pct",     config.SPOT_PROFIT_FLOOR_PCT)),
        min_exit        = float(g("spot_min_exit_pct",         config.SPOT_MIN_EXIT_PROFIT_PCT)),
        dca_max         = int(  g("spot_dca_max",              config.SPOT_DCA_MAX)),
        dca_drop        = float(g("spot_dca_drop",             config.SPOT_DCA_DROP_PCT)),
        dca_size        = float(g("spot_dca_size",             config.SPOT_DCA_SIZE_PCT)),
        dca_step        = float(g("spot_dca_step",             config.SPOT_DCA_STEP_PCT)),
        time_stop_days  = float(g("spot_time_stop_days",       config.SPOT_TIME_STOP_DAYS)),
        cooldown        = int(  g("spot_stop_cooldown_candles",0)),
        partial_pct     = float(g("spot_partial_close_pct",    0.0)),
        partial_trail   = float(g("spot_partial_close_trail",  0.02)),
        reentry_drop    = float(g("spot_reentry_drop_pct",     0.0)),
    )


def _fut_params(s):
    o = s.overrides
    g = lambda k, d: o.get(k, d)
    return dict(
        balance    = float(g("futures_balance",    config.FUTURES_SIMULATION_BALANCE)),
        leverage   = int(  g("futures_leverage",   config.FUTURES_LEVERAGE)),
        pos_pct    = float(g("futures_pos_pct",    config.FUTURES_POSITION_SIZE_PCT)),
        rsi_period = int(  g("futures_rsi_period", config.futures_rsi_period_for(s.symbols[0] if s.symbols else "ETHUSDT"))),
        rsi_buy    = int(  g("futures_rsi_oversold",   config.futures_rsi_oversold_for(s.symbols[0] if s.symbols else "ETHUSDT"))),
        rsi_sell   = int(  g("futures_rsi_overbought", config.futures_rsi_overbought_for(s.symbols[0] if s.symbols else "ETHUSDT"))),
        ema_gap    = float(g("futures_ema_gap",    config.futures_ema_gap_for(s.symbols[0] if s.symbols else "ETHUSDT"))),
        dca_drop   = float(g("futures_dca_drop",   config.FUTURES_DCA_DROP_PCT)),
        dca_size   = float(g("futures_dca_size",   config.FUTURES_DCA_SIZE_PCT)),
    )


# ── spot simulation ────────────────────────────────────────────────────────────

def _process_spot_candle(pair, i, precomp, st, balance, p, recording, ts, trade_rows):
    """
    Process one candle for one pair within a shared-balance multi-pair simulation.
    Mutates `st` (per-pair state dict) and `balance` in place via return value.
    Returns (new_balance, had_trade) where had_trade=True means the candle was
    consumed by a trade event (caller should not write a mid-candle snapshot for
    this pair on its own, but the combined mark will still be written).
    """
    rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = precomp
    price = float(close_arr[i])
    rsi   = float(rsi_arr[i])
    ema   = float(ema_arr[i])
    hi    = float(hi_arr[i])
    lo    = float(lo_arr[i])
    dca_step_eff = p["dca_step"] if p["dca_step"] else p["dca_drop"]

    position = st["position"]

    if position is not None:
        spot_update_peak(position, hi)

        if st["partial_closed"]:
            stop_level = position.peak() * (1 - p["partial_trail"])
        else:
            stop_level = max(
                position.entry_price * (1 + SPOT_FEE + p["floor_pct"]) / (1 - SPOT_FEE),
                position.peak() * (1 - p["trail_pct"]),
            )
        tp_price = position.entry_price * (1 + p["tp_pct"])

        # take-profit / partial close
        if hi >= tp_price and not st["partial_closed"]:
            ep = tp_price
            if p["partial_pct"] > 0:
                sell_amt = position.amount * p["partial_pct"]
                bf = position.value_eur * p["partial_pct"] * SPOT_FEE
                sf = sell_amt * ep * SPOT_FEE
                pnl = (ep - position.entry_price) * sell_amt - bf - sf
                balance += sell_amt * ep - sf
                if recording:
                    trade_rows.append((ts, pair, "SELL", ep, sell_amt, sell_amt * ep, bf + sf, pnl, "partial_tp"))
                position.amount    *= (1 - p["partial_pct"])
                position.value_eur *= (1 - p["partial_pct"])
                st["partial_closed"] = True
            else:
                bf  = position.value_eur * SPOT_FEE
                sf  = position.amount * ep * SPOT_FEE
                pnl = spot_calc_pnl(position, ep, bf, sf)
                balance += position.amount * ep - sf
                if recording:
                    trade_rows.append((ts, pair, "SELL", ep, position.amount, position.amount * ep, bf + sf, pnl, "take_profit"))
                st["position"] = None
                st["partial_closed"] = False
            return balance, True

        # trailing stop
        position = st["position"]
        if position is not None and position.peak() > stop_level and lo <= stop_level:
            ep  = stop_level
            bf  = position.value_eur * SPOT_FEE
            sf  = position.amount * ep * SPOT_FEE
            pnl = spot_calc_pnl(position, ep, bf, sf)
            balance += position.amount * ep - sf
            if recording:
                trade_rows.append((ts, pair, "SELL", ep, position.amount, position.amount * ep, bf + sf, pnl, "trailing_stop"))
            st["last_stop_price"] = ep
            st["position"] = None
            st["partial_closed"] = False
            if p["cooldown"] > 0:
                st["cooldown_until"] = i + p["cooldown"]
            return balance, True

        # time stop
        position = st["position"]
        if (position is not None
                and p["time_stop_days"] > 0
                and (i - st["entry_candle"]) > p["time_stop_days"] * CANDLES_PER_DAY
                and position.peak() <= stop_level):
            bf  = position.value_eur * SPOT_FEE
            sf  = position.amount * price * SPOT_FEE
            pnl = spot_calc_pnl(position, price, bf, sf)
            balance += position.amount * price - sf
            if recording:
                trade_rows.append((ts, pair, "SELL", price, position.amount, position.amount * price, bf + sf, pnl, "time_stop"))
            st["position"] = None
            st["partial_closed"] = False
            return balance, True

    # DCA
    position = st["position"]
    if position is not None and st["dca_count"] < p["dca_max"]:
        next_drop = p["dca_drop"] + st["dca_count"] * dca_step_eff
        if (position.entry_price - price) / position.entry_price >= next_drop:
            spend = balance * p["dca_size"]
            if spend >= 1:
                fee = spend * SPOT_FEE
                amt = (spend - fee) / price
                spot_apply_dca(position, price, spend - fee)
                balance -= spend
                st["dca_count"] += 1
                if recording:
                    trade_rows.append((ts, pair, "BUY", price, amt, spend, fee, None, f"dca_{st['dca_count']}"))
                return balance, True

    # entry
    if (st["position"] is None and i >= st["cooldown_until"]
            and rsi < p["rsi_buy"]
            and price > ema * (1 + p["ema_gap"])):
        if p["reentry_drop"] > 0 and st["last_stop_price"] is not None:
            if price > st["last_stop_price"] * (1 - p["reentry_drop"]):
                return balance, False
        spend = balance * p["pos_pct"]
        if spend >= 1:
            fee    = spend * SPOT_FEE
            amt    = (spend - fee) / price
            tp_p   = price * (1 + p["tp_pct"])
            st["position"] = Position(pair, price, amt, spend - fee, tp_p, price)
            balance -= spend
            st["entry_candle"] = i
            st["dca_count"] = 0
            st["partial_closed"] = False
            st["last_stop_price"] = None
            if recording:
                trade_rows.append((ts, pair, "BUY", price, amt, spend, fee, None, "signal"))
            return balance, True

    # RSI sell signal
    position = st["position"]
    if (position is not None and not st["partial_closed"]
            and rsi > p["rsi_sell"]
            and price >= position.entry_price * (1 + p["min_exit"])):
        bf  = position.value_eur * SPOT_FEE
        sf  = position.amount * price * SPOT_FEE
        pnl = spot_calc_pnl(position, price, bf, sf)
        balance += position.amount * price - sf
        if recording:
            trade_rows.append((ts, pair, "SELL", price, position.amount, position.amount * price, bf + sf, pnl, "signal"))
        st["position"] = None
        st["partial_closed"] = False
        return balance, True

    return balance, False


def simulate_spot_shadow(pairs, dfs, start_dt, cutoff_dt, params, balance_in, conn, db_mode):
    """
    Simulate one or more spot pairs sharing a single balance pool.
    Writes ONE combined portfolio mark per candle to balance_history,
    preventing the duplicate-entry / false-spike bug in multi-pair shadows.
    """
    p = params
    precomp = {pair: _precompute(dfs[pair], p["rsi_period"]) for pair in pairs}

    # candle index lookup: pair -> {datetime: array_index}
    pair_indices = {}
    for pair in pairs:
        df = dfs[pair]
        pair_indices[pair] = {}
        for i in range(WARMUP, len(df)):
            t = _candle_dt(df, i)
            pair_indices[pair][t] = i

    all_times = sorted(set().union(*[set(pi.keys()) for pi in pair_indices.values()]))

    states = {pair: {
        "position": None, "entry_candle": 0, "dca_count": 0,
        "cooldown_until": 0, "partial_closed": False, "last_stop_price": None,
    } for pair in pairs}

    balance      = balance_in
    balance_rows = []
    trade_rows   = []
    first_recording_written = False

    for tick_num, t in enumerate(all_times):
        if t >= cutoff_dt:
            break

        if t < start_dt:
            continue   # RSI/EMA are precomputed — no pre-start trading needed

        ts        = _ts_str(t)
        had_trade = False

        # Write the clean starting balance once, at the very first recorded tick
        if not first_recording_written:
            balance_rows.append((ts, round(balance_in, 2)))
            first_recording_written = True

        for pair in pairs:
            if t not in pair_indices[pair]:
                continue
            i  = pair_indices[pair][t]
            st = states[pair]
            balance, traded = _process_spot_candle(
                pair, i, precomp[pair], st, balance, p, True, ts, trade_rows
            )
            if traded:
                had_trade = True

        # ONE combined mark per candle tick
        if had_trade or tick_num % 4 == 0:
            total_mark = balance
            for pair in pairs:
                pos = states[pair]["position"]
                if pos is not None and t in pair_indices[pair]:
                    i = pair_indices[pair][t]
                    total_mark += pos.amount * float(precomp[pair][2][i])  # close_arr
            balance_rows.append((ts, round(total_mark, 2)))

    # final snapshot
    if balance_rows:
        final_mark = balance
        for pair in pairs:
            pos = states[pair]["position"]
            if pos is not None:
                final_mark += pos.amount * float(precomp[pair][2][-1])
        balance_rows.append((balance_rows[-1][0], round(final_mark, 2)))

    for ts, pair_, side, price_, amt, val, fee, pnl, notes in trade_rows:
        conn.execute(
            "INSERT INTO trades (timestamp, pair, side, price, amount, value_eur, fee, mode, pnl, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, pair_, side, price_, amt, val, fee, db_mode, pnl, notes)
        )
    for ts, bal in balance_rows:
        conn.execute(
            "INSERT INTO balance_history (timestamp, balance, mode) VALUES (?,?,?)",
            (ts, bal, db_mode)
        )

    return balance


# ── futures simulation ─────────────────────────────────────────────────────────

def simulate_futures_pair(symbol, df, start_dt, cutoff_dt, params, balance_in, conn, db_mode):
    """
    Simulate one futures symbol from start_dt to cutoff_dt.
    Returns final balance.
    """
    p = params
    rsi_arr, ema_arr, close_arr, hi_arr, lo_arr = _precompute(df, p["rsi_period"])

    balance  = balance_in
    position = None   # FuturesPosition or None

    balance_rows = []
    trade_rows   = []
    first_recording_written = False

    for i in range(WARMUP, len(close_arr)):
        t = _candle_dt(df, i)
        if t >= cutoff_dt:
            break
        if t < start_dt:
            continue   # RSI/EMA are precomputed — no pre-start trading needed

        ts    = _ts_str(t)

        if not first_recording_written:
            balance_rows.append((ts, round(balance_in, 2)))
            first_recording_written = True

        price = float(close_arr[i])
        rsi   = float(rsi_arr[i])
        ema   = float(ema_arr[i])

        if position is not None:
            fut_update_peak(position, price)

            if check_liquidation(position, price):
                reason = "liquidation"
            elif check_take_profit(position, price):
                reason = "take_profit"
            elif check_trailing_stop(position, price):
                reason = "trailing_stop"
            else:
                reason = None

            if reason:
                ep        = price
                notional  = ep * position.amount
                exit_fee  = notional * FUTURES_FEE
                entry_fee = position.entry_price * position.amount * FUTURES_FEE
                pnl       = fut_calc_pnl(position, ep, entry_fee=entry_fee, exit_fee=exit_fee)
                balance  += max(position.margin + pnl, 0)
                trade_rows.append((ts, symbol, "SELL", ep, position.amount, notional, exit_fee, pnl, reason))
                balance_rows.append((ts, round(balance, 2)))
                position = None
                continue

            # DCA
            if not position.dca_done:
                drop = (position.entry_price - price) / position.entry_price
                if drop >= p["dca_drop"]:
                    margin = min(balance * p["dca_size"], balance)
                    if margin >= 1:
                        notional  = margin * position.leverage
                        amount    = notional / price
                        fee       = notional * FUTURES_FEE
                        fut_apply_dca(position, price, margin)
                        balance  -= margin + fee
                        trade_rows.append((ts, symbol, "BUY", price, amount, notional, fee, None, "dca"))
                        mark = balance + position.unrealized_pnl(price)
                        balance_rows.append((ts, round(mark, 2)))
                        continue

        else:
            # check buy signal using a rolling window of the df up to i
            sub_df = df.iloc[max(0, i - 250):i + 1]
            result = compute_signal(
                sub_df,
                rsi_period     = p["rsi_period"],
                rsi_oversold   = p["rsi_buy"],
                rsi_overbought = p["rsi_sell"],
                ema_gap        = p["ema_gap"],
            )
            from bot.strategy import Signal
            if result.signal == Signal.BUY:
                margin   = balance * p["pos_pct"]
                leverage = p["leverage"]
                notional = margin * leverage
                amount   = notional / price
                if amount > 0 and margin >= 1:
                    fee      = notional * FUTURES_FEE
                    position = FuturesPosition(
                        symbol=symbol, side="LONG",
                        entry_price=price, amount=amount, margin=margin,
                        leverage=leverage,
                        take_profit_price=price * (1 + config.FUTURES_TAKE_PROFIT_PCT),
                        highest_price=price,
                        opened_at=t.timestamp(),
                    )
                    balance -= margin + fee
                    trade_rows.append((ts, symbol, "BUY", price, amount, notional, fee, None, f"lev={leverage}x"))
                    mark = balance + position.unrealized_pnl(price)
                    balance_rows.append((ts, round(mark, 2)))
                    continue

        # periodic mark snapshot
        if i % 4 == 0:
            open_pnl = position.unrealized_pnl(price) if position else 0
            mark = balance + open_pnl
            balance_rows.append((ts, round(mark, 2)))

    for ts, sym_, side, price_, amt, val, fee, pnl, notes in trade_rows:
        conn.execute(
            "INSERT INTO trades (timestamp, pair, side, price, amount, value_eur, fee, mode, pnl, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, sym_, side, price_, amt, val, fee, db_mode, pnl, notes)
        )
    for ts, bal in balance_rows:
        conn.execute(
            "INSERT INTO balance_history (timestamp, balance, mode) VALUES (?,?,?)",
            (ts, bal, db_mode)
        )

    return balance


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    conn = sqlite3.connect(db.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    # live spot start = our reference zero point
    live_start_row = conn.execute(
        "SELECT MIN(timestamp) FROM balance_history WHERE mode='live'"
    ).fetchone()[0]
    if not live_start_row:
        print("No live balance history found — nothing to do.")
        conn.close()
        return

    live_start_ts = live_start_row
    start_dt = datetime.datetime.fromisoformat(live_start_ts)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=datetime.timezone.utc)
    print(f"Live spot started: {live_start_ts}")

    # cutoff = ~now (latest completed candle, ~4 min ago)
    now_utc   = datetime.datetime.now(datetime.timezone.utc)
    cutoff_dt = now_utc - datetime.timedelta(minutes=4)
    print(f"Backfill cutoff:   {cutoff_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    cutoff_ts = cutoff_dt.isoformat()

    # ── wipe all existing shadow data ─────────────────────────────────────────
    print("\nErasing all shadow data...")
    for tbl in ("balance_history", "trades"):
        conn.execute(f"DELETE FROM {tbl} WHERE mode LIKE 'shadow_%'")
        conn.execute(f"DELETE FROM {tbl} WHERE mode LIKE 'futures_shadow_%'")
    conn.commit()
    print("Done.\n")

    # ── spot shadows ──────────────────────────────────────────────────────────
    init_shadows()
    spot_shadows = [s for s in get_shadows() if s.overrides.get("type") != "grid"]
    skipped_grid = [s.name for s in get_shadows() if s.overrides.get("type") == "grid"]

    for s in spot_shadows:
        db_mode  = s._db_mode
        pairs    = list(s.pairs if s.pairs else config.SPOT_TRADING_PAIRS)
        interval = getattr(s, "interval", config.SPOT_INTERVAL)
        params   = _spot_params(s)

        print(f"SPOT {s.name:<20} {live_start_ts} → {cutoff_ts[:19]}")

        dfs = {}
        for pair in pairs:
            pair_df = get_df(pair, interval, limit=50000)
            if pair_df.empty:
                print(f"  {pair}: no candles — skip")
            else:
                dfs[pair] = pair_df

        pairs_with_data = [p for p in pairs if p in dfs]
        if not pairs_with_data:
            print("  no candle data — skip")
            continue

        balance = simulate_spot_shadow(
            pairs_with_data, dfs, start_dt, cutoff_dt, params, params["balance"], conn, db_mode
        )
        conn.commit()
        print(f"  → €{balance:.2f} final cash")

    # ── futures shadows ───────────────────────────────────────────────────────
    print()
    init_futures_shadows()
    fut_shadows = get_futures_shadows()

    for s in fut_shadows:
        db_mode = s._db_mode
        symbols = list(s.symbols if s.symbols else config.FUTURES_TRADING_PAIRS)
        params  = _fut_params(s)
        balance = params["balance"]

        print(f"FUTURES {s.name:<18} {live_start_ts} → {cutoff_ts[:19]}")

        for symbol in symbols:
            sym_df = get_df(symbol, "15m", limit=50000)
            if sym_df.empty:
                print(f"  {symbol}: no candles — skip")
                continue
            balance = simulate_futures_pair(
                symbol, sym_df, start_dt, cutoff_dt, params, balance, conn, db_mode
            )

        conn.commit()
        print(f"  → ${balance:.2f} final cash")

    conn.close()

    if skipped_grid:
        print(f"\nSkipped grid shadows: {', '.join(skipped_grid)}")
    print("\nBackfill complete.")


if __name__ == "__main__":
    main()
