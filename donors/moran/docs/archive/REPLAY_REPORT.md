# Sniperbot Replay Analysis Report
_Generated: 2026-04-08 15:53 UTC_
_History records: 3000 | Total closes: 263 | Excluded (contaminated): **31** | Clean closes: **232** | With mark data: 92 | With nonzero MFE: 47_

## 1. Dataset Overview

### Contamination Filter — 31 Trades Excluded

> Contaminated = `sell_attempt_ts=0.0` (ghost close, no TX) OR `proceeds_usd=0.0` OR `roi_pct ≤ −99.0` (corrupted accounting). Excluded from ALL analysis below.

| Close Reason                      | Excluded Count |
| --------------------------------- | -------------- |
| HARD_CAP_EXIT                     | 10             |
| RAPID DUMP EXIT                   | 7              |
| MANUAL SELL 100%                  | 4              |
| TRAIL/SL EXIT                     | 4              |
| QUOTEABILITY_GUARD_EXIT           | 3              |
| QUOTEABILITY_GUARD_EXIT:transient | 3              |

### Close Reason Distribution

| Close Reason                      | Count | % of Closes | Avg ROI | Total P&L |
| --------------------------------- | ----- | ----------- | ------- | --------- |
| QUOTEABILITY_GUARD_EXIT           | 94    | 40.5%       | -0.5%   | $-5.6537  |
| TRAIL/SL EXIT                     | 38    | 16.4%       | -5.6%   | $-6.9060  |
| HARD_CAP_EXIT                     | 17    | 7.3%        | -18.6%  | $-1.9454  |
| STALE_MARK_TIMEOUT_EXIT           | 16    | 6.9%        | -8.0%   | $-3.8404  |
| QUOTEABILITY_GUARD_EXIT:no_pool   | 16    | 6.9%        | -1.5%   | $-0.5119  |
| QUOTEABILITY_GUARD_EXIT:transient | 11    | 4.7%        | -7.1%   | $-0.6331  |
| RAPID DUMP EXIT                   | 9     | 3.9%        | -20.8%  | $-1.1489  |
| STAGNATION_EXIT                   | 8     | 3.4%        | -4.2%   | $-0.9984  |
| FAILSAFE STOP                     | 5     | 2.2%        | -23.4%  | $-3.5132  |
| VELOCITY_TRAIL_EXIT               | 5     | 2.2%        | -19.6%  | $-2.9428  |
| MANUAL SELL 100%                  | 4     | 1.7%        | 5.8%    | +$1.7546  |
| AGE_GATE_EXIT                     | 4     | 1.7%        | -4.2%   | $-0.1756  |
| FAILOVER_LIQUIDATION              | 3     | 1.3%        | 2.3%    | +$0.2076  |
| TTL_TIMEOUT_EXIT                  | 2     | 0.9%        | -9.7%   | $-0.5815  |

### Realized Performance (All Closes)

- **Total trades closed:** 232
- **Wins / Losses:** 51 / 181 (22.0% win rate)
- **Total realized P&L:** $-26.8887
- **Average ROI per trade:** -5.4%
- **Average hold time:** 152s

## 2. Latency Distribution

### Detection → Execution Latency (`detect_to_exec_ms`)

_N = 140 execution attempts with latency data_

| Percentile | Latency  | Trades > this |
| ---------- | -------- | ------------- |
| p25        | 785 ms   | 105/140       |
| p50        | 1283 ms  | 70/140        |
| p75        | 3596 ms  | 35/140        |
| p90        | 7905 ms  | 14/140        |
| p99        | 10611 ms | 2/140         |

- **> 3 000 ms:** 43 / 140 (30.7%)
- **> 5 000 ms:** 27 / 140 (19.3%)

### Execution Submit Latency (`execution_latency_ms`)

_N = 1161 execution attempts_

| Percentile | Latency  |
| ---------- | -------- |
| p50        | 142 ms   |
| p75        | 2500 ms  |
| p90        | 5943 ms  |
| p99        | 21200 ms |

## 3. Counterfactual Scenarios

> **Note on scope:** Stop-loss and TP counterfactuals run on ALL closed trades.
> Trades with `mfe_pct = 0` (QUOTEABILITY_GUARD_EXIT, STALE_MARK exits, etc.)
> have no mark data — their outcome is unchanged across stop/TP scenarios.
> Latency filter counterfactuals run on the execution_attempt population.

