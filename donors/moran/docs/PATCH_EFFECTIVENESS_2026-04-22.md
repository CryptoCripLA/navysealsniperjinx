# Post-Patch Effectiveness Report

_Generated: 2026-04-22 20:18 EDT_
_Full log: [`logs/analysis_2026-04-22_201856.txt`](../logs/analysis_2026-04-22_201856.txt)_

Scope: all closes written to `history.json` after the 2026-04-22 patch cycle (Phases 0–6 + Tier 1 strategy deployment). Bot was **live** (`LIVE_CANARY_RESEARCH`, $1/trade, max concurrent 2) during this window.

## Verdict: **FAIL** — patches did not unblock the scaling gate

Paper-trade validation (P7.1) has not yet been run — the 20 closes below are the first live-mode sample after the Tier 1 config changes landed on 2026-04-22.

## Headline numbers

| Slice | N | WR | Avg ROI | Total P&L | HARD_CAP rate | Verdict gate (WR≥35%, HARD_CAP<15%) |
|-------|---|-----|---------|-----------|---------------|-----|
| **Last 20 (today, post-Tier-1)** | 20 | 20.0% (CI95 5.7–43.7%) | -3.0% | **-$0.23** | 4/20 = 20% | ❌ FAIL both |
| Post-fix (all clean since 2026-04-08) | 334 | 28.7% | -5.2% | -$25.24 | 83/334 = 24.9% | ❌ FAIL both |
| Real-price subset (post-fix) | 241 | 31.1% | -5.3% | -$20.24 | — | ❌ FAIL |

Small-sample caveat: N=20 means wide CI — the true WR could plausibly sit anywhere in [5.7%, 43.7%]. The 334-trade post-fix sample is the more reliable read.

## Where the money is still leaking

From `batch_scorecard.py` (last 20) and `replay_v2.py` (post-fix 334):

| Source | Count | Total | Notes |
|---|---|---|---|
| HARD_CAP_EXIT (last 20) | 4 | -$0.34 | avg ROI -25%; worst -62.4% |
| HARD_CAP_EXIT (post-fix 334) | 83 | **-$23.13** | avg ROI -30.4% — **still the dominant drain** |
| TIME_EXIT_120S (last 20) | 3 | -$0.28 | avg -6.4% |
| RAPID DUMP EXIT (post-fix) | 37 | -$2.41 | |
| TRAIL/SL EXIT (post-fix) | 192 | +$0.81 | 57.5% of closes, slightly positive on average |

**HARD_CAP continues to account for ~100% of net losses.** This is the same finding as the 2026-04-18 audit. Patches addressed mechanics (stale-mark guard, score threshold, sentinel veto) but did not change the underlying fact: entries are still landing in tokens that dump 30%+ in one tick before the trail can engage.

## What the entry score is telling us

From `trade_attribution.py` last 20 (18/20 scored):

- **All 18 scored trades fell in the 40–59 bucket.** Zero scored 60+.
- Avg score: 44.2
- Bucket breakdown: `0-39: 0 | 40-59: 18 | 60-79: 0 | 80+: 0`
- Component coverage:
  - `graduation_pts`: avg 10.0 — **0/18 got the full 40** (tokens being detected too close to migration to earn age points)
  - `dex_pts`: 30.0 — 18/18 at full (DexScreener verification working)
  - `holder_pts`: avg 1.4 — **0/18 got full 20** (top-5 concentration is consistently high)
  - `social_pts`: 2.8 — 5/18 got full credit
  - `getAsset` errors: 5/18 — RPC returns missing metadata, score defaults to neutral

**Implication:** the current signal set is not spreading entries across the bucket range. The threshold at 65 is not filtering anything because nothing is crossing 60. Either the scoring doesn't discriminate well, or the candidate pool is genuinely all mid-quality.

## Exit-lab counterfactual (last 20)

Current live config is `TRAIL_STOP_PCT=0.06`. Best counterfactual ranking on this 20-trade sample:

| Rank | Strategy | Win% | Avg ROI | ~P&L | vs base |
|------|----------|------|---------|------|---------|
| 1 | **Partial TP@+20% + -6% trail** | 50.0% | +4.0% | $+0.78 | +$1.43 |
| 2 | Fixed -6% trail (current) | 50.0% | +3.9% | $+0.75 | +$1.40 |
| 3 | Tiered trail (20/50% MFE) | 40.0% | +1.8% | $+0.35 | +$1.00 |

**Finding:** adding a Partial TP@+20% would have improved this 20-trade sample by +$0.03 over the current Fixed -6% trail. That's below signal threshold on n=20 — not worth changing exit logic based on this.

The MFE≥20% subset (n=6 in this window) shows +$1.10 vs +$1.07 for Partial vs Fixed — again, too close to act on.

## Pre-entry rejection telemetry (from logs)

Log-grep counts over the recent window:
- SURGE rejections (fill > 1.3× reference): 118
- DUMP rejections (fill < 0.4× reference): 48
- `PRICE_UNVERIFIED_SKIP` today: 1
- Recent `DELAYED_ENTRY_HOLDER_VETO` hits: 5 examples, all 77–100% top-5 concentration

Veto pipeline is firing — this is evidence the entry filters are active, not that they're selective enough.

## What the patches DID verify

From log scan, none of these regressions appeared post-patch:
- ✅ No `NameError` from `get_token_balance` (P1.2)
- ✅ No `NO_ENTRY_PRICE` tag firings in the last 20 (P1.8 sentinel guard working)
- ✅ No double-sell telemetry
- ✅ `EXEC_OK` on 20/20 trades — zero swap failures / ghost positions in this window
- ✅ Graduation scoring at 100% coverage with real `age_s` values (time_since_detection_s working)

So the Phase 1–3 mechanical fixes are holding. The problem is **strategy**, not **engineering**.

## Recommendation

1. **Switch to paper mode before spending more SOL.** The 334-trade post-fix sample is a -$25 drawdown in research. No patch has changed the underlying -5% avg ROI. Continuing live canary just adds cost.
2. **Raise `ENTRY_SCORE_THRESHOLD` above 60** so zero trades fire until a 60+ candidate appears. If none appear for a week, the signal set needs rebuilding — don't paper your way to bankruptcy waiting for a 65 that never shows up.
3. **The `graduation_pts = 0/18 at full` signal is the most actionable finding.** Either the scoring window is too short (all tokens detected at age < 120s) or the weighting is wrong. Investigate before changing any other config.
4. **Do not add Partial TP@+20%** based on the +$0.03 exit-lab delta — that's noise on n=20.

## Artifacts

- `logs/analysis_2026-04-22_201856.txt` — full output (batch_scorecard, trade_attribution, exit_lab, replay_v2, replay_experiment_runner)
- `REPLAY_REPORT_V2.md` — regenerated, now 334 post-fix closes
- `EXIT_LAB_REPORT.md` — post-`constants.py` ranking baseline (unchanged)
- `history.json` — latest close 2026-04-22 23:40 UTC
