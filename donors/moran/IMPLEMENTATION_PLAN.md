# Solana Micro-Sniper — Implementation Plan
_Last updated: 2026-04-22_

---

## 2026-04-22 — Audit remediation progress

**Phases 0–6 complete. Phase 7 blocked on operator paper-trade session.**

Full task tracker: [`task.md`](task.md). One-line status:

| Phase | Scope | Status |
|-------|-------|--------|
| 0 — Safety gate | snapshot + bot-stopped verification | ✅ done |
| 1 — Critical money-path | Jito sig, token balance, slippage, tip in PnL, bundle confirm, price mismatch, sentinel guard, HARD_CAP stale guard, paper daily cap, price>0 enqueue (11 items) | ✅ done |
| 2 — Concurrency / state | `asyncio.Lock` trades, per-mint sell lock, GHOST_PENDING timeout, TP stage serialize, entry_ts restore, sell-verify vs forced-exit, runtime_state read-lock, rename off loop, partial-fill accounting (9 items) | ✅ done |
| 3 — Exit-logic correctness | TP vs trail priority, stop ladder, liquidity alignment, surge wiring, score enforcement all paths, RPC 429 backoff, Jupiter requeue backoff, queue-full telemetry (8 items) | ✅ done |
| 4 — Analysis trust | `constants.py` single source, canonical WR def, at-entry vs post-hoc features, survivorship denom, Clopper–Pearson CIs, centralized contamination, dedup suspect-price, bucket justification, MFE-cap flag, latency-aware ROI (10 items) | ✅ done |
| 5 — HUD / ops hardening | operator_commands.jsonl, HTML escape, graceful SIGTERM + .env snapshot, metrics streaming + rotation, offline log rotation, append-only ops log, base58 mint validation, WS heartbeat + stale-drop, fcntl shared-lock reads, shell hardening (10 items) | ✅ done |
| 6 — Hygiene / medium | Jupiter `priceImpactPct` gate, rent-exemption check, hourly timing reset, `threading.Lock` keypair + zeroize, Jito tip rotation, unused-import purge, `CANARY_FAILSAFE_STOP_PCT` wiring, veto-log rotation try/except, empty-df guard, HANDOFF superseded, `canary_status` deleted, env-based public path (12 items) | ✅ done |
| 7.3 — Replay + exit-lab baseline | post-`constants.py` ranking frozen in `EXIT_LAB_REPORT.md`; Fixed -6% trail #1 in every slice; live `TRAIL_STOP_PCT=0.06` already matches | ✅ done |
| 7.1 / 7.2 / 7.4 | Paper-trade 50 / wallet reconciliation ≤1% / scaling gate (2× PASS of 20, WR≥40%, HARD_CAP<15%) | 🟡 awaiting operator session |

**Next action (you):** run a paper-trade session against `.env.live_canary_research`. Confirm zero `NO_ENTRY_PRICE`, no stale HARD_CAP, no double-sell telemetry across 50 candidates. Then reconcile wallet SOL delta to reported PnL within 1% (Jito tip booking is now wired). Only then open the two-batch scaling gate.

**Artifacts produced this sprint:**
- `EXIT_LAB_REPORT.md` — exit-strategy ranking baseline (re-run and diff after any constants change)
- `REPLAY_REPORT_V2.md` — regenerated 2026-04-22 (n=328 post-fix closes, was n=262)
- `constants.py` — single source of truth for exit/win-rate thresholds
- `.env_snapshots/` — automatic `.env` snapshot before every `hard-reset.sh`
- `data/operator_commands.jsonl` — append-only operator command channel

---

## 2026-04-22 — Tier 1 strategy deployment

### Deployed today (live, PIDs 11492 sim / 11493 hud)
- `LIVE_BUY_MAX_SLIPPAGE_PCT` 0.15 → 0.05 — was guaranteeing we were exit liquidity for snipers
- `MIN_LIQUIDITY_USD` 500 → 5000 — $500 pools can't absorb a $1 exit without basis loss
- `CANARY_FAILSAFE_STOP_PCT` 0.20 → 0.25 — 0.20 was firing inside memecoin noise
- `CANARY_MAX_CONCURRENT` 5 → 2 — 5 × $1 on 8% WR was accelerating drawdown
- `feature_builder.py:97` — `rug_label` redefined to `roi ≤ -50 OR reason ∈ {HARD_CAP, FAILSAFE, GHOST_BUY, RAPID_DUMP}` (old definition never fired because FAILSAFE exits at -20%, so rugs were mislabeled)

All diffs recorded in `data/variable_changes.jsonl` via `variable_logger.py`. Profile `.env.live_canary_research` mirrored so hard-reset preserves the changes.

