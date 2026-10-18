# Catastrophic-Trade Characterization (OHLCV-only)
_Comparing 31 catastrophic trades (ROI ≤-90%) vs 55 winners (ROI >+20%). All features derived from cached 1m OHLCV._

## Close reason breakdown — catastrophic group

| close_reason | count |
|---|---:|
| HARD_CAP_EXIT | 13 |
| RAPID DUMP EXIT | 7 |
| TRAIL/SL EXIT | 4 |
| QUOTEABILITY_GUARD_EXIT | 3 |
| QUOTEABILITY_GUARD_EXIT:transient | 3 |
| MANUAL SELL 100% | 1 |

## Feature medians (catastrophic vs winners)

| feature | catastrophic | winners | separation? |
|---|---:|---:|---|
| pre_entry_ramp_x (entry÷earliest low) | 3.14 | 12.27 | **yes** |
| pre_vol_usd (last 5 pre-entry bars, avg) | 178,908,333.26 | 284,145,925.98 | **yes** |
| post_vol_usd (first 5 post-entry bars, avg) | 202,619,277.28 | 193,034,027.51 | **yes** |
| vol_drop_ratio (post÷pre) | 0.46 | 0.73 | weak |
| minutes_to_50%_drop | 26.00 | 11.00 | weak |
| minutes_to_90%_drop | 7.00 | 30.00 | weak |
| max_drop_one_bar_pct | -34.96 | -42.74 | weak |
| bars_grace_before_red | 1 | 1 | no |
| first_bar_range_pct | 32.63 | 30.28 | weak |

## Candidate filter hit rates

`catches` = % of catastrophic trades the rule would reject.  `false positives` = % of winners it would also reject. A useful filter has **catches >> false positives**.

| rule | catches catastrophic | rejects winners | net value |
|---|---:|---:|---|
| pre_entry_ramp_x ≥ 3 | 52% | 84% | -32pp — no edge |
| pre_entry_ramp_x ≥ 5 | 42% | 75% | -33pp — no edge |
| pre_entry_ramp_x ≥ 10 | 32% | 60% | -28pp — no edge |
| first_bar_range ≥ 50% | 29% | 20% | +9pp — no edge |
| first_bar_range ≥ 100% | 6% | 2% | +5pp — no edge |
| vol_drop_ratio ≤ 0.1 | 10% | 0% | +10pp — no edge |
| vol_drop_ratio ≤ 0.05 | 10% | 0% | +10pp — no edge |
| post_vol ≤ $50k | 6% | 0% | +6pp — no edge |

## Post-entry rug detector — feasibility

- Catastrophic trades hitting -50% within **1 minute**: 3/31 (10%)
- Catastrophic trades hitting -50% within **5 minutes**: 8/31 (26%)
- Winners that briefly hit -50% (false positives): 13/55

## Verdict

**No clean OHLCV-only signature.** Rugs and winners look similar in the pre-entry and early-post-entry candle features we can measure. Either the signal lives in data Birdeye free tier won't give us (dev wallet / security flags / mempool), or rugs are too fast to filter from candle data alone.
