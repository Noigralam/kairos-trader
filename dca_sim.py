"""
Simulate combined ETH+SOL portfolio with monthly cash top-ups.
Usage: python dca_sim.py [start_balance] [monthly_topup] [days]
"""
import sys
from dotenv import load_dotenv
load_dotenv()

from bot import config
from bot.strategy import compute_signal, Signal
from bot.candles import get_df
from bot.risk import Position, apply_dca, update_peak, check_take_profit, calc_pnl

PAIRS      = ["ETHEUR", "SOLEUR"]
START_BAL  = float(sys.argv[1]) if len(sys.argv) > 1 else 200.0
MONTHLY    = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0
DAYS       = int(sys.argv[3])   if len(sys.argv) > 3 else 365
FEE        = config.BINANCE_FEE
POS_PCT    = config.POSITION_SIZE_PCT
TP_PCT     = config.TAKE_PROFIT_PCT
TRAIL_PCT  = config.TRAILING_STOP_PCT
DCA_DROP   = config.DCA_DROP_PCT
MIN_EXIT   = config.MIN_EXIT_PROFIT_PCT
FLOOR      = 0.01
WARMUP     = 210

print(f"Loading candles…", flush=True)
dfs = {p: get_df(p, "15m", limit=DAYS*96+WARMUP) for p in PAIRS}

common_idx = None
for df in dfs.values():
    idx = set(df["open_time"].astype(str))
    common_idx = idx if common_idx is None else common_idx & idx

aligned = {}
for pair, df in dfs.items():
    df = df[df["open_time"].astype(str).isin(common_idx)].reset_index(drop=True)
    aligned[pair] = df

min_len = min(len(df) for df in aligned.values())
print(f"Running {min_len - WARMUP:,} candles across {len(PAIRS)} pairs…", flush=True)

balance     = START_BAL
positions   = {}
total_pnl   = 0.0
total_fees  = 0.0
trades      = 0
added       = 0.0
last_month  = None
monthly_log = []

for i in range(WARMUP, min_len):
    row0      = aligned[PAIRS[0]].iloc[i]
    ts        = row0["open_time"].to_pydatetime()
    month_key = (ts.year, ts.month)

    if last_month is not None and month_key != last_month:
        balance += MONTHLY
        added   += MONTHLY
    last_month = month_key

    for pair in PAIRS:
        df    = aligned[pair]
        row   = df.iloc[i]
        price = float(row["close"])
        hi    = float(row["high"])
        lo    = float(row["low"])
        pos   = positions.get(pair)

        if pos:
            update_peak(pos, hi)
            stop_level = max(
                pos.entry_price * (1 + FEE + FLOOR) / (1 - FEE),
                pos.peak() * (1 - TRAIL_PCT),
            )
            if check_take_profit(pos, hi):
                ep   = pos.take_profit_price
                bfee = pos.value_eur * FEE
                sfee = pos.amount * ep * FEE
                pnl  = calc_pnl(pos, ep, bfee, sfee)
                balance    += pos.amount * ep - sfee
                total_pnl  += pnl
                total_fees += bfee + sfee
                trades     += 1
                del positions[pair]
                continue
            if pos.peak() > stop_level and lo <= stop_level:
                ep   = stop_level
                bfee = pos.value_eur * FEE
                sfee = pos.amount * ep * FEE
                pnl  = calc_pnl(pos, ep, bfee, sfee)
                balance    += pos.amount * ep - sfee
                total_pnl  += pnl
                total_fees += bfee + sfee
                trades     += 1
                del positions[pair]
                continue

        window = df.iloc[:i+1]
        result = compute_signal(window)

        if result.signal == Signal.BUY and pair not in positions:
            size = balance * POS_PCT
            if size < 1:
                continue
            amount   = size / price
            buy_fee  = size * FEE
            tp_price = price * (1 + TP_PCT)
            pos      = Position(pair, price, amount, size, tp_price, price)
            pos._entry_time = ts
            positions[pair] = pos
            balance    -= size + buy_fee
            total_fees += buy_fee

        elif result.signal == Signal.BUY and pair in positions:
            pos  = positions[pair]
            drop = (pos.entry_price - price) / pos.entry_price
            if not pos.dca_done and drop >= DCA_DROP:
                dca_val = balance * POS_PCT
                if dca_val >= 1:
                    buy_fee = dca_val * FEE
                    apply_dca(pos, price, dca_val)
                    balance    -= dca_val + buy_fee
                    total_fees += buy_fee

        elif result.signal == Signal.SELL and pair in positions:
            pos = positions[pair]
            if price >= pos.entry_price * (1 + MIN_EXIT):
                bfee = pos.value_eur * FEE
                sfee = pos.amount * price * FEE
                pnl  = calc_pnl(pos, price, bfee, sfee)
                balance    += pos.amount * price - sfee
                total_pnl  += pnl
                total_fees += bfee + sfee
                trades     += 1
                del positions[pair]

    pos_value = sum(
        p.amount * float(aligned[pr].iloc[i]["close"])
        for pr, p in positions.items()
    )
    monthly_log.append((ts, balance + pos_value))

# Close open positions at last candle price
for pair, pos in list(positions.items()):
    price = float(aligned[pair].iloc[-1]["close"])
    bfee  = pos.value_eur * FEE
    sfee  = pos.amount * price * FEE
    pnl   = calc_pnl(pos, price, bfee, sfee)
    balance    += pos.amount * price - sfee
    total_pnl  += pnl
    total_fees += bfee + sfee

taxable        = max(0.0, total_pnl)
tax            = min(taxable, 30000) * 0.30 + max(0.0, taxable - 30000) * 0.34
after_tax_pnl  = total_pnl - tax
total_invested = START_BAL + added

print(f"\n{'═'*54}")
print(f"  ETH + SOL  |  {DAYS} days  |  +€{MONTHLY:.0f}/month")
print(f"{'═'*54}")
print(f"  Total invested : €{total_invested:.2f}  (€{START_BAL:.0f} start + €{added:.0f} added)")
print(f"  Final balance  : €{balance:.2f}")
print(f"  Trading PnL    : €{total_pnl:+.2f}")
print(f"  Tax (FI 30%)   : €{tax:.2f}")
print(f"  After-tax PnL  : €{after_tax_pnl:+.2f}")
print(f"  After-tax bal  : €{START_BAL + added + after_tax_pnl:.2f}")
print(f"  ROI on invested: {(balance - total_invested) / total_invested * 100:+.1f}%")
print(f"  Trades         : {trades}  |  Fees: €{total_fees:.2f}")

print(f"\n  Month-by-month portfolio value (cash + open positions):")
print(f"  {'Month':<10}  {'Value':>8}  {'vs Invested':>12}")
print(f"  {'─'*10}  {'─'*8}  {'─'*12}")
seen = set()
cumulative_invested = START_BAL
for ts, val in monthly_log:
    key = (ts.year, ts.month)
    if key not in seen:
        seen.add(key)
        gain = val - cumulative_invested
        print(f"  {ts.strftime('%Y-%m'):<10}  €{val:>7.2f}  {gain:>+11.2f}")
        cumulative_invested += MONTHLY