### Also shipped
- `variable_logger.py` (scan / log / session / tail) — every parameter change logged to `data/variable_changes.jsonl` with snapshot baseline in `data/variable_snapshot.json`
- `hard-reset.sh` now auto-calls `scan` + `session_stop` pre-restart and `scan` + `session_start` post-restart, so every session boundary is recorded including HUD `/health` + `/hud_digest` response

### Bake window
24–48h after 2026-04-22 restart. **Do not** layer Q1–Q3 below until ≥40 post-config closes to isolate Tier 1 effect. Watch reject counts on HUD — if `MIN_LIQUIDITY_USD=5000` starves signal flow, we'll see it immediately.

### Queued — post-bake (from peer-bot analysis)
Ideas from a practitioner running the same strategy class at similar scale (£2 stakes, 1,150 trades, self-reported 65.6% WR). Noted here for later; not committed.

**Q1. Proportionality rug filter.** Before firing RAPID_DUMP / soft-rug, require `liq_drop_pct >= 0.40 * price_drop_pct`. Real rugs drain liq first; whale dumps move price without draining. Currently our `RAPID_DUMP` is pure price-velocity. *Prereq:* confirm hold-time liquidity snapshots exist (not just entry). *Cost:* small, additive, flag-gated.

**Q2. 45s confirmation hold on soft-rug.** When soft-rug condition triggers (price breach but no drain), defer sell one scan cycle and re-check. Real rugs keep falling; noise bounces. *Scope:* soft path only — hard drains (liq collapse, GHOST_BUY, RugCheck flip) keep instant exit. *Cost:* small state change in exit path.

**Q3. Audit catch-up sells on fast-mover TP skips.** If a token jumps past TP0/TP1/TP2/MICRO_TP2 in one scan, verify missed partials fire before trail activates. Peer reported their ladder was marking levels "hit" without executing. *Cost:* read-only audit of existing exit code; patch only if bug found.

### Explicitly rejected
- **Peak-chase entry** (enter at h1 +150–350% if m5 positive) — different strategy class; needs h1/m5 momentum in the hot path we don't collect. Don't graft a second strategy before the base is profitable.
- **"$35k/day wallet" imitation** — one profitable on-chain wallet ≠ strategy. Survivorship bias. Ignore.

---

## 1. Current Status

| Item | Value |
|------|-------|
| Mode | LIVE |
| Profile | LIVE_CANARY_RESEARCH |
| Trade size | $1.00 |
| Concurrent | 5 |
| Daily cap | $40.00 |
| SOL balance | ~0.32 SOL |
| Post-fix real-price win rate | ~40% (140+ clean trades) |
| Entry score threshold | 65 |
| Graduation scoring | time_since_detection_s (100% coverage) |
| Jupiter fallback | Live (with FILL_SLIPPAGE_ABORT gate) |
| RPC pool | 3 endpoints (Helius + Chainstack + GetBlock) |
| getTransaction concurrency | 6 |

**Current state:** Full package deployed 2026-04-18. Awaiting first JUPITER_PRICE_VERIFIED_FALLBACK confirmation and 20 clean scored trades for next analysis batch.

---

## 2. Completed Work

### Infrastructure
- [x] PumpSwap/Raydium migration detection
- [x] Ghost buy fix, fill price tracking, sell-side propagation guard
- [x] getTransaction semaphore GET_TX_CONCURRENCY=6
- [x] disable_new_entries resets to False on startup
- [x] Daily loss cap rehydrates from history.json on restart
- [x] hard-refresh.sh fixed
- [x] **3-RPC round-robin pool**: Helius (primary/TX) + Chainstack (25 RPS) + GetBlock (20 RPS)
  - Read calls rotate; TX submission stays on Helius only
  - READ_RPC_POOL_SIZE=3 confirmed in CONFIG_SYNC
- [x] **3-WebSocket pool**: Helius + Chainstack + api.mainnet-beta.solana.com
- [x] GET_TX_CONCURRENCY logged in CONFIG_SYNC

