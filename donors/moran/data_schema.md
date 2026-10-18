# Research-pipeline data schema

Pipeline order: `extract_trades.py` → `feature_builder.py` →
(`backtest_rules.py`, `walk_forward_eval.py`, `model_ranker.py`) →
`research_report.py`.

All outputs live in `./data/`. All scripts read from files, not from
`simulation_mode.py` internals. **None of the scripts touch
`simulation_mode.py` or `history.json`** — they only *read* the
history.

## `data/trades.csv` (from `extract_trades.py`)

One row per closed trade. Join logic:

- Find every `trade_close` event in `history.json`.
- Attach the nearest `execution_attempt` event where `ok=True`, same
  mint, and |ts − entry_ts| ≤ 60s.
- Attach the most-recent `signal_accept` where ts ≤ entry_ts + 5s.
- Also attach the nearest `execution_attempt` regardless of `ok`
  (within 120s) to recover `holder_pct_top5`, which only attaches to
  the first attempt.

| column | type | source event | notes |
| --- | --- | --- | --- |
| mint | str | trade_close | token mint address |
| entry_ts | float | trade_close.entry_ts | unix seconds |
| close_ts | float | trade_close.ts | unix seconds |
| hold_time_s | float | trade_close.hold_time_seconds | |
| pnl_usd | float | trade_close.pnl_usd | net P&L |
| roi_pct | float | trade_close.roi_pct | net, realized |
| mfe_pct | float | trade_close.mfe_pct | max favorable excursion |
| mae_pct | float | trade_close.mae_pct | max adverse excursion |
| close_reason | str | trade_close.close_reason | e.g. `HARD_CAP_EXIT`, `TRAIL/SL EXIT` |
| detect_to_exec_ms | float | execution_attempt | detection → tx send |
| execution_latency_ms | float | execution_attempt | signer/bundle latency |
| entry_price | float | execution_attempt.fill_price_usd / reference_price_usd / signal_accept.price_usd | first non-null |
| exit_price | float | trade_close.exit_price | |
| realized_basis_usd | float | trade_close | |
| proceeds_usd | float | trade_close | |
| candidate_age_ms | float | execution_attempt | |
| opportunity_age_ms | float | trade_close | |
| size_usd | float | execution_attempt | $ committed |
| entry_score | float | signal_accept.entry_score | sparse (92/1409 signals — field was added mid-run) |
| holder_pct_top5 | float | execution_attempt.holder_pct_top5 | sparse (attaches to failed attempts more than successful) |
| delay_s | float | signal_accept.delay_s | 502/635 trades have this |
| entry_jito_tip_lamports | int | trade_close | sparse |
| executable_mark_observations | int | trade_close | sparse |
| late_entry | bool | execution_attempt | |
| mark_price_source | str | trade_close | |
| entry_price_source | str | execution_attempt | |
| decision_breakdown_price_meta_rug_ms | float | execution_attempt.decision_breakdown_ms.price_meta_rug_ms | |
| decision_breakdown_rpc_wait_ms | float | execution_attempt.decision_breakdown_ms.rpc_wait_ms | |
| decision_breakdown_rugcheck_primary_ms | float | execution_attempt.decision_breakdown_ms.rugcheck_primary_ms | |
| source | str | execution_attempt | route (jupiter, jito, etc.) |

## `data/features.csv` (from `feature_builder.py`)

All of `trades.csv` plus:

| column | type | rule |
| --- | --- | --- |
| delay_bucket | str | `<28 / 28_30 / 30_32 / 32_36 / 36_45 / gte_45 / unknown` |
| hold_bucket | str | `<60s / 60_180s / 180_600s / 600_1800s / gte_1800s` |
| exit_bucket | str | coarse grouping: `trail_sl / take_profit / hard_cap / rapid_dump / liquidity / ghost / time_exit / other` |
| liq_bucket | str | size-proxy: `micro / rung1 / rung2 / rung3_plus` |
| roi_class | str | `loss (≤−20) / neutral (−20..10) / win (>10)` |
| rug_label | 0/1 | `roi ≤ −80` OR `close_reason` in {HARD_CAP, FAILSAFE, GHOST_BUY_NO_TOKENS} |
| win_label | 0/1 | `roi_pct > 10` |
| good_entry_bad_exit | 0/1 | `mfe_pct ≥ 30 AND roi_pct < 5` |
| pnl_usd_log, mfe_pct_log, mae_pct_log, roi_pct_log | float | signed `log1p(|x|)` |
| slow_exec | 0/1 | `detect_to_exec_ms ≥ 1500` |
| fast_exec | 0/1 | `detect_to_exec_ms < 800` |
| has_entry_score | 0/1 | non-null flag for `entry_score` |
| has_delay | 0/1 | non-null flag for `delay_s` |

## `data/rule_results.csv` (from `backtest_rules.py`)

Each row is one rule evaluated in-sample over `features.csv`:

- `rule` — rule name
- `n` — trades matched
- `avg_roi_pct`
- `win_rate` — fraction with `win_label=1`
- `rug_rate` — fraction with `rug_label=1`
- `total_pnl_usd`
- `profit_factor` — sum(wins) / sum(|losses|), `inf` if no losses

## `data/walk_forward_results.csv` (from `walk_forward_eval.py`)

For each rule × fold: `train_n`, `train_avg_roi`, `train_win_rate`,
`test_n`, `test_avg_roi`, `test_win_rate`, `test_rug_rate`, `test_pnl`,
and `sign_match` (bool — train and test share sign of avg ROI, and
test has at least 20 trades).

## `data/feature_importance.csv` + `data/model_metrics.csv` (from `model_ranker.py`)

- `feature_importance.csv`: one row per feature, `rf_importance` (random
  forest), `logreg_coef` / `logreg_abs` (logistic regression).
- `model_metrics.csv`: one row per model, `auc`, `accuracy`,
  `precision`, `recall`, `train_n`, `test_n` (time-ordered 70/30).

## `RESEARCH_REPORT.md` (from `research_report.py`)

Stitches the above into a summary with explicit separation of
*predictive-at-entry* vs *post-hoc leaky* features.

## Known leakage boundaries

At entry time the live bot knows:
- `mint`, `entry_ts`, `delay_s`, `entry_score` (when present),
  `detect_to_exec_ms`, `execution_latency_ms`, `size_usd`,
  `entry_price`, `candidate_age_ms`, `source`, `entry_price_source`,
  `holder_pct_top5` (if RugCheck ran).

It does **not** know (until after close):
- `mfe_pct`, `mae_pct`, `hold_time_s`, `roi_pct`, `pnl_usd`,
  `exit_price`, `close_reason`, `opportunity_age_ms` (final),
  `executable_mark_observations` (final), `exit_bucket`.

Anything in that second group is descriptive only; it must not be
used as input to an entry-time model intended for deployment.