| Scenario                    | N   | Wins | Loss | Win%  | Avg ROI | Avg P&L  | Total P&L | Note                                        |
| --------------------------- | --- | ---- | ---- | ----- | ------- | -------- | --------- | ------------------------------------------- |
| Baseline (actual)           | 232 | 51   | 181  | 22.0% | -5.41%  | $-0.1159 | $-26.8887 |                                             |
| Stop loss −6%               | 232 | 51   | 181  | 22.0% | -1.90%  | $-0.0545 | $-12.6422 |                                             |
| Stop loss −8%               | 232 | 51   | 181  | 22.0% | -2.24%  | $-0.0615 | $-14.2678 |                                             |
| Stop loss −10%              | 232 | 51   | 181  | 22.0% | -2.63%  | $-0.0708 | $-16.4207 |                                             |
| Stop loss −12%              | 232 | 51   | 181  | 22.0% | -2.88%  | $-0.0758 | $-17.5852 | ← current                                   |
| Stop loss −15%              | 232 | 51   | 181  | 22.0% | -3.30%  | $-0.0826 | $-19.1556 |                                             |
| Stop loss −20%              | 232 | 51   | 181  | 22.0% | -3.84%  | $-0.0909 | $-21.0865 |                                             |
| Trail stop −6% from MFE     | 47  | 37   | 10   | 78.7% | 61.05%  | +$0.5785 | +$27.1915 |                                             |
| Trail stop −7% from MFE     | 47  | 37   | 10   | 78.7% | 59.64%  | +$0.5602 | +$26.3315 |                                             |
| Trail stop −8% from MFE     | 47  | 37   | 10   | 78.7% | 58.22%  | +$0.5419 | +$25.4715 |                                             |
| Trail stop −12% from MFE    | 47  | 32   | 15   | 68.1% | 52.76%  | +$0.4703 | +$22.1026 | ← current est.                              |
| Trail stop −15% from MFE    | 47  | 30   | 17   | 63.8% | 48.97%  | +$0.4226 | +$19.8628 |                                             |
| Trail stop −20% from MFE    | 47  | 27   | 20   | 57.4% | 41.19%  | +$0.3187 | +$14.9801 |                                             |
| Latency filter ≤2000ms      | 201 | 39   | 162  | 19.4% | -6.18%  | $-0.1304 | $-26.2200 | skips 31/232 trades with known late latency |
| Latency filter ≤3000ms      | 208 | 43   | 165  | 20.7% | -6.02%  | $-0.1272 | $-26.4480 | skips 24/232 trades with known late latency |
| Latency filter ≤5000ms      | 222 | 48   | 174  | 21.6% | -5.93%  | $-0.1215 | $-26.9809 | skips 10/232 trades with known late latency |
| Latency filter ≤10000ms     | 232 | 51   | 181  | 22.0% | -5.41%  | $-0.1159 | $-26.8887 | skips 0/232 trades with known late latency  |
| Partial TP +20% (sell 30%)  | 232 | 56   | 176  | 24.1% | -4.65%  | $-0.0995 | $-23.0927 | new                                         |
| Partial TP +30% (sell 40%)  | 232 | 59   | 173  | 25.4% | -4.29%  | $-0.0963 | $-22.3381 | TP1 current                                 |
| Partial TP +50% (sell 30%)  | 232 | 58   | 174  | 25.0% | -4.33%  | $-0.0955 | $-22.1491 | new                                         |
| Partial TP +100% (sell 25%) | 232 | 57   | 175  | 24.6% | -4.19%  | $-0.0944 | $-21.9064 | new                                         |
| Quoteability window 10s     | 232 | 51   | 181  | 22.0% | -5.41%  | $-0.1159 | $-26.8887 |                                             |
| Quoteability window 20s     | 232 | 51   | 181  | 22.0% | -5.41%  | $-0.1159 | $-26.8887 |                                             |
| Quoteability window 35s     | 232 | 47   | 185  | 20.3% | -5.07%  | $-0.1132 | $-26.2556 | ← current                                   |
| Quoteability window 60s     | 232 | 47   | 185  | 20.3% | -5.07%  | $-0.1132 | $-26.2556 |                                             |

## 4. Stop Loss Deep Dive

Trades where stop level matters = trades where MAE breaches the stop threshold.
Total closed trades: **232** | With nonzero MAE: **70**

| Stop Level | Trades Stopped | Avg ROI (stopped) | Avg ROI (not stopped) | Total P&L Δ vs baseline |
| ---------- | -------------- | ----------------- | --------------------- | ----------------------- |
| −6%        | 57             | -16.0%            | -2.0%                 | +$14.2465               |
| −8%        | 53             | -16.8%            | -2.0%                 | +$12.6209               |
| −10%       | 49             | -18.0%            | -2.0%                 | +$10.4680               |
| −12%  ←    | 44             | -18.8%            | -2.3%                 | +$9.3035                |
| −15%       | 40             | -19.7%            | -2.4%                 | +$7.7331                |
| −20%       | 34             | -19.8%            | -2.9%                 | +$5.8022                |