### Entry Filters
- [x] Sentinel fix — graduation curve constant no longer used as fake ref_px
- [x] **Jupiter price v2 parallel fetch** at SENTINEL_PRICE_DETECTED — fires GET api.jup.ag/price/v2 in parallel with DexScreener. If Jupiter returns non-sentinel price → JUPITER_PRICE_VERIFIED, proceed immediately.
- [x] **Jupiter price fallback after DexScreener exhaustion** (2026-04-18) — when all DexScreener retries fail, tries Jupiter price v2 as final fallback. Only discards if both sources empty.
- [x] **Exponential retry backoff** (2026-04-18) — was flat 12s×3=36s, now 8s/16s/32s=56s total. Gives DexScreener more indexing time.
- [x] **FILL_SLIPPAGE_ABORT** (2026-04-18) — pre-send safety gate in live_canary._execute_buy. Rejects Jupiter-quoted buys where implied fill > 20% above reference price BEFORE TX signs/sends. Mandatory counterpart to Jupiter fallback.
- [x] Delayed entry holder concentration check
- [x] Jito exclusion — jitodontfront + tip accounts + prefix guard
- [x] LP lock check (95%), liquidity depth check, drift guard
- [x] **Entry scoring system** — pre-fetched during wait, zero exec-path latency:
  - **time_since_detection_s** (2026-04-18) — replaces broken pairCreatedAt (1% coverage). Computed at T+30 in _finalize_entry_score using first_ts. 100% coverage.
  - Bands: >120s=40pts, >60s=25pts, >30s=10pts, else=0pts
  - DexScreener verified at T+30 (30 pts)
  - Top wallet %: <30%=20, 30-50%=10, 50-60%=5, >60%=0 (20 pts)
  - Social links from DexScreener info.socials (10 pts)
  - **Threshold raised 50→60→65** (2026-04-18)
  - **Sentinel+fresh veto** — blocks is_sentinel=true AND age_s<30s
  - Score + breakdown saved to history.json signal_accept
- [x] **QUOTEABILITY_VALIDATION_WINDOW raised 20s→90s** (2026-04-18) — was cutting positions after 20s with no marks before pool indexed. :TRANSIENT exits were averaging -28% ROI. 90s gives pool time to appear.

### Exit Logic
- [x] Trail stop -6% from MFE peak (hardcoded trail_factor=0.94)
- [x] **Trail activation lowered 0.08→0.04 MFE** (2026-04-16)
- [x] Hard cap widened 0.15→0.20
- [x] HARD_CAP ghost close after 4 sell failures
- [x] TTL exit at 900s
- [x] **BREAKEVEN_TRIGGER_PCT env var wired** (2026-04-18) — was hardcoded 0.06, now reads from env. Currently 0.08 per .env.
- [x] **GHOST_PENDING 60s recheck** (2026-04-17) — instead of immediately finalizing ghost buys, holds position in GHOST_PENDING state for 60s and rechecks wallet. Recovers late-landing fills. 5 confirmed cases of real fills being lost before this fix.
- [x] **GHOST_PENDING loop guard** (2026-04-17) — stop/trail/HARD_CAP loops skip GHOST_PENDING positions. Prevents phantom TRAIL/SL exits on unconfirmed positions.

### Analytics Tools
- [x] batch_scorecard.py, replay_v2.py, exit_lab.py, trade_attribution.py, replay_experiment_runner.py
- [x] analyze.sh — master runner, timestamped reports
- [x] watch.sh — live log watcher (modes: default/entry/funnel/buys/all)
- [x] status.sh — funnel snapshot + verdict
- [x] Custom funnel script (python3 /tmp/funnel.py) — detailed breakdown including sentinel veto, ghost pending, confirmed late

### Key Audit Findings (2026-04-17/18)
- **Graduation scoring was at 1% coverage** — pairCreatedAt almost never returned. Fixed by switching to wall-clock time_since_detection_s.
- **Scoring computed at T=0** — age_s was always 0.0. Fixed by moving calculation to _finalize_entry_score at T+30.
- **is_sentinel=true on 49/50 scored entries** — bot was entering blind at graduation curve price. Now gated: sentinel+age<30s → veto.
- **QUOTEABILITY window was 20s** — code default is 60s but env overrode to 20s. Fixed to 90s.
- **BREAKEVEN_TRIGGER_PCT was dead config** — hardcoded 0.06 ignored env. Fixed.
- **Ghost buys silently lost 5 partial fills** — GHOST_PENDING fix recovers these.
- **HARD_CAP losses are catastrophic gaps** — tokens dump 40-86% in single tick, not gradual. Stop tightening doesn't help; entry filtering does.
- **dependency_retry has 0% HARD_CAP** but was pre-fix contaminated data, not a real signal.

---

## 3. Scaling Ladder

| Rung | Size | Status | Gate |
|------|------|--------|------|
| Canary | $0.50 | ✅ GRADUATED | |
| Rung 1 | $1.00 | ✅ GRADUATED | |
| Rung 2 | $2.50 | ❌ FAILED — back to $1.00 | Two PASS at $1.00, HARD_CAP < 15% |
| Rung 2 retry | $2.50 | 🔲 PENDING | |
| Rung 3 | $5.00 | 🔲 PENDING | Two PASS at $2.50 |
| Rung 4 | $15.00 | 🔲 PENDING | WR ≥ 38%, expectancy > +$0.10/trade |
| Rung 5 | $50.00 | 🔲 PENDING | Phase 5 infra |

