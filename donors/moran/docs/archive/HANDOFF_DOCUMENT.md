# Solana Micro-Sniper — Handoff Document

> **HISTORICAL — superseded by [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)** (2026-04-22).
> Preserved for incident/audit context. Do not use for current state or next steps.

_Generated: 2026-04-18_
_Session: Full system audit + optimization sprint_

---

## Executive Summary

Two full-system audits (Claude Code) confirmed: **the bot has no proven edge yet.** Realized P&L is -$19 over 243 post-fix clean trades at -5.5% avg ROI. HARD_CAP exits (29% of trades, -30% avg ROI) account for ~100% of net losses.

However, 7 significant bugs were found and fixed this session that were contaminating the data and blocking entries. The bot is now in the best shape it has been. The next 20-30 trades will be the first truly clean sample with working graduation scoring.

---

## What Was Fixed This Session

### 1. Graduation scoring was broken (most impactful)
- **Bug:** `time_since_detection_s` computed at T=0 when score task kicks off. `age_s=0.0` on every entry → 0 graduation points always.
- **Fix:** Moved calculation to `_finalize_entry_score` at T+30 using `first_ts` from queue item.
- **Impact:** Graduation scoring goes from 1% to 100% coverage. Score distribution now meaningful.

### 2. DexScreener exhaustion → discard was too aggressive
- **Bug:** After 3 retries (flat 12s each = 36s), token discarded. Jupiter fallback was previously removed due to bad fills.
- **Fix:** Exponential backoff 8s/16s/32s (56s total) + Jupiter price v2 as final fallback after DexScreener exhaustion. **FILL_SLIPPAGE_ABORT gate** (20% cap pre-send) makes the fallback safe.
- **Impact:** Pass rate recovers from near-zero. Jupiter fallback previously caused -82% losses; now gated.

### 3. QUOTEABILITY_VALIDATION_WINDOW was misconfigured
- **Bug:** `.env` had 20s but code default is 60s. Bot was cutting positions after 20s with no marks — before Raydium pools finish indexing.
- **Fix:** Raised to 90s.
- **Impact:** Eliminates :TRANSIENT exits that were averaging -28% ROI.

### 4. BREAKEVEN_TRIGGER_PCT was dead config
- **Bug:** Env var `BREAKEVEN_TRIGGER_PCT=0.08` ignored. Code used hardcoded 0.06.
- **Fix:** Wired constant. Now respects env value.

### 5. Ghost buys silently lost real fills
- **Bug:** Buy verify saw wallet=0 at T+4s → immediately finalized as GHOST_BUY_NO_TOKENS. 5 confirmed cases where TX landed late and fills were lost.
- **Fix:** GHOST_PENDING state for 60s. Stop/trail/HARD_CAP loops skip GHOST_PENDING positions. After 60s recheck: if tokens arrived → clear to OPEN, else finalize.

### 6. Sentinel+fresh entries allowed through
- **Bug:** `is_sentinel=true` stored in score dict but never acted upon. Bot was entering with fake graduation curve price AND age_s=0.
- **Fix:** Sentinel+fresh veto — blocks `is_sentinel=true AND age_s < 30s`. Entries with old sentinel price (age 60s+) still pass threshold check normally.

### 7. Entry score threshold too low
- **Fix:** Raised 50→60→65. With working graduation scoring, tokens detected 30s+ ago now score above threshold. Tokens detected <30s ago with no real price correctly blocked.

---

## Current Config State

```
ENTRY_SCORE_THRESHOLD=65
MAX_ROUTE_RETRIES=3
QUOTEABILITY_VALIDATION_WINDOW_SECONDS=90
BREAKEVEN_TRIGGER_PCT=0.08
FILL_SLIPPAGE_ABORT_ENABLED=true
FILL_SLIPPAGE_ABORT_PCT=0.20
GHOST_BUY_PENDING_RECHECK_SECONDS=60
GET_TX_CONCURRENCY=6
MAX_POSITION_LOSS_PCT=0.20
DELAYED_ENTRY_SECONDS=30
DAILY_LOSS_CAP_USD=40.00
```

---

## Key Audit Findings

**From two full-system Claude Code audits (2026-04-17/18):**

1. **HARD_CAP losses are catastrophic gaps, not gradual moves.** Tokens dump 40-86% in a single 1.5s poll tick. Stop tightening doesn't help. Entry filtering is the only solution.

2. **Scoring was not predicting HARD_CAPs** — 40-59 bucket had 24% HARD_CAP rate, 60-79 had 30%, but graduation was broken (always 0pts) so these numbers are unreliable. Rerun after 30+ trades with working graduation.

3. **UNSET hot-path entries have 0% HARD_CAP but 66% QUOTEABILITY rate.** These enter immediately on detection before pool is indexed. QUOTEABILITY exits them 90s later. Not a better strategy.

4. **Slippage distribution:** Median 1.2%, p90 +30%, 43 trades >10%. The 30s delay works — 47% of fills are below reference price. But tail is brutal when entering pumped pools.

5. **Ghost reconciliation confirmed 5 partial_exit cases** — real fills that were silently lost. GHOST_PENDING fix addresses this going forward.

---

## What to Watch For

**Good signs:**
- `JUPITER_PRICE_VERIFIED_FALLBACK` appearing in logs — confirms Jupiter fallback is recovering entries DexScreener missed
- `age_s=60-150` in ENTRY_SCORE_PASS lines — confirms graduation scoring working
- `FILL_SLIPPAGE_ABORT` firing occasionally — confirms safety gate working
- HARD_CAP rate dropping below 20% over 20 trades

**Warning signs:**
- `age_s=0.0` in ENTRY_SCORE lines — scoring still broken, check _finalize_entry_score
- FILL_SLIPPAGE_ABORT firing on >50% of entries — Jupiter routing into pumped pools, may need tighter cap
- `LIVE_BUY_PENDING_NO_TOKENS` appearing — ghost pending firing (expected, but watch for `LIVE_BUY_CONFIRMED_LATE` recovery)
- Pass rate dropping below 5% — DexScreener + Jupiter both failing, investigate

---

## Next Session Priorities

1. **Run `./analyze.sh --n 20`** after 20 clean trades
2. **Check score correlation** — do 60-79 bucket trades outperform 40-59 with real age_s?
3. **Check FILL_SLIPPAGE_ABORT fire rate** — adjust cap if needed
4. **Gate for $2.50**: two consecutive PASS + HARD_CAP < 15%
5. If HARD_CAP rate does not improve: strategy problem, not engineering problem. Stop trading and rebuild entry criteria from the correlation data.

---

## Files Modified This Session

| File | Changes |
|------|---------|
| simulation_mode.py | time_since_detection_s scoring, score at T+30, sentinel veto, GHOST_PENDING guards, Jupiter fallback, exponential backoff, QUOTEABILITY window, BREAKEVEN_TRIGGER_PCT, FILL_SLIPPAGE_ABORT caller |
| live_canary.py | FILL_SLIPPAGE_ABORT constants + pre-send gate, reference_price_usd kwarg |
| models.py | ghost_pending_since field, GHOST_PENDING in position_truth_state comment |
| .env / .env.live_canary_research | FILL_SLIPPAGE_ABORT vars, QUOTEABILITY_VALIDATION_WINDOW_SECONDS=90, ENTRY_SCORE_THRESHOLD=65, MAX_ROUTE_RETRIES=3 |

---

## Transcript Reference

Full session transcript: `/mnt/transcripts/` (see journal.txt for catalog)

Previous handoff covered: RPC infrastructure, entry scoring deployment, Jupiter parallel fetch, exit lab validation, trail activation fix.