## 5. Latency Filter Impact

Closed trades with latency data: **69** / 232
_(Trades without latency data are **kept** under all filter scenarios.)_

| Max Latency | Trades Kept | Trades Skipped | Win%  | Avg ROI | Total P&L |
| ----------- | ----------- | -------------- | ----- | ------- | --------- |
| ≤2000ms     | 201         | 31             | 19.4% | -6.18%  | $-26.2200 |
| ≤3000ms     | 208         | 24             | 20.7% | -6.02%  | $-26.4480 |
| ≤5000ms     | 222         | 10             | 21.6% | -5.93%  | $-26.9809 |
| ≤10000ms    | 232         | 0              | 22.0% | -5.41%  | $-26.8887 |

## 6. MFE vs Actual Exit — Missed Upside

_Trades with nonzero MFE: **47**_

- **Avg MFE:**      73.5%
- **Avg exit ROI:** -7.2%
- **Avg gap:**      80.7%  _(upside left on the table per trade)_

### Top 10 Trades by Missed Upside (MFE − Exit ROI)

| Mint (prefix) | MFE    | Exit ROI | Gap    | Exit Reason     | Hold Time |
| ------------- | ------ | -------- | ------ | --------------- | --------- |
| 9sze2N4nXEqQ… | 936.4% | 18.8%    | 917.7% | TRAIL/SL EXIT   | 228s      |
| D5ZH2uvmcrCn… | 399.1% | -6.1%    | 405.1% | TRAIL/SL EXIT   | 37s       |
| FcS3t2tzksjD… | 228.8% | 7.2%     | 221.5% | TRAIL/SL EXIT   | 19s       |
| Fo5SMkYk2rqR… | 200.2% | -1.7%    | 201.8% | TRAIL/SL EXIT   | 74s       |
| Dk2eBYFNP76L… | 150.4% | -16.6%   | 167.0% | RAPID DUMP EXIT | 1467s     |
| CwLerN3g6i1T… | 151.0% | 1.0%     | 150.0% | TRAIL/SL EXIT   | 11s       |
| Hwbfe9UnYKXa… | 112.1% | -24.1%   | 136.1% | RAPID DUMP EXIT | 10s       |
| mHSnVDGFdkLG… | 143.1% | 10.9%    | 132.2% | TRAIL/SL EXIT   | 10s       |
| PtLy4SofsE1s… | 111.4% | -14.7%   | 126.1% | TRAIL/SL EXIT   | 20s       |
| 77cN4Vw39V6G… | 116.4% | -6.8%    | 123.2% | RAPID DUMP EXIT | 7s        |

### MFE Distribution Across Marked Trades

| MFE Range | Count | Avg MFE | Avg Exit ROI | Avg Gap |
| --------- | ----- | ------- | ------------ | ------- |
| 0–10%     | 11    | 2.5%    | -15.3%       | 17.9%   |
| 10–30%    | 16    | 18.5%   | -7.8%        | 26.3%   |
| 30–75%    | 7     | 45.6%   | -3.6%        | 49.3%   |
| 75–200%   | 9     | 116.2%  | -4.3%        | 120.4%  |
| >200%     | 4     | 441.1%  | 4.6%         | 436.5%  |

## 7. Partial Take-Profit Scenarios (on Marked Trades)

_Basis: 47 trades with nonzero MFE. TP fires only when MFE ≥ TP level._

| TP Level            | Sell Frac | TP Fires | Avg Blended ROI | Avg P&L  | Total P&L |
| ------------------- | --------- | -------- | --------------- | -------- | --------- |
| +20%                | 30%       | 27 / 47  | -3.47%          | $-0.0978 | $-4.5968  |
| +30% ← current TP1  | 40%       | 20 / 47  | -1.71%          | $-0.0817 | $-3.8422  |
| +50%                | 30%       | 16 / 47  | -1.86%          | $-0.0777 | $-3.6532  |
| +75% ← current TP2  | 20%       | 13 / 47  | -2.97%          | $-0.0972 | $-4.5661  |
| +100%               | 25%       | 11 / 47  | -1.19%          | $-0.0726 | $-3.4105  |
| +200% ← current TP3 | 20%       | 4 / 47   | -3.88%          | $-0.1031 | $-4.8450  |

## 8. Quoteability Exit Analysis

**Quoteability exits: 121 / 232 (52.2% of all closes)**