---

## 4. Infrastructure Upgrade Path

### Current Stack (Free)

| Service | Role | Limit |
|---------|------|-------|
| Helius | Primary RPC, TX, DAS, WebSocket | 10 RPS |
| Chainstack | Secondary RPC, WebSocket | 25 RPS, 3M req/mo |
| GetBlock | Tertiary RPC | 20 RPS, 50K CU/day |
| Jupiter lite-api | Swap execution + price quotes | 2 RPS |
| DexScreener | Price verification, pair data | 300 req/min |

### Upgrade Triggers by Scale

| When | Upgrade | Cost | Impact |
|------|---------|------|--------|
| At $2.50 | Helius Developer | $49/mo | 50 RPS, staked TX landing |
| At $5.00 | Chainstack Yellowstone gRPC | $49/mo | 4x faster migration detection |
| At $5.00 | Smart 30s delay (polling) | Free | Enter on first real price, not fixed wait |
| At $15.00 | Helius Business | $499/mo | Enhanced WS, 1.5-2x faster events |
| At $50.00 | Helius Professional | $999/mo | LaserStream gRPC, Geyser-level |

**Never needed:** Birdeye paid, GeckoTerminal paid (10 req/min useless), DexCheck/Codex (quota math doesn't work at current volume)

**Do NOT pay for infra until 200-trade realized P&L ≥ $0.**

---

## 5. Score-to-Outcome Correlation (2026-04-18)

From 48 matched scored trades:

| Bucket | N | Avg ROI | HARD_CAP % |
|--------|---|---------|------------|
| 40-59 | 37 | -11.5% | 24% |
| 60-79 | 10 | -4.9% | 30% |
| 80+ | 1 | -23.8% | 100% |

**Key finding:** Direction is correct (higher score = better avg ROI) but ALL buckets negative. Graduation scoring was broken (age_s=0.0) during this period — real correlation analysis needs 30+ trades with working graduation scoring.

**Next gate:** 30+ scored trades with real age_s values → rerun correlation → decide if threshold should move above 65.

---

## 6. Build Queue

### Done This Session
- [x] time_since_detection_s graduation scoring (100% coverage)
- [x] Score computed at T+30 not T=0
- [x] Threshold raised to 65
- [x] Sentinel+fresh veto
- [x] QUOTEABILITY window 20→90s
- [x] BREAKEVEN_TRIGGER_PCT wired
- [x] GHOST_PENDING 60s recheck
- [x] Jupiter fallback after DexScreener exhaustion
- [x] FILL_SLIPPAGE_ABORT pre-send gate
- [x] Exponential retry backoff 8s/16s/32s

### Priority Queue

**#1 — Validate scoring signal (gate: 30+ scored trades with real age_s)**
Run correlation script after 30 trades. If 60-79 bucket significantly outperforms 40-59, raise threshold. If not, scoring may need rebuild.

**#2 — Fill slippage abort tuning**
After 20+ trades, check FILL_SLIPPAGE_ABORT fire rate. If >30% of Jupiter fallback entries abort, the 20% cap is right. If <5%, consider tightening to 15%.

**#3 — Helius Enhanced TX at T=0 (#3 from original plan)**
Now that sig is confirmed available in on_log_event via candidate_key.split("sig:",1)[1], parse migration TX directly for pool address + initial reserves. Eliminates DexScreener indexing lag entirely.
Gate: two consecutive PASS batches first.

**#4 — Smart 30s delay (polling)**
Replace fixed 30s wait with poll-every-3s up to 30s ceiling. Enter as soon as price verified.
Gate: two consecutive PASS batches.

**#5 — Q1 Proportionality rug filter** *(gate: ≥40 post-Tier-1 closes)*
See "Queued — post-bake" at top. Requires hold-time liquidity snapshots; confirm coverage before building.

**#6 — Q2 45s soft-rug confirmation hold** *(gate: ≥40 post-Tier-1 closes)*
See "Queued — post-bake" at top. Can run in parallel with Q1.

**#7 — Q3 Catch-up TP sells audit** *(anytime — read-only audit)*
See "Queued — post-bake" at top. Read existing exit code to verify missed-TP handling on fast movers. Patch only if bug found.

**NOT building yet:**
- Auto-adaptive parameter system (needs 200+ clean trades)
- ML/RL system (needs 1000+ trades minimum)
- Any paid infrastructure
- Peak-chase entry on mid-run tokens (different strategy class — see top-of-doc rejection note)

---

## 7. Key Config Values (.env.live_canary_research)

```
# Tier 1 values (2026-04-22)
MIN_LIQUIDITY_USD=5000            # was 500
LIVE_BUY_MAX_SLIPPAGE_PCT=0.05    # was 0.15
CANARY_FAILSAFE_STOP_PCT=0.25     # was 0.20
CANARY_MAX_CONCURRENT=2           # was 5

# Unchanged today
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

For the full live snapshot + diff history see `data/variable_snapshot.json` and `data/variable_changes.jsonl`.

---

## 8. Phase 5 — Data-Justified Optimization Loop

**One change at a time. Minimum 20 new trades before re-evaluating.**

**Changes made with evidence:**
1. Hard cap 0.15→0.20: experiment runner showed 0.25 optimal
2. Trail activation 0.08→0.04: 5 exit_lab runs + code audit confirmed mechanism
3. Threshold 50→60→65: score-to-ROI correlation showed 60-79 bucket outperforms 40-59
4. QUOTEABILITY 20→90s: :TRANSIENT exits averaging -28% ROI, confirmed misconfiguration
5. BREAKEVEN_TRIGGER_PCT: dead env var, now wired
6. time_since_detection_s: pairCreatedAt had 1% coverage, wall-clock has 100%
7. Score at T+30 not T=0: age_s was always 0.0, now real values
8. Jupiter fallback + FILL_SLIPPAGE_ABORT: both required together, not separately

---

## 9. Immediate Tasks

1. **Confirm JUPITER_PRICE_VERIFIED_FALLBACK firing** — `./watch.sh entry`
2. **Collect 20 clean post-restart trades** — all fixes now live
3. **Run `./analyze.sh --n 20`** — check graduation score distribution with real age_s
4. **Run correlation script** after 30+ scored trades:
   ```bash
   python3 -c "
   import json
   from collections import defaultdict
   data = json.load(open('history.json'))
   closes = {}
   scores = {}
   for e in data:
       if e.get('type') == 'trade_close':
           closes[e['mint']] = e.get('roi_pct', 0)
       if e.get('type') == 'signal_accept':
           sc = e.get('entry_score') or {}
           if isinstance(sc, dict) and sc.get('total'):
               scores[e['mint']] = sc['total']
   buckets = defaultdict(list)
   for mint, roi in closes.items():
       if mint in scores:
           s = scores[mint]
           if s < 40: buckets['0-39'].append(roi)
           elif s < 60: buckets['40-59'].append(roi)
           elif s < 80: buckets['60-79'].append(roi)
           else: buckets['80+'].append(roi)
   for bucket, rois in sorted(buckets.items()):
       avg = sum(rois)/len(rois)
       hc = sum(1 for r in rois if r < -18)
       print(f'{bucket}: n={len(rois)} avg_roi={avg:.1f}% hard_caps={hc} ({100*hc/len(rois):.0f}%)')
   "
   ```
5. **Gate for $2.50**: two consecutive PASS + HARD_CAP < 15%

---

## 10. $1,000/Day Math

| Variable | Value |
|----------|-------|
| Target trade size | $50.00 |
| Target trades/day | ~67 |
| Target win rate | 55% |
| Target avg ROI | +8% |
| Expectancy/trade | ~$15 |
| Daily P&L | ~$1,000 |

**Path:** $1.00 → $2.50 → $5.00 → $15.00 → $50.00 → $100.00

**Infrastructure cost at $50/trade:** ~$1,100/month (Helius Professional + Chainstack Yellowstone). At $1,000/day that's 1.1 days of profits per month in overhead.

**Current position:** $1.00, full scoring pipeline live with real graduation data, Jupiter fallback + FILL_SLIPPAGE_ABORT deployed, accumulating first clean scored batch.

---

## 11. Quick Commands

```bash
./hard-reset.sh live_canary_research   # full restart
./hard-refresh.sh                       # metrics reset only
./analyze.sh --n 20                     # full analysis
./status.sh                             # quick funnel check
./watch.sh entry                        # live entry scoring monitor
python3 /tmp/funnel.py                  # detailed funnel breakdown

# Arm after restart
curl -s -X POST http://localhost:8000/control \
  -H "Content-Type: application/json" \
  -d '{"action": "enable_live_tx"}' | python3 -m json.tool
curl -s -X POST http://localhost:8000/control \
  -H "Content-Type: application/json" \
  -d '{"action": "enable_trading"}' | python3 -m json.tool
```
