# Sniperbot Variables Registry

_Single source of truth for every tunable parameter in the system: what it is, where it lives, what's been tested, what hasn't, and what to try next._

**Last synced:** 2026-04-22
**Dataset size at sync:** 662 closed trades
**Current classification (from decision memo):** B — marginally salvageable (20% doomed / 55% marginal / 25% viable)

**Value-source convention:**
Where multiple values exist for a parameter, we note all three:
- **`live .env`** — what the bot is actually running right now. This is ground truth.
- **`template`** — the value in `.env.canary.template`, the seed file. Often stale.
- **`code default`** — the `os.getenv("KEY", "<default>")` fallback in `simulation_mode.py`, used only if env missing.
When the three disagree, **live .env always wins at runtime**. Any analysis that assumed template values was working from a stale reference.

**How to use this file:**
- Before proposing a parameter change, check `tested_range` — if the value is inside what's already been tried, look up the study before re-running.
- Before proposing a filter, check whether the gating feature actually has coverage in the dataset (telemetry patch shipped 2026-04-21; most market-state fields have ≤ this date's data).
- When you run a new study, append the parameter + date + result here. Don't leave stale "Never tested" entries after a test lands.

**Priority legend:**
- **H** = highest expected EV, cheap to test, data already supports the question
- **M** = plausible lever, moderate cost, or requires new data accumulation
- **L** = completionist / long-tail; run only after H/M are exhausted

---

## 1. Entry timing

| param | location | current_value | tested_range | untested_range | last_tested | hypothesis | priority | cost_to_test |
|---|---|---|---|---|---|---|---|---|
| `delay_s` (primary entry gate) | signal_accept path, `simulation_mode.py` | 32–36s window | <28 / 28_30 / 30_32 / 32_36 / 36_45 / ≥45 buckets | 0–30s post-Jupiter-indexing; fine grain 33–35 vs 34–36 | 2026-04-21 (walk_forward_eval, filter_lab) | 32–36 was only forward-stable (67% sign agreement) but broke even in last 25 | H | code change only |
| Sub-30s delay with Jupiter-live gate | not implemented | n/a | never | 0–30s conditional on `route_available_at_signal = True` | never | Jupiter indexes faster than when 32s floor was chosen; pre-32s window may have reopened | **H** | new code path + runtime data |
| Post-45s delay | signal_accept | — | ≥45 bucket tested briefly, losing | 45–90s for post-dump rebound entries | 2026-04-21 (backtest_rules) | Probably a different strategy (rebound), not micro-sniper | L | code change |
| `detect_to_exec_ms` threshold | filter_lab candidate | none (observed only) | ≥1500ms flagged as slow_exec | 800 / 1200 / 2000 thresholds | 2026-04-21 (filter_lab) | Slow exec ≈ stale quote at fill | M | filter toggle |
| `execution_latency_ms` threshold | filter_lab candidate | none (observed only) | <3000 explored | 1500 / 5000 / 8000 | 2026-04-21 (filter_lab) | Tail runs to 13s; above 3s unlikely profitable | M | filter toggle |
| Re-entry cooldown same mint | not implemented | 0s (unrestricted) | never | 60 / 300 / 900s | never | Avoid compounding losses on same failing launch | M | small code change |
| Cool-down after rug cluster | not implemented | n/a | never | pause N seconds after any ≤ −50% close | never | Rugs cluster; post-rug window may have elevated rug density | M | small code change |
| Hour-of-day filter | not implemented | all hours | never | UTC hour buckets; day vs night split | never | 662 trades across 25 days — enough for 4-hour bucket split | M | analysis only |
| Day-of-week filter | not implemented | all days | never | weekday vs weekend | never | Possible regime difference | L | analysis only |

## 2. Entry gates / filters

| param | location | current_value | tested_range | untested_range | last_tested | hypothesis | priority | cost_to_test |
|---|---|---|---|---|---|---|---|---|
| `MIN_LIQUIDITY_USD` | `.env` (live) | **5000** (live .env, raised from 500 on 2026-04-22); template = 2000; code default = 25000 | 500 (historical) | 10000 / 25000 | 2026-04-22 (Tier 1 applied, data accumulating) | $500 pools cannot absorb a $1 exit; raising floor 10× to 5000 to cut rug-tail | **H** | config change + new data |
| Market cap floor | not implemented | 0 | never | $50K / $100K / $500K | never | Sub-$50K MC is mostly insider scams; filter may cut rug rate materially | **H** | telemetry now captures `market_cap_usd` (2026-04-21+) |
| Market cap ceiling | not implemented | ∞ | never | $5M / $10M / $25M | never | Above ceiling = already pumped; you're late | M | same telemetry |
| FDV floor / ceiling | not implemented | none | never | mirror of MC | never | FDV vs MC ratio signals unlocked supply | L | same telemetry |
| Pair age floor | implicit via 32s delay | ≥32s effectively | delay-bucketed | explicit age gate decoupled from delay | never | `pair_created_at_ms` now captured — can test as independent variable | M | new filter |
| Pair age ceiling | not implemented | ∞ | never | 300 / 600 / 1800s | never | If pair is >10min old and we're just now seeing it, missed data path | M | same telemetry |
| Entry price floor | not implemented | $0 | never | $0.0001 / $0.001 / $0.01 | never | Sub-cent tokens dominate the rug tail; crude but cheap proxy | **H** | analysis of existing data |
| Entry price ceiling | not implemented | ∞ | never | $0.10 / $1.00 | never | Unclear EV; upper tokens may already be saturated | L | analysis |
| m5 volume floor | not implemented | 0 | never | $5K / $20K / $100K | never | Low-volume pair = illiquid, can't exit | M | new data required |
| h1 volume floor | not implemented | 0 | never | $50K / $250K | never | Coarser version of above | L | new data |
| Buy/sell m5 imbalance | not implemented | none | never | `buys > sells * 1.5` / `buys > sells * 2` | never | Early structural buy pressure proxy | M | new data |
| m5 price momentum ceiling | not implemented | none | never | skip if `price_change_m5 > +80%` | never | Avoid buying the local top | M | new data |
| m5 price momentum floor | not implemented | none | never | skip if `price_change_m5 < −40%` | never | Avoid knife-catching | M | new data |
| Holder top5 concentration | implemented as telemetry only | observed | never gated | skip if `holder_pct_top5 > 50%` | never (sparse coverage historically) | Concentration proxy for rug risk | **H** once coverage fills |
| Holder top10 concentration | now null-captured | none | never | skip if top10 > 70% | never | Same hypothesis, softer | M | new data |
| Dev holder % | not captured at runtime | null | never | skip if dev holds > 15% | never | Would require extra API — parked | L | new data source |
| Unique holder count | not captured | null | never | floor at 30 / 50 | never | Same | L | new data source |
| Entry score (`entry_score_dict`) gate | `simulation_mode.py` | score pass required | implicit gate | score threshold tuning | sparse (92/1409 signals have score field) | Field was added mid-run; coverage inadequate | M | accumulate data |
| Pre-entry structure shape | now captured as heuristic | observed, not gated | never | require `pre_entry_structure = pullback` | never | Spikes may have worse gain retention | M | new data |

## 3. Exit mechanics (the known leak)

| param | location | current_value | tested_range | untested_range | last_tested | hypothesis | priority | cost_to_test |
|---|---|---|---|---|---|---|---|---|
| `DEFAULT_DOWNSIDE_STOP_PCT` | `.env` (live) | **0.10** (live .env); template = 0.30; code default = 0.12 | only 0.10 run live | 0.15 / 0.20 / 0.30 | never | 10% stop is aggressive — likely contributing to the trail-whipsaw problem | **H** | config change |
| `ABSOLUTE_FAILSAFE_STOP_PCT` | `.env` (live) | **0.20** (live .env); template = 0.50; code default = 0.20 | only 0.20 run live | 0.30 / 0.40 / 0.50 | never | At 0.20 FAILSAFE fires at -20% — still true for ABSOLUTE. See `CANARY_FAILSAFE_STOP_PCT` row for the actual stop raised on 2026-04-22. | **H** | config change |
| `CANARY_FAILSAFE_STOP_PCT` | `.env` (live) | **0.25** (live .env, raised from 0.20 on 2026-04-22) | 0.20 (historical) | 0.30 / 0.35 | 2026-04-22 (Tier 1 applied) | 0.20 was firing inside memecoin noise; widen to 0.25 to let winners breathe while still catching rugs | **H** | config change |
| Trail initial band width | exit logic in `simulation_mode.py` | current tight setting | only current | 8% / 12% / 15% / 20% | never | Median gain retention −28% says trail is clipping winners | **H** | code change |
| Breakeven arm MFE threshold | exit logic | not explicitly armed | only current | arm @ +10% / +15% / +20% MFE | never | Directly attacks 7/25 MFE-gap cases | **H** | code change |
| Trail decay / step size | exit logic | current | only current | slower decay curves | never | Winners run in memecoins; fast decay = early cutoff | M | code change |
| Hard time cap | exit logic | HARD_CAP_EXIT exists | only current | 60s / 120s / 300s / 600s | indirectly: TIME_EXIT_120S is the only positive close reason | **Timer-out trades beat trail-out trades** — possibly the biggest signal in the dataset | **H** | code change + parameter sweep |
| TP ladder rung 1 | exit logic | current | only current | +15% / +25% / +40% | never | Partial lock at low MFE may flip retention positive | M | code change |
| TP ladder rung 2 | exit logic | current | only current | +50% / +80% / +120% | never | Higher rung for runners | M | code change |
| TP ladder qty at rung | exit logic | current | only current | 25% / 50% / 75% | never | Aggressive partial at rung 1 = lock retention | M | code change |
| MFE-ceiling auto-exit | not implemented | none | never | auto-close at +100% / +150% MFE | never | Lock gains on parabolic winners before reversal | M | code change |
| Mark-staleness exit | telemetry only | observed, not gated | never | auto-close if `stale_mark_ratio > 0.7` | never (new telemetry) | Positions with no executable mark are ghosts | M | new code + data |
| Exit on m5 price reversal | not implemented | none | never | close if `price_change_m5 < −20%` intra-hold | never | Trend-follow exit complement | L | new code |

## 4. Sizing / portfolio

| param | location | current_value | tested_range | untested_range | last_tested | hypothesis | priority | cost_to_test |
|---|---|---|---|---|---|---|---|---|
| `CANARY_TRADE_SIZE_USD` | `.env` (live) | **1.00** (live .env); template = 5; code default = 5 | only 1 run live | 2 / 5 / 10 / 25 | never | Currently 5× smaller than assumed — MFE-gap absolute-dollar math needs recompute | L (until PF gate passes) | config change |
| `CANARY_MAX_CONCURRENT` | `.env` (live) | **2** (live .env, reduced from 5 on 2026-04-22); template = 2; code default = 2 | 5 (historical) | 1 / 3 | 2026-04-22 (Tier 1 applied) | 5 concurrent on 8% WR accelerated drawdown; cut to 2 to force exit discipline | **H** | config change |
| `CANARY_MAX_DAILY_LOSS_USD` | `.env` | 25.00 | only 25 live | 15 / 50 / 100 | never | Kill-switch threshold — tune after sizing changes | L | config change |
| Kelly / performance-based sizing | not implemented | fixed | never | scale size by recent PF | never | Compound strength, starve weakness | L | new code |
| Per-hour trade cap | not implemented | none | never | 3 / 5 / 10 per hour | never | Avoid overtrading in quiet windows | L | new code |
| Daily trade cap | not implemented | none | never | 20 / 40 / 80 | never | Complement to daily loss cap | L | new code |

## 5. Rug / risk gates

| param | location | current_value | tested_range | untested_range | last_tested | hypothesis | priority | cost_to_test |
|---|---|---|---|---|---|---|---|---|
| RugCheck primary gate | `simulation_mode.py` | pass required | binary | tunable severity threshold | indirectly in replay | Already blocking known-bad — further tuning marginal | L | code change |
| Solsniffer gate | `simulation_mode.py` | implemented | binary | score threshold | never | Same | L | code change |
| `rug_label` boundary | `feature_builder.py:97` | ROI ≤ −50 OR HARD_CAP/FAILSAFE/GHOST/RAPID_DUMP (updated 2026-04-22) | old: ROI ≤ −80 OR HARD_CAP/FAILSAFE/GHOST | −30 / −40 / −60 boundaries; with/without RAPID_DUMP | 2026-04-22 (redefinition applied) | Prior definition never fired: FAILSAFE exits at -20% so "rug" ROI never reached -80%. Lowered to -50 and added RAPID_DUMP so downstream ML can actually learn the class. | **H** | analysis |
| Post-rug pause | not implemented | 0s | never | 60 / 300 / 900s cooldown | never | Rugs cluster in time | M | small code change |
| LP lock gate | `simulation_mode.py` | implemented | binary | required lock % threshold | never | Unlocked LP is the classic rug vector | M | config change |
| Dev sells seen pre-entry | not captured | null | never | any dev sell = skip | never | Requires tracking dev wallet tx pre-entry | L | new data source |

## 6. Execution quality

| param | location | current_value | tested_range | untested_range | last_tested | hypothesis | priority | cost_to_test |
|---|---|---|---|---|---|---|---|---|
| `LIVE_BUY_MAX_SLIPPAGE_PCT` | `.env` (live) | **0.05** (live .env, reduced from 0.15 on 2026-04-22); template = 0.03; code default = 0.03 | 0.15 (historical) | 0.03 / 0.08 / 0.10 | 2026-04-22 (Tier 1 applied) | 15% guaranteed we were exit liquidity for snipers; 5% aligns with template and cuts basis leak | **H** | config change |
| `JITO_TIP_LAMPORTS` | `.env` (live) | **200000** (live .env); template = 20000; code default = 20000 | only 200k run live | 20k / 50k / 100k / 500k | never | At 200k lamports = ~$0.04/trade overhead, likely fine; but 10× template suggests past tuning | L | config change |
| `JITO_REGION` | `.env` | ny | only ny | ams / fra / tokyo | never | Unlikely to change EV materially from US | L | config change |
| RPC pool composition | live_canary.py config | current (Helius/Chainstack) | current | adding LaserStream | never | Speed floor likely saturated | L | infra change |
| Bundle vs direct submission | submission path | bundle via Jito | only bundle | direct | never | Direct may be faster on quiet blocks | L | code change |

## 7. Structure / shape (telemetry-only, not gated)

All of these are captured as of the 2026-04-21 telemetry patch but not yet used as filters. Coverage fills starting 2026-04-22.

| param | location | current_value | tested_range | untested_range | last_tested | hypothesis | priority | cost_to_test |
|---|---|---|---|---|---|---|---|---|
| `pre_entry_structure` | telemetry | observed | never gated | require `pullback` only | never | Pullback entries may have better gain retention | M | analysis once n≥50 |
| `local_pullback_depth_pct` | telemetry | observed | never gated | filter on depth | never | Deeper pullbacks = better R:R | L | same |
| `local_spike_height_pct` | telemetry | observed | never gated | skip high spikes | never | Spike buyers are usually wrong | L | same |

## 8. Meta / regime

| param | location | current_value | tested_range | untested_range | last_tested | hypothesis | priority | cost_to_test |
|---|---|---|---|---|---|---|---|---|
| Regime pause after N consecutive losses | not implemented | none | never | 3 / 5 / 8 consecutive | never | Protects against locally bad windows | M | small code change |
| Weekly PF-based switch to paper | not implemented | none | never | auto-pause when 7-day PF < 0.6 | never | Preserves capital when edge weakens | M | monitor + toggle |
| Telemetry schema version tag | not implemented | none | never | tag every signal_accept with schema_ver | never | Track when gates were added/changed | L | one-line change |
| Canary vs scaled live flag | `EXECUTION_MODE` | paper / live | current | canary_1 / canary_2 / prod tiers | never | Staged size-up ladder | L | config work |

---

## Top-priority experiments (sorted by EV × cheapness)

1. **Hard time cap sweep (exit).** TIME_EXIT_120S is the only positive close bucket. Test hard-cap at 60 / 120 / 300s as a default exit with trail only activating above +15% MFE. Evidence already exists; this is a reframing of your whole exit stack.
2. **Breakeven arm + wider trail (exit).** Arm @ +15% MFE, widen initial trail to 12%. Directly targets −28% median gain retention. 40-trade cohort is enough per decision memo.
3. **Sub-30s delay window reopen (entry).** Gate on `route_available_at_signal = True`; test 20–30s. Jupiter indexes faster now than when 32s floor was chosen.
4. **Market cap floor $50K / $100K (entry).** Cheapest possible filter. Targets the tail of rug-dominant sub-cent launches.
5. **Entry price floor $0.001 / $0.01 (entry).** Cruder version of (4); test either-or, not both.
6. **Concurrency cap at 1 (portfolio).** Tests whether exit discipline degrades with two positions. One config line; one cohort of data.
7. **Liquidity threshold sweep (entry).** 1000 / 3000 / 5000 as alternates to 2000. Reshapes the selection universe; non-trivial to isolate from (4)(5).

## Blocked pending data

These are plausible but cannot be tested until the 2026-04-21 telemetry patch accumulates n≥50:
- Holder top5 gate
- m5 volume floor
- Buy/sell imbalance gate
- m5 momentum floor/ceiling
- Pre-entry structure filter
- Pair age ceiling

## Explicitly off-table

Things previously discussed and decided not to pursue, with reasons:
- **New ML model from current data.** AUCs are leakage-inflated. No non-leaky feature ranks above noise. Re-examine when (blocked) data accumulates.
- **Birdeye-based entry features in live path.** Research-only; no HTTP in hot path.
- **Holder graph / dev wallet tracking at entry.** Requires new API + adds latency. Park unless edge is confirmed elsewhere.
- **Pivoting away from pump.fun migrations.** Would waste the 662-trade dataset and shipped telemetry. Revisit only if doomed-probability exceeds 50%.

---

## Change log

| date | change | by |
|---|---|---|
| 2026-04-22 | Initial registry created | quant synth |
| 2026-04-22 | Corrected 7 parameter rows to use live `.env` values (not template). Added value-source convention section. Promoted liquidity/stop/slippage/concurrency rows to **H** priority based on actual deltas from assumed values. | quant synth (post-verification) |
| 2026-04-22 | **Tier 1 strategy fix deployed.** `.env` + `.env.live_canary_research` updated: `MIN_LIQUIDITY_USD` 500→5000, `LIVE_BUY_MAX_SLIPPAGE_PCT` 0.15→0.05, `CANARY_FAILSAFE_STOP_PCT` 0.20→0.25, `CANARY_MAX_CONCURRENT` 5→2. `feature_builder.py:97` `rug_label` redefined (ROI ≤ −50 OR reason includes RAPID_DUMP). All diffs recorded in `data/variable_changes.jsonl` via `variable_logger.py`. Bot restarted via `hard-reset.sh`. | operator + claude |
| 2026-04-22 | Added `variable_logger.py` — tracks `.env` diffs + code constants into `data/variable_changes.jsonl` (append-only JSONL), `data/variable_snapshot.json` (latest state baseline). Commands: `scan`, `log KEY VALUE`, `tail N`, `init`. | claude |