- Base `QUOTEABILITY_GUARD_EXIT`:          94 trades  (avg ROI -0.5%)
- `QUOTEABILITY_GUARD_EXIT:no_pool`:       16 trades  (avg ROI -1.5%)
- `QUOTEABILITY_GUARD_EXIT:transient`:     11 trades  (avg ROI -7.1%)
- **Total P&L from quoteability exits:    $-6.7987**

### Window Sensitivity

_(Assumptions: `no_pool` exits are unaffected by window length. `transient` exits resolve at 0% ROI with longer window. Base exits unchanged.)_

| Window        | Total Trades | Win%  | Avg ROI | Total P&L | vs Current |
| ------------- | ------------ | ----- | ------- | --------- | ---------- |
| 10s           | 232          | 22.0% | -5.41%  | $-26.8887 | $-0.6331   |
| 20s           | 232          | 22.0% | -5.41%  | $-26.8887 | $-0.6331   |
| 35s ← current | 232          | 20.3% | -5.07%  | $-26.2556 | +$0.0000   |
| 60s           | 232          | 20.3% | -5.07%  | $-26.2556 | +$0.0000   |
| 120s          | 232          | 20.3% | -5.07%  | $-26.2556 | +$0.0000   |

## 9. Top 5 Trades by MFE — Outcome vs Potential

### #1: `9sze2N4nXEqQKaWv…`

| Metric                               | Value         |
| ------------------------------------ | ------------- |
| MFE (peak gain)                      | 936.4%        |
| MAE (max drawdown)                   | 0.0%          |
| Actual exit ROI                      | 18.8%         |
| Actual P&L                           | +$0.0634      |
| Close reason                         | TRAIL/SL EXIT |
| Hold time                            | 228s          |
| Source                               | delayed_entry |
| Detect→exec latency                  | 845ms         |
| ─── Counterfactuals ───              |               |
| Trail stop −12% from MFE → ROI       | 812.1%        |
| Trail stop −20% from MFE → ROI       | 729.1%        |
| Partial TP +20% (sell 30%) → blended | 19.1%         |
| Missed upside (MFE − exit)           | 917.7%        |

### #2: `D5ZH2uvmcrCn1nC9…`

| Metric                               | Value         |
| ------------------------------------ | ------------- |
| MFE (peak gain)                      | 399.1%        |
| MAE (max drawdown)                   | 0.0%          |
| Actual exit ROI                      | -6.1%         |
| Actual P&L                           | $-0.0606      |
| Close reason                         | TRAIL/SL EXIT |
| Hold time                            | 37s           |
| Source                               | delayed_entry |
| Detect→exec latency                  | 937ms         |
| ─── Counterfactuals ───              |               |
| Trail stop −12% from MFE → ROI       | 339.2%        |
| Trail stop −20% from MFE → ROI       | 299.2%        |
| Partial TP +20% (sell 30%) → blended | 1.8%          |
| Missed upside (MFE − exit)           | 405.1%        |

### #3: `FcS3t2tzksjDSuVi…`

| Metric                               | Value         |
| ------------------------------------ | ------------- |
| MFE (peak gain)                      | 228.8%        |
| MAE (max drawdown)                   | 0.0%          |
| Actual exit ROI                      | 7.2%          |
| Actual P&L                           | +$0.0059      |
| Close reason                         | TRAIL/SL EXIT |
| Hold time                            | 19s           |
| Source                               | delayed_entry |
| Detect→exec latency                  | 864ms         |
| ─── Counterfactuals ───              |               |
| Trail stop −12% from MFE → ROI       | 189.3%        |
| Trail stop −20% from MFE → ROI       | 163.0%        |
| Partial TP +20% (sell 30%) → blended | 11.1%         |
| Missed upside (MFE − exit)           | 221.5%        |

### #4: `Fo5SMkYk2rqRKGwm…`

| Metric                               | Value         |
| ------------------------------------ | ------------- |
| MFE (peak gain)                      | 200.2%        |
| MAE (max drawdown)                   | 0.0%          |
| Actual exit ROI                      | -1.7%         |
| Actual P&L                           | $-0.0500      |
| Close reason                         | TRAIL/SL EXIT |
| Hold time                            | 74s           |
| Source                               | primary       |
| Detect→exec latency                  | —             |
| ─── Counterfactuals ───              |               |
| Trail stop −12% from MFE → ROI       | 164.1%        |
| Trail stop −20% from MFE → ROI       | 140.1%        |
| Partial TP +20% (sell 30%) → blended | 4.8%          |
| Missed upside (MFE − exit)           | 201.8%        |

### #5: `CwLerN3g6i1TdPRB…`

