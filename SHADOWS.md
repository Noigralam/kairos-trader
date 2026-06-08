# Shadow Simulation Profiles

All profiles started fresh: **2026-06-08 19:48 (Helsinki)**

## Live baseline (SOLEUR)

| Setting | Value |
|---|---|
| Pairs | SOLEUR |
| Interval | 15m |
| Position size | 75% |
| DCA max | 3 (1% drop, 1% step, 75% size — last tranche 100%) |
| Take profit | 5% |
| Trailing stop | 5% |
| Profit floor | 3% |
| Min exit profit | 3% |
| RSI period | 7 |
| RSI oversold | 30 |
| RSI overbought | 80 |
| EMA gap | 2% |
| Max drawdown pause | 20% |

---

## ACTIVE / ACTIVE_SOL

**Pairs:** ETHEUR+SOLEUR / SOLEUR only  
**Hypothesis:** Faster cycling with no DCA and tighter exits captures more short-term moves.

| Setting | Live | ACTIVE |
|---|---|---|
| DCA max | 3 | **0** |
| RSI oversold | 30 | **33** |
| RSI overbought | 80 | **65** |
| Trailing stop | 5% | **2.5%** |
| Profit floor | 3% | **1.5%** |
| Min exit | 3% | **1%** |
| EMA gap | 2% | **0** |

---

## HYBRID / HYBRID_SOL

**Pairs:** ETHEUR+SOLEUR / SOLEUR only  
**Hypothesis:** Middle ground — modest DCA, tighter exits than live, no EMA filter.

| Setting | Live | HYBRID |
|---|---|---|
| DCA max | 3 | **2** |
| RSI oversold | 30 | **33** |
| RSI overbought | 80 | **70** |
| Trailing stop | 5% | **3.5%** |
| Profit floor | 3% | **2%** |
| Min exit | 3% | **1.5%** |
| EMA gap | 2% | **0** |

---

## ETH

**Pairs:** ETHEUR only  
**Hypothesis:** Track how ETHEUR would perform with its original live settings now that it's off live.

| Setting | Value |
|---|---|
| RSI period | 7 |
| RSI overbought | 65 |
| Everything else | live defaults |

---

## SCALP

**Pairs:** SOLEUR only  
**Hypothesis:** Fast cycling with tight TP and trailing stop maximises trade frequency on SOL's volatility; no DCA keeps capital free.

| Setting | Live | SCALP |
|---|---|---|
| Take profit | 5% | **3%** |
| Trailing stop | 5% | **2%** |
| Profit floor | 3% | **1%** |
| DCA max | 3 | **0** |
| EMA gap | 2% | **0** |

---

## SOL30M

**Pairs:** SOLEUR only  
**Hypothesis:** 30m candles reduce noise and false signals vs 15m; fewer but higher-conviction entries.

| Setting | Live | SOL30M |
|---|---|---|
| Interval | 15m | **30m** |
| Everything else | live defaults | live defaults |

---

## NEAR

**Pairs:** NEAREUR only  
**Hypothesis:** Evaluate NEAR as a candidate live pair using default settings before committing capital.

| Setting | Value |
|---|---|
| RSI period | 14 (default — no per-pair override yet) |
| RSI oversold | 30 |
| RSI overbought | 75 |
| EMA gap | 2% |
| DCA max | 3 |
| Take profit | 5% |

---

## TP3

**Pairs:** ETHEUR+SOLEUR  
**Hypothesis:** 3% take-profit trades more frequently than 5% on both pairs; test whether higher trade count outweighs smaller per-trade gain.

| Setting | Live | TP3 |
|---|---|---|
| Take profit | 5% | **3%** |
| Everything else | live defaults | live defaults |

---

## TIME7

**Pairs:** SOLEUR only  
**Hypothesis:** Forcing a close after 7 days frees stuck capital for fresh cycles; higher churn may outperform waiting for a recovery.

| Setting | Live | TIME7 |
|---|---|---|
| Time stop | off | **7 days** |
| Everything else | live defaults | live defaults |

---

## NOEMA

**Pairs:** SOLEUR only  
**Hypothesis:** Removing the 2% EMA gap filter catches more buy signals in sideways markets; test whether the extra entries are profitable or just noise.

| Setting | Live | NOEMA |
|---|---|---|
| EMA gap | 2% | **0** |
| Everything else | live defaults | live defaults |

---

## RSI25

**Pairs:** SOLEUR only  
**Hypothesis:** Only entering on RSI<25 (vs live RSI<30) means fewer entries but stronger oversold conditions — used by the futures engine with good results.

| Setting | Live | RSI25 |
|---|---|---|
| RSI oversold | 30 | **25** |
| Everything else | live defaults | live defaults |

---

## NEAR_ACTIVE

**Pairs:** NEAREUR only  
**Hypothesis:** NEAR's higher volatility suits the ACTIVE profile's no-DCA fast-exit style better than default settings.

| Setting | Live | NEAR_ACTIVE |
|---|---|---|
| DCA max | 3 | **0** |
| RSI oversold | 30 | **33** |
| RSI overbought | 80 | **65** |
| Trailing stop | 5% | **2.5%** |
| Profit floor | 3% | **1.5%** |
| Min exit | 3% | **1%** |
| EMA gap | 2% | **0** |

---

## SOL1H

**Pairs:** SOLEUR only  
**Hypothesis:** 1h candles give even cleaner signals than 30m; completes the 15m / 30m / 1h comparison set.

| Setting | Live | SOL1H |
|---|---|---|
| Interval | 15m | **1h** |
| Everything else | live defaults | live defaults |
