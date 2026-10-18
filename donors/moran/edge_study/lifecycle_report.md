# Lifecycle Study — Edge vs Strategy
_Sample: 608 trades across 595 unique mints (skipped 0 with no candles)_

## Headline
- Our average ROI over these trades: **-9.04%**
- % of mints that eventually hit ≥2x from migration_low: **85.4%**
- % of mints that eventually hit ≥5x from migration_low: **67.6%**
- % of mints that eventually hit ≥10x from migration_low: **55.6%**

## Entry Quality — did we buy near the low, or near the top?

`entry_vs_migration_x` = our entry price ÷ migration low. 1.0 = we bought the bottom. Higher = we bought the pump.

| pct | entry_x_from_migration |
|---|---|
| p10 | 1.00x |
| p25 | 1.74x |
| median | 5.50x |
| p75 | 13.11x |
| p90 | 17.97x |

## Strategy Gap — did our losses eventually become winners?

- Losers (our ROI < -5%): **269** (44.2% of trades)
- Losers that reached ≥+50% MFE after our entry: **108** (40.1% of losers)
- Losers that reached ≥+100% MFE after our entry: **67** (24.9% of losers)
- Losers where post-exit peak ≥2x our exit price: **67** (24.9% of losers)

## MFE distribution (max % we could have closed at)

| pct | MFE |
|---|---|
| p10 | +0.0% |
| p25 | +8.3% |
| median | +42.5% |
| p75 | +123.8% |
| p90 | +325.8% |

## Ideal exit timing — gap from entry → peak

| pct | seconds to peak |
|---|---|
| p10 | 17s (0m) |
| p25 | 44s (0m) |
| median | 226s (3m) |
| p75 | 1518s (25m) |
| p90 | 4440s (74m) |

## Close reason breakdown

| close_reason | n | our avg ROI | avg MFE | % with MFE ≥+50% |
|---|---:|---:|---:|---:|
| TRAIL/SL EXIT | 224 | +1.6% | +146.0% | 57.1% |
| HARD_CAP_EXIT | 106 | -33.3% | +130.1% | 42.5% |
| QUOTEABILITY_GUARD_EXIT | 100 | -4.0% | +70.7% | 24.0% |
| RAPID DUMP EXIT | 69 | -15.6% | +102.1% | 47.8% |
| GHOST_BUY_NO_TOKENS | 29 | +0.0% | +203.6% | 62.1% |
| QUOTEABILITY_GUARD_EXIT:no_pool | 16 | -1.5% | +229.6% | 68.8% |
| STALE_MARK_TIMEOUT_EXIT | 15 | -1.5% | +14.6% | 13.3% |
| QUOTEABILITY_GUARD_EXIT:transient | 14 | -28.3% | +229.4% | 57.1% |
| MANUAL SELL 100% | 10 | -12.8% | +42.7% | 30.0% |
| STAGNATION_EXIT | 8 | -4.2% | +71.9% | 12.5% |
| VELOCITY_TRAIL_EXIT | 5 | -19.6% | +73.7% | 60.0% |
| AGE_GATE_EXIT | 4 | -4.2% | +179.9% | 25.0% |
| FAILSAFE STOP | 3 | -39.4% | +52.6% | 66.7% |
| FAILOVER_LIQUIDATION | 3 | +2.3% | +86.1% | 66.7% |
| TTL_TIMEOUT_EXIT | 2 | -9.7% | +1.3% | 0.0% |

## Verdict

**STRATEGY PROBLEM.** Tokens we pick routinely double and losers routinely recover — we're cutting winners too early and eating real drawdowns.

- `edge_ceiling` (% of mints that 2x from migration): **85.4%**
- `strategy_gap` (% of losers with MFE ≥+50% post-entry): **40.1%**
