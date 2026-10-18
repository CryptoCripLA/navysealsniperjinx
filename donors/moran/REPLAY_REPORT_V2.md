# Sniperbot Replay Analysis Report V2
_Generated: 2026-04-23 00:18 UTC_
_Post-fix cutoff: **2026-04-08 21:00 UTC** (rapid-dump guard + sell-retry-6001)_

## Summary

| Metric | Count |
| ------ | ----- |
| Total history records | 4375 |
| Total reconstructed closes | 698 |
| Contaminated (excluded) | 142 |
| Clean closes (all time) | 556 |
| Pre-fix closes (before cutoff) | 222 |
| **Post-fix closes (after cutoff)** | **334** |
| Post-fix: price_suspect | 93 |
| Post-fix: real entry price | 241 |

## 1. Contamination Filter

| Close Reason                      | Excluded Count |
| --------------------------------- | -------------- |
| TRAIL/SL EXIT                     | 55             |
| GHOST_BUY_NO_TOKENS               | 29             |
| RAPID DUMP EXIT                   | 26             |
| HARD_CAP_EXIT                     | 21             |
| QUOTEABILITY_GUARD_EXIT           | 3              |
| QUOTEABILITY_GUARD_EXIT:transient | 3              |
| MANUAL SELL 100%                  | 2              |
| TIME_EXIT_120S                    | 2              |
| STALE_MARK_TIMEOUT_EXIT           | 1              |

## 2. Win Rate — Post-fix Trades

**Post-fix** — 334 trades

| Metric | Value |
| ------ | ----- |
| Wins / Losses | 96 / 238 |
| Win rate | 28.7% |
| Total realized P&L | $-25.2414 |
| Average ROI | -5.2% |
| Best ROI | 159.8% |
| Worst ROI | -96.0% |

### By price_suspect flag

**Real entry price (post-fix)** — 241 trades

| Metric | Value |
| ------ | ----- |
| Wins / Losses | 75 / 166 |
| Win rate | 31.1% |
| Total realized P&L | $-20.2395 |
| Average ROI | -5.3% |
| Best ROI | 84.8% |
| Worst ROI | -96.0% |

**price_suspect (post-fix)** — 93 trades

| Metric | Value |
| ------ | ----- |
| Wins / Losses | 21 / 72 |
| Win rate | 22.6% |
| Total realized P&L | $-5.0019 |
| Average ROI | -4.9% |
| Best ROI | 159.8% |
| Worst ROI | -95.4% |

> **price_suspect trades** have entry_price ≈ graduation curve constant (`6.16e-05`) or legacy reference `1.3e-05`. Their ROI figures are unreliable because the entry price is a reference estimate, not an actual fill price.

## 3. Close Reason Distribution (Post-fix)

| Close Reason            | Count | % of Closes | Avg ROI | Total P&L |
| ----------------------- | ----- | ----------- | ------- | --------- |
| TRAIL/SL EXIT           | 192   | 57.5%       | 4.9%    | +$0.8098  |
| HARD_CAP_EXIT           | 83    | 24.9%       | -30.4%  | $-23.1333 |
| RAPID DUMP EXIT         | 37    | 11.1%       | -4.2%   | $-2.4053  |
| TIME_EXIT_120S          | 10    | 3.0%        | -1.0%   | $-0.4031  |
| MANUAL SELL 100%        | 6     | 1.8%        | 2.5%    | $-0.0445  |
| QUOTEABILITY_GUARD_EXIT | 3     | 0.9%        | -15.3%  | $-0.2298  |
| STALE_MARK_TIMEOUT_EXIT | 2     | 0.6%        | 28.0%   | +$0.2198  |
| TTL_EXIT                | 1     | 0.3%        | -2.5%   | $-0.0550  |

## 4. Hold Time Distribution (Post-fix)

| Percentile   | Hold Time        |
| ------------ | ---------------- |
| p25          | 24s (0.4 min)    |
| p50 (median) | 58s (1.0 min)    |
| p75          | 123s (2.0 min)   |
| p90          | 234s (3.9 min)   |
| max          | 3662s (61.0 min) |

- **Mean hold time:** 134s (2.2 min)
- **< 60s:** 172 / 334 (51.5%)
- **< 5 min:** 313 / 334 (93.7%)

## 5. MFE Capture Rate (Post-fix)

_Trades with nonzero MFE: 221 / 334_

| Metric                                      | Value   |
| ------------------------------------------- | ------- |
| Avg MFE                                     | 20.5%   |
| Avg exit ROI (same trades)                  | 2.0%    |
| Median MFE capture                          | 0.4%    |
| Mean MFE capture (skewed by rugs)           | -401.2% |
| Positive capture (kept some MFE)            | 111     |
| Negative capture (gave back all + more)     | 110     |
| Trades where exit > 0 (despite MFE > 0)     | 111     |
| Trades where exit < 0 (gave back all gains) | 110     |

- **MFE capture p25/p50/p75:** p25=-176.2 p50=0.4 p75=59.4 p90=84.2

## 6. Trail Stop Counterfactual — `-6%` vs `-8%` from MFE Peak

_Using Post-fix trades with nonzero MFE (221 trades)_

| Scenario           | N   | Wins | Win Rate | Avg ROI | Total P&L |
| ------------------ | --- | ---- | -------- | ------- | --------- |
| Actual (as-traded) | 221 | 91   | 41.2%    | 2.0%    | $-3.5466  |
| Trail −6% from MFE | 221 | 171  | 77.4%    | 11.7%   | +$20.5574 |
| Trail −8% from MFE | 221 | 158  | 71.5%    | 9.9%    | +$17.2206 |

- **−6% captures +$3.3368 more** than −8% (tighter stop wins)

## 7. price_suspect vs Real Entry Price Comparison

**Real entry price trades** — 241 trades

| Metric            | Value                                            |
| ----------------- | ------------------------------------------------ |
| Win rate          | 36.5%                                            |
| Avg ROI           | -5.3%                                            |
| Total P&L         | $-20.2395                                        |
| Avg MFE (nonzero) | 19.1%                                            |
| Avg hold time     | 127s                                             |
| Entry prices      | 0.005357, None, 7.119e-05, 1.386e-05, 3.706e-05… |

**price_suspect trades** — 93 trades

| Metric            | Value                                                  |
| ----------------- | ------------------------------------------------------ |
| Win rate          | 31.2%                                                  |
| Avg ROI           | -4.9%                                                  |
| Total P&L         | $-5.0019                                               |
| Avg MFE (nonzero) | 24.7%                                                  |
| Avg hold time     | 151s                                                   |
| Entry prices      | 6.163e-05, 6.163e-05, 6.163e-05, 6.163e-05, 6.163e-05… |

> price_suspect trades show **5.3pp lower win rate** (31.2% vs 36.5% real-price). Difference is small; price contamination may not be material for this batch.

## 8. Data Provenance

- **History file:** `/Users/alexandermoran/Desktop/solana-micro-sniper/history.json`
- **Earliest close:** 2026-03-26 20:29 UTC
- **Latest close:** 2026-04-22 23:40 UTC
- **Post-fix cutoff:** 2026-04-08 21:00 UTC

### Suspect price constants flagged

| Constant         | Value           | Meaning                                              |
| ---------------- | --------------- | ---------------------------------------------------- |
| graduation_curve | 6.162627048e-05 | Raydium init price at SOL≈$150 (pump.fun graduation) |
| legacy_reference | 1.3e-05         | Legacy reference price constant (pre-fix)            |
