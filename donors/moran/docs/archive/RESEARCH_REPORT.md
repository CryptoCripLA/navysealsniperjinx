# RESEARCH REPORT — Solana micro-sniper

_Generated 2026-04-21 15:26 UTC. Source: `history.json` → `data/trades.csv` → `data/features.csv`._

## Dataset

- Closed trades: **635**
- Rugs (roi ≤ −80% OR HARD_CAP/FAILSAFE/GHOST): **162** (25.5%)
- Wins (roi > 10%): **87** (13.7%)
- Good-entry-bad-exit (mfe ≥ 30% but roi < 5%): **29**
- Total PnL: **$-71.09**, avg ROI **-8.91%**

## Rule backtest (in-sample)

| rule | n | avg_roi_pct | win_rate | rug_rate | total_pnl_usd | profit_factor |
| --- | --- | --- | --- | --- | --- | --- |
| had_mfe_gte_30 | 78 | 12.496 | 0.615 | 0.077 | 3.674 | 1.494 |
| rugs_removed_AND_delay_32_36 | 106 | 7.267 | 0.236 | 0.0 | 2.414 | 1.427 |
| had_mfe_gte_15 | 144 | 4.416 | 0.458 | 0.125 | 1.276 | 1.091 |
| delay_32_36_AND_not_slow | 92 | 3.771 | 0.163 | 0.25 | -0.463 | 0.928 |
| delay_32_36s | 138 | 1.694 | 0.181 | 0.232 | -2.499 | 0.765 |
| exit_trail_sl | 246 | 1.041 | 0.24 | 0.016 | -9.335 | 0.6 |
| delay_32_45s | 140 | 0.916 | 0.179 | 0.236 | -3.056 | 0.726 |
| rugs_removed | 473 | 0.213 | 0.178 | 0.0 | -19.321 | 0.591 |
| rung1_size | 174 | -4.417 | 0.132 | 0.345 | -11.565 | 0.364 |
| exit_liquidity_guard | 145 | -5.839 | 0.11 | 0.048 | -17.305 | 0.402 |
| slow_exec_gte_1500ms | 121 | -8.252 | 0.207 | 0.256 | -8.805 | 0.352 |
| ALL_TRADES | 635 | -8.909 | 0.137 | 0.255 | -71.092 | 0.284 |
| exec_lt_1500ms | 514 | -9.063 | 0.121 | 0.255 | -62.287 | 0.273 |
| delay_gte_45s | 58 | -9.701 | 0.172 | 0.431 | -6.334 | 0.257 |
| delay_30_32s | 304 | -13.535 | 0.132 | 0.312 | -29.786 | 0.185 |

## Walk-forward OOS stability

Rules are re-evaluated on later time folds after being computed on earlier folds. `oos_sign_agreement` = fraction of folds where train and test sign of avg ROI match.


| rule | oos_sign_agreement |
| --- | --- |
| not_slow_exec | 1.0 |
| delay_32_36s | 0.67 |
| delay_32_45s | 0.67 |
| had_mfe_gte_15 | 0.67 |
| delay_32_36_AND_not_slow | 0.33 |
| had_mfe_gte_30 | 0.33 |
| rugs_removed | 0.33 |
| rugs_removed_AND_delay_32_36 | 0.33 |

## Model AUC (time-ordered 70/30 split)

| model | train_n | test_n | auc | accuracy | precision | recall |
| --- | --- | --- | --- | --- | --- | --- |
| logreg_balanced_C0.5 | 444 | 191 | 0.806 | 0.387 | 0.165 | 0.958 |
| rf_d4_l10_n200_balanced | 444 | 191 | 0.972 | 0.948 | 0.733 | 0.917 |


> **Important:** AUC on these models is inflated by post-close features (`mfe_pct`, `hold_time_s`, `exit_bucket_*`) that the live bot does NOT have at entry time. Treat these as descriptive, not deployable.

## Feature ranking

### Predictive-at-entry (usable for entry decisions)

| feature | rf_importance | logreg_coef |
| --- | --- | --- |
| detect_to_exec_ms | 0.078 | 0.0055 |
| execution_latency_ms | 0.0651 | 0.1663 |
| entry_price | 0.0357 | -1.1009 |
| slow_exec | 0.0248 | 0.4407 |
| candidate_age_ms | 0.0196 | -0.0379 |
| delay_bucket_32_36 | 0.0089 | 0.1908 |
| delay_bucket_unknown | 0.0084 | -0.2291 |
| size_usd | 0.0078 | 0.1312 |
| has_delay | 0.007 | 0.23 |
| delay_bucket_30_32 | 0.0057 | 0.1175 |
| liq_bucket_rung3_plus | 0.0054 | 0.1247 |
| liq_bucket_micro | 0.0036 | -0.1329 |

### Post-hoc / leaky (descriptive only)

| feature | rf_importance | logreg_coef |
| --- | --- | --- |
| mfe_pct | 0.3382 | 1.4794 |
| hold_time_s | 0.1534 | -0.23 |
| mae_pct | 0.0939 | 0.0218 |
| exit_bucket_trail_sl | 0.0849 | 0.2668 |
| exit_bucket_rapid_dump | 0.0259 | -0.4654 |
| exit_bucket_hard_cap | 0.0163 | -0.2428 |
| exit_bucket_liquidity | 0.0154 | 0.2008 |
| exit_bucket_other | 0.0017 | 0.1298 |

## Bottom line

- **Entry timing matters.** The `delay_bucket=32_36` slice (n=138) averages **+1.69%** ROI vs **-8.91%** overall.
- **Rugs dominate deficit.** Removing rugs lifts avg ROI from **-8.91%** → **+0.21%**, meaning ~162/635 trades (25.5%) are responsible for most of the loss.
- **The model's 'real' AUC is likely ≤0.6** once leaky features are stripped. Logistic regression AUC 0.806 is boosted by hold_time/mfe, which the live bot does not know at entry.
- **Walk-forward verdict:** only simple delay-bucket rules survive; the narrower 'rugs_removed AND delay_32_36' rule is overfit when tested forward.
- **Actionable:** tighten delay gate to 32–36s and harden rug screening (see existing `HARD_CAP`/`RAPID_DUMP` patches). Do not rely on model-ranked post-hoc features for entry policy.