| Metric                               | Value         |
| ------------------------------------ | ------------- |
| MFE (peak gain)                      | 151.0%        |
| MAE (max drawdown)                   | 0.0%          |
| Actual exit ROI                      | 1.0%          |
| Actual P&L                           | +$0.0293      |
| Close reason                         | TRAIL/SL EXIT |
| Hold time                            | 11s           |
| Source                               | primary       |
| Detect→exec latency                  | —             |
| ─── Counterfactuals ───              |               |
| Trail stop −12% from MFE → ROI       | 120.9%        |
| Trail stop −20% from MFE → ROI       | 100.8%        |
| Partial TP +20% (sell 30%) → blended | 6.7%          |
| Missed upside (MFE − exit)           | 150.0%        |

## 10. MFE Capture Rate

> **Capture rate** = `realized_roi_pct / mfe_pct`. 1.0 (100%) = exited exactly at peak. <0 = sold after price reversed past entry. >1.0 = exited above MFE (rounding / partial-fill artifact).

_Clean trades with MFE > 0: **47**_

| Metric           | Value    |
| ---------------- | -------- |
| Avg capture rate | -217.27% |
| p25 capture rate | -142.57% |
| p50 capture rate | -23.63%  |
| p75 capture rate | 1.56%    |

| Capture Rate Bucket | Count | % of Trades |
| ------------------- | ----- | ----------- |
| <0%                 | 33    | 70.2%       |
| 0–25%               | 6     | 12.8%       |
| 25–50%              | 2     | 4.3%        |
| 50–75%              | 2     | 4.3%        |
| 75–100%             | 2     | 4.3%        |
| >100%               | 2     | 4.3%        |

## 11. Hold Time vs Outcome

| Hold Time | Count | Win%  | Avg ROI | Avg MFE | Avg Capture Rate |
| --------- | ----- | ----- | ------- | ------- | ---------------- |
| <5s       | 3     | 0.0%  | -4.6%   | 0.0%    | —                |
| 5–15s     | 17    | 17.6% | -5.3%   | 30.8%   | -295.06%         |
| 15–30s    | 28    | 39.3% | -7.9%   | 21.4%   | -39.38%          |
| 30–60s    | 35    | 22.9% | -9.1%   | 19.5%   | -216.50%         |
| 60–120s   | 94    | 19.1% | -4.3%   | 4.8%    | -144.17%         |
| >120s     | 55    | 20.0% | -3.7%   | 21.8%   | -373.88%         |

## 12. Entry Price Deduplication Check

> An entry price appearing in >3 trades is suspicious — it may indicate a stuck reference price feed recycling the same value across unrelated tokens.

**2 entry price(s) appear in >3 trades (208 / 232 clean trades affected)**

| Entry Price | Trade Count | Sample Mints                                                                         |
| ----------- | ----------- | ------------------------------------------------------------------------------------ |
| 1.3000e-05  | 128         | `DYpKzbVf41bHn2…`, `44e3tk3VsZPgHe…`, `CYHiZGj5RTpyzV…`, `GenLgev2PTwWVo…` +124 more |
| 6.1626e-05  | 80          | `Dk2eBYFNP76Lry…`, `E6ifp2mJy8cYQe…`, `E7BBA3zG2dPosn…`, `6SKFJGhSFSyCh9…` +76 more  |

**Interpretation:** These prices are almost certainly stuck feed values, not real reference prices. Trades using a recycled entry price have unreliable entry/exit ROI comparisons and stop/TP trigger points.

## 13. Key Observations & Recommendations

- **QUOTEABILITY_GUARD_EXIT is 52% of all closes** — the dominant failure mode. These trades enter but Jupiter can't quote them within `QUOTEABILITY_VALIDATION_WINDOW_SECONDS=35s`. Total P&L from these exits: $-6.7987. Primary fix: ensure entry only happens after route is confirmed (already in `_pre_entry_swap_route_check`).

- **Trail/SL exits leave significant upside on the table:** 38 TRAIL/SL EXIT trades have avg MFE=92.0% but avg exit ROI=-5.6%. Gap of 97.6% suggests the trailing stop triggers too aggressively near entry before the pump completes.

- **43/140 executions (31%) had detection→exec latency > 3 000 ms.** The latency filter counterfactuals above show the P&L impact of skipping late entries.

- **Average MFE−exit gap is 80.7% across 47 marked trades.** Adding an early partial TP (e.g. +20% sell 30%) would lock in gains before the trailing stop fires on the way back down.

---
_Replay analysis is read-only. No simulation_mode.py logic was modified._
_Source: `./history.json`_