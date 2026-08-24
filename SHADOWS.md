# Shadow Simulation Profiles

All profiles run in real-time alongside the live bot from their creation date forward.

The live bot currently trades **SOLEUR only** with: RSI(7), buy<30, sell>80, trail=2.5%, floor=1.5%, min\_exit=1%, TP=5%, DCA×3 (1%/1%/75%), EMA gap=0.

---

## Spot shadows

### LIVE2500

**Pairs:** SOLEUR | **Balance:** €2500  
**Hypothesis:** Same strategy as live, scaled to €2500 — shows what the account would look like with more capital deployed.

> Exact live params (RSI 7, buy<30, sell>80) at 10× the balance.

---

### LIVE_CD

**Pairs:** SOLEUR | **Balance:** €200  
**Hypothesis:** After a trailing stop, require SOL to drop another 3% before re-entering — avoids buying back into a continuation drop.

> Live params + `REENTRY_DROP_PCT=3%`

---

### LIVE_CDT

**Pairs:** SOLEUR | **Balance:** €200  
**Hypothesis:** After a trailing stop, block re-entry for 4 candles (1h) — avoids the same whipsaw pattern as LIVE_CD but with a time gate instead of a price gate.

> Live params + `STOP_COOLDOWN_CANDLES=4`

---

### ACTIVE / ACTIVE_SOL

**Pairs:** SOLEUR+ETHEUR / SOLEUR only | **Balance:** €200  
**Hypothesis:** Faster cycling with no DCA and tighter exits captures more short-term moves on both pairs.

