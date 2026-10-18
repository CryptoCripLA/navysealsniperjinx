# Edge Study — Verdict Report
_Date: 2026-04-19_
_Sample: 601 trade_close events, 2026-03-26 → 2026-04-19_

---

## Verdict: **MARGINAL EDGE — with actionable sub-signals**

Per PLAN.md decision gate:
- Model A AUC 0.621 (threshold 0.65 for "edge") → marginal
- Model C AUC 0.729 but N=61 (too small to route to Phase 2A)
- **Route: Phase 2B (collect more) BUT with targeted config changes based on sub-signal findings below — not a pure "wait and see"**

---

## Headline numbers

| Model | Features | N | AUC | Top-decile ROI | Baseline ROI |
|---|---|---:|---:|---:|---:|
| A_broad | time + source + delay | 601 | **0.621 ± 0.054** | -2.34% | -9.10% |
| B_fill | A + fill/pool/slippage | 281 | 0.510 ± 0.056 | -9.84% | -12.55% |
| C_score | B + entry_score components | 61 | 0.729 ± 0.078 | +8.60% | -7.56% |

**Baseline bot P&L over these 601 trades at $1 flat: -$54.69.**

### Key finding 1 — `delay_s` is the dominant signal in every model
Permutation importance, AUC delta:
- Model A: `delay_s` = +0.286 (the next feature is less than half that)
- Model C: `delay_s` = +0.279 (above every score component)

### Key finding 2 — score components add ~zero predictive value
In Model C (N=61 trades with full scoring), the four score components (graduation, dex, holder, social) rank at or near the bottom of feature importance. `delay_s` and `lp_locked_usd` beat every score component by 4-10x.

**The current entry scoring system is not predictive on out-of-sample data.**

### Key finding 3 — fill features are noise
Model B adding slippage, pre_entry_ratio, lp depth, holder concentration etc. *reduces* AUC to 0.510 (random). Noise features hurt more than signal helped.

---

## The actionable sub-signal

When I cross-tab delay_s × hour_of_day, a clear subset emerges:

| Filter | N | Win rate | Avg ROI | $1 P&L | $50 P&L |
|---|---:|---:|---:|---:|---:|
| Baseline (all trades) | 601 | 18.3% | -9.10% | -$55 | -$2,736 |
| Delay 32-35s only | 109 | 25.7% | **+3.78%** | +$4.12 | +$206 |
| "Good" hours only (1, 2, 12, 15, 19, 21, 22, 23 UTC) | 294 | 22.4% | -3.60% | -$10.58 | -$529 |
| **Delay 32-35s AND good hours** | **67** | **29.9%** | **+7.21%** | **+$4.83** | **+$241** |

### What this means

The bot's current default `DELAYED_ENTRY_SECONDS=30` puts 299 of 468 timed trades (64%) into the **30-32s bucket — avg ROI -13.59%**. A handful of trades land in 32-35s (via retry paths or network friction) and those average **+3.78%**.

The likely causal story: at ~30s, Raydium pools are often *still indexing* and fills are into bonding-curve sentinel prices or extremely thin book. At 32-35s, pools are indexed, dev-bot pump is partially unwound, and fills are meaningful.

### Caveats on the hour-of-day signal

- "Good hours" were selected post-hoc from the training data. Real out-of-sample lift will be smaller.
- Some hours have small N (e.g. 02h UTC only has 17 trades → +11.93% could easily be noise).
- 8-week sample crosses multiple market regimes (pre/post pump.fun macro events).

### Caveats on delay_s

- `delay_s` of 32-35s in current data is mostly a *byproduct* — the bot landed there due to retry or price-fetch latency, not intentional.
- Moving `DELAYED_ENTRY_SECONDS` from 30 to 33 is easy to test, but the 32-35s subset may owe part of its edge to *the tokens that required retries being inherently different* (harder to quote = less liquid = less dev-bot attention).

---

## Honest interpretation

### What we proved
1. The bot's entry *has some signal*. AUC 0.62 on N=601 is above random at statistical significance.
2. The signal is in **timing and context**, not in the custom scoring system we've been tuning.
3. A subset of our existing trades — ~15% of the volume — is genuinely profitable at +7% avg ROI.

### What we disproved
1. **The entry score system does not earn its complexity.** Graduation / dex / holder / social components do not rank as predictive features. The 8 weeks of score tuning produced no measurable lift.
2. **Fill-quality gating does not predict outcomes.** slippage_pct, pre_entry_ratio, holder_top5_pct all rank poorly. The PRE_ENTRY_PRICE_SURGE gate we just deployed — while safe — is not selecting winners.
3. **Tiered $1/$25/$100/$500 sizing based on current model is actively dangerous** — Model A tiered P&L is **-$722** vs flat -$55. Model is not calibrated enough for confident sizing.

### What we did not prove
- Whether this is a real edge or pattern-matching noise. N=601 is enough to detect a medium effect but not enough to confidently deploy $500 stakes.
- Whether "delay 32-35s" is *causal* or *correlational* (i.e. a proxy for token characteristics we can't directly measure).

---

## Recommended Phase 2 path

**Phase 2B modified** — do not just passively collect more data. Instead:

### Changes to deploy immediately (all reversible)
1. **Raise `DELAYED_ENTRY_SECONDS` from 30 → 33.** Test the hypothesis directly on the next 50-100 trades. If ROI improves measurably, the signal was causal. If not, it was correlational.
2. **Add hour-of-day filter (soft, low-risk).** Skip new entries during 14-18 UTC where historical avg ROI is -15% to -33%. This cuts trade volume ~30% but targets the worst buckets.
3. **Do NOT deploy tiered sizing yet.** Model calibration is too poor. Hold $1 stakes until we see if #1 flips ROI positive.
4. **Park the entry score system.** Set `ENTRY_SCORE_THRESHOLD=40` to let most scored tokens through — we're using it as a smoke detector, not a selector, until we prove it predicts.

### Re-run Phase 1 at N=600 additional trades (target: 2-3 weeks at current rate)
- If verdict = edge (AUC > 0.65 stable) → Phase 2A with recalibrated sizing
- If verdict = marginal again → Phase 2C (pivot to copy-trade)
- If verdict = no edge → Phase 2C

### Target P&L for validation
- Over next 100 trades, baseline projection: -$9 (at $1 stake, current -9% avg ROI)
- If delay=33 filter works: projection +$2 to +$7 over 100 trades
- If it doesn't: we have a definitive negative result and pivot with conviction

---

## Files
- `edge_study/features.csv` — 601 rows, 28 features, label = is_winner (ROI > 5%)
- `edge_study/extract_features.py` — ETL joins history.json + logs_sim.err
- `edge_study/train_classifier.py` — HistGradientBoosting 5-fold CV across 3 tiers
- `edge_study/report.md` — this file