> DCA 3→**0** · RSI oversold 30→**33** · RSI overbought 80→**65** · Trail 2.5%→unchanged · Floor 1.5%→unchanged · Min exit 1%→**unchanged** · EMA gap 0→unchanged  
> (uses global RSI period 14 for SOLEUR — intentional contrast with live's RSI 7)

---

### HYBRID / HYBRID_SOL

**Pairs:** SOLEUR+ETHEUR / SOLEUR only | **Balance:** €200  
**Hypothesis:** Middle ground — modest DCA, tighter exits than live, RSI(7) for both pairs.

> DCA 3→**2** · RSI(7) · buy<**33** · sell<**70** · Trail **3.5%** · Floor **2%** · Min exit **1.5%** · EMA gap **0**

---

### SCALP

**Pairs:** SOLEUR | **Balance:** €200  
**Hypothesis:** Tight TP and trailing stop maximise trade frequency on SOL's volatility; no DCA keeps capital free.

> TP 5%→**3%** · Trail **2%** · Floor **1%** · DCA→**0** · EMA gap→**0**  
> (RSI global default 14 — intentional)

---

### SOL30M

**Pairs:** SOLEUR | **Balance:** €200 | **Interval:** 30m  
**Hypothesis:** 30m candles reduce noise vs 15m; fewer but higher-conviction entries.

> Interval→**30m** · all else live defaults (RSI 14 — calibrated for 30m)

---

### SOL1H

**Pairs:** SOLEUR | **Balance:** €200 | **Interval:** 1h  
**Hypothesis:** 1h candles give cleaner signals than 30m; completes the 15m/30m/1h comparison.

> Interval→**1h** · all else live defaults (RSI 14 — calibrated for 1h)

---

### SOL4H

**Pairs:** SOLEUR | **Balance:** €200 | **Interval:** 4h  
**Hypothesis:** 4h candles catch only the major oversold swings; fewer trades but stronger conviction.

> Interval→**4h** · RSI(14) · buy<**35** · sell<**70** · Trail **3.5%** · Floor **2%** · Min exit **1.5%** · TP **7%** · DCA→**0** · EMA gap→**0**

---

### SOL4H_FNG

**Pairs:** SOLEUR | **Balance:** €200 | **Interval:** 4h  
**Hypothesis:** Same as SOL4H with a Fear & Greed gate — skip entries when FNG>35 (not oversold enough at market level).

> SOL4H params + **FNG_MAX=35**

---

### SOL4H_FNG_CD

**Pairs:** SOLEUR | **Balance:** €200 | **Interval:** 4h  
**Hypothesis:** SOL4H_FNG with an additional 3% re-entry price gate after trailing stops.

> SOL4H_FNG params + **REENTRY_DROP_PCT=3%**

---

### SOL4H_FNG_CDT

**Pairs:** SOLEUR | **Balance:** €200 | **Interval:** 4h  
**Hypothesis:** SOL4H_FNG with a 2-candle (8h) cooldown after trailing stops instead of a price gate.

> SOL4H_FNG params + **STOP_COOLDOWN_CANDLES=2**

---

### NOEMA

**Pairs:** SOLEUR | **Balance:** €200  
**Hypothesis:** Removing the EMA gap filter catches more signals in sideways markets — test whether extra entries are profitable or noise.

> EMA gap→**0** · all else live defaults

---

### RSI25

**Pairs:** SOLEUR | **Balance:** €200  
**Hypothesis:** RSI<25 means fewer but stronger oversold entries — mirrors what the futures engine uses.

> RSI oversold 30→**25** · all else live defaults

---

### TIME7

**Pairs:** SOLEUR | **Balance:** €200  
**Hypothesis:** Forcing a close after 7 days frees stuck capital for fresh cycles.

> TIME_STOP_DAYS=**7** · all else live defaults

---

### SOL_PC (Partial Close)

**Pairs:** SOLEUR | **Balance:** €200  
**Hypothesis:** Sell 50% at 10% TP to bank profit; trail the remaining 50% with a 5% stop — captures spikes while staying in the move.

> TP 5%→**10%** · PARTIAL_CLOSE_PCT=**50%** · PARTIAL_CLOSE_TRAIL=**5%** · RSI(7) · buy<30 · sell>80 · DCA max **1**

---

### SOL_DOT

**Pairs:** SOLEUR + DOTEUR | **Balance:** €200  
**Hypothesis:** Apply live SOL parameters across two coins — tests whether the same strategy works on DOT while sharing a balance pool.

> Live SOLEUR params (RSI 7, buy<30, sell>80, trail=2.5%, floor=2%) applied to both SOLEUR and DOTEUR

---

### ETH

**Pairs:** ETHEUR | **Balance:** €200  
**Hypothesis:** Track how ETHEUR would perform with its original per-pair live settings now that it's off the live trading list.

> RSI(7) · sell<**65** · all else live defaults

---

### NEAR

**Pairs:** NEAREUR | **Balance:** €200  
**Hypothesis:** Evaluate NEAR as a candidate live pair using default settings before committing capital.

> All global defaults (RSI 14 — no per-pair override yet)

---

### NEAR2

**Pairs:** NEAREUR | **Balance:** €200  
**Hypothesis:** Tuned NEAR params from backtest sweep — RSI(7) with 2% floor and 3% TP outperformed global defaults significantly (+€272 vs +€55 over 730d). Tests whether the optimised setup holds up in live conditions.

> RSI(7) · buy<30 · sell>75 · Trail **2.5%** · Floor **2%** · Min exit **1%** · TP **3%** · DCA×**3**

---

### XRP

**Pairs:** XRPEUR | **Balance:** €200  
**Hypothesis:** Test XRP with tighter exits tuned for its volatility profile.

> RSI(7) · buy<**33** · Trail **2.5%** · Floor **2%** · Min exit **2%** · EMA gap→**0**

---

### XRP_GRID *(grid strategy)*

**Pairs:** XRPEUR | **Balance:** €200  
**Type:** Grid — places limit orders at fixed spacing levels; not RSI-based.

> 8 levels · 1.5% spacing

---

### BNB

**Pairs:** BNBEUR | **Balance:** €200  
**Hypothesis:** Test BNB using an optimised setup from backtests.

> RSI(7) · buy<**32** · Trail **2.5%** · Floor **3%** · Min exit **2%** · EMA gap→**0**

---

### DOT

**Pairs:** DOTEUR | **Balance:** €200  
**Hypothesis:** DOTEUR moves slowly; a wider profit floor gives the trailing stop room before it fires.

> RSI(7) · buy<**33** · sell<**65** · Trail **2%** · Floor **4%** · Min exit **1%** · TP **5%** · DCA max **1** · EMA gap→**0**

---

## Futures shadows

All futures shadows use **ETHUSDT** or **SOLUSDT** at 2× leverage unless stated. Base strategy: RSI(7) buy<25, EMA gap 2%, TP 5%, trailing stop 5% (floor 1%), 100% position size.

### ETH / SOL

**Symbols:** ETHUSDT / SOLUSDT  
**Hypothesis:** Baseline futures performance per symbol — exact live params, tracked separately to see which drives results.

---

### NO_FNG_ETH / NO_FNG_SOL

**Symbols:** ETHUSDT / SOLUSDT  
**Hypothesis:** Remove the funding rate gate (`MAX_FUNDING_RATE=0`) — shows cost of paying high funding vs the missed entries the gate causes.

> MAX_FUNDING_RATE disabled

---

### LEV1X_ETH / LEV1X_SOL

**Symbols:** ETHUSDT / SOLUSDT  
**Hypothesis:** Run at 1× leverage (no amplification) to isolate strategy alpha from leverage effect.

> LEVERAGE=**1**

---

### NO_EMA_ETH / NO_EMA_SOL

**Symbols:** ETHUSDT / SOLUSDT  
**Hypothesis:** Remove the EMA trend filter — enters in downtrends too; shows the cost of the filter vs raw RSI performance.

> EMA_GAP→**0** (trend guard disabled)

---

### LEV3X_ETH / LEV3X_SOL

**Symbols:** ETHUSDT / SOLUSDT  
**Hypothesis:** 3× leverage on the same signal — direct gain amplifier if signal quality holds. Liquidation on isolated margin is still far enough away at 3× with a 5% TP.

> LEVERAGE=**3**

---

### HIGH_TP

**Symbols:** ETHUSDT + SOLUSDT  
**Hypothesis:** TP=10%, trail=8%, floor=3% — fewer trades but captures bigger moves. Tests whether riding runs longer beats the current 5% quick-exit cadence.

> TP **10%** · Trail **8%** · Floor **3%**

---

### DCA_SOL

**Symbols:** SOLUSDT  
**Hypothesis:** Enable DCA at 3% drop from entry, deploying 50% of remaining balance. SOL's volatility makes DCA averaging meaningful — lower entry, better sell price.

> DCA_DROP=**3%** · DCA_SIZE=**50%**

---

### RSI30_SOL

**Symbols:** SOLUSDT  
**Hypothesis:** RSI<30 instead of <25 — more entry signals. Tests whether extra entries are profitable or noise compared to the stricter threshold.

> RSI_OVERSOLD=**30**

---

### BOTH

**Symbols:** ETHUSDT + SOLUSDT  
**Hypothesis:** Run both symbols sharing one balance, mirroring what the live futures engine actually does. Per-symbol shadows don't capture the diversification effect.

> All live defaults, both symbols in one pool
