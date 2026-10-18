# Sniper Bot — 8-Week Edge Audit Findings

_Date: 2026-04-19 • 608 closed trades over 2026-03-26 → 2026-04-19 • Total P&L: -$69.02_

This document consolidates everything the edge study has proven, disproven, and left open. It is the artifact that should inform all future decisions about whether to keep tuning sniper vs fully commit to copy-trades-bot.

---

## TL;DR

- Sniper has **real but tiny edge** on the small subset of tokens that survive — 55 trades (9%) produced +$21.73 at avg +44% ROI.
- Sniper is killed by **31 catastrophic trades (5%)** that lost ~$3 each = $93 of damage — more than the total deficit.
- From **OHLCV data alone, rugs and winners are statistically indistinguishable**. No price-action filter beats its own false-positive rate.
- Profitable public sniper operators have **data sources we don't** (paid security APIs, dev-wallet reputation, honeypot simulators, insider feeds).
- At current $1-3 stakes, **no paid data service pays for itself** — the total addressable damage is ~$46/mo vs $49-299/mo service costs.
- Conclusion: **sniper is capped at negligible P&L unless stakes scale**, and stakes can't scale without confidence that requires data we'd have to pay for — a chicken-and-egg we can't break without a first data purchase or a pivot.

---

## What we investigated

### 1. Feature-based ML on entry quality (`extract_features.py`, `train_classifier.py`)
- N=601 trades, 28 features, is_winner = ROI > +5%
- Best AUC: Model A = 0.621 (marginal edge)
- Dominant feature: `delay_s` at importance +0.286 — entries landing at 32-35s beat 30s bucket by ~17pp ROI
- Entry score components (graduation, dex, holder, social) rank at or near bottom
- **Result: marginal predictive signal exists in timing, none in our scoring system**

### 2. Lifecycle reconstruction (`lifecycle_extract.py`, `lifecycle_analyze.py`)
- Fetched 1m OHLCV for all 595 unique mints from Birdeye (free tier)
- Captured ~30min pre-entry → 4h post-exit per mint
- Found:
  - **85.4% of mints we bought eventually 2x'd from earliest captured low** — edge ceiling exists
  - **Median entry = 5.5x above migration low** — we buy late into pumps
  - **47.8% of our losses hit ≥+50% MFE after entry** — trail/SL kicks out future winners
  - **TRAIL/SL EXIT bucket (224 trades)**: avg our ROI +1.6%, avg MFE **+146%**
- Caveat that emerged later: Birdeye 1m candles aggregate away the sub-minute spikes where real winners printed. A trade with our real ROI +160% had candle MFE of only +32% — Birdeye cannot see intra-minute volatility on thin pump.fun tokens

### 3. Backtest grid (`lifecycle_backtest.py`)
- 15 entry rules × 18 exit rules = 270 combinations
- Assumptions: $1 stake, 3% round-trip slippage (later proven too pessimistic — real fees are ~0.22%), 4h hold cap
- **Best combo**: `pullback ≤0.8x × tp+30%/sl-8%` → +$2.30 over 248 trades
- **Baseline (current bot approx)**: -$10.76
- **Only 3 of 270 strategies profitable** — and none by a material margin
- Best per-trade yield: $0.009 — won't scale
- Caveat: backtest analyzed at 1m resolution is incompatible with a bot operating at sub-second resolution on thin tokens

### 4. Catastrophic-trade characterization (`catastrophic_analysis.py`)
- Compared 31 rugs vs 55 winners on OHLCV-derived features
- Key findings:
  - Rugs have **LESS** pre-entry pump than winners (3.14x vs 12.27x median)
  - **Every candidate pre-trade filter has negative net value** — catches more winners than rugs
  - Only 26% of rugs hit -50% within 5 min; 24% of winners also dip that far — post-entry rug detector via price alone has ~0 separation
- Birdeye free tier returned empty for `token_security` / `token_creation_info` (401 / 429 errors) — we cannot evaluate dev-wallet or security signals without paying
- **Verdict**: rugs and winners are statistically indistinguishable from free-tier price/volume data

---

## What this means structurally

### Why sniper cannot work at our data level
The 31 catastrophics are not "tokens that pumped and crashed" — they are **tokens that never gained organic interest** and quietly bled to zero, passing our liquidity/rugcheck gates because the rug mechanism wasn't technical (freeze authority, transfer tax) but behavioral (dev wallet dump, no real buyers). The discriminating signal for these lives in:
- **Dev wallet history** — has this address deployed rug tokens before? (available via paid Birdeye / Dune / Flipside)
- **Pre-migration holder distribution** — is this controlled by 5 wallets? (available via Helius DAS or paid Birdeye)
- **Early buyer quality** — are known smart wallets buying in the first minute? (requires real-time wallet tagging — paid services)
- **Social signal** — Telegram/Twitter mentions (scrape or paid feed)

None of these are in our current pipeline.

### Why public sniper bots exist and appear profitable
Operators who sell public bots or claim consistent profits have one or more of:
1. **Block-0 execution** via Jito bundles (sub-second detection) — costs infrastructure and priority fees
2. **Private alpha** Telegram feeds listing known bad deployers — often paid group memberships
3. **Paid Birdeye/RugCheck/dexscreener Pro** for pre-trade security signals
4. **Dev wallet databases** (Dune dashboards, Solscan Pro, SolanaFM) — some paid, some built from scraping
5. **Cherry-picked performance claims** — bots that publish "profits" often selectively report

We have #1 partially (15-30s detection latency — mid-pack), #2-#4 not at all, #5 irrelevant.

---

## Paid-service ROI analysis

### What each service unlocks and costs

| service | monthly cost | what we get | plausible rug-detection lift |
|---|---:|---|---|
| **Birdeye Starter** | $99 | `token_security`, `token_creation_info`, dev wallet endpoints, 60 RPS | 30-50% rug catch via dev + security flags |
| **Birdeye Business** | $299 | + full tx history, deeper analytics | marginal additional lift |
| **RugCheck API** | ~$49-99 | honeypot simulation, transfer-fee/freeze detection, dev scoring | 20-40% rug catch (mostly honeypot-class) |
| **Helius Developer** | $49 | faster RPC, Enhanced WS, webhooks, DAS with creator info | reduces latency 15-30s → 2-5s (different lever — speed, not filter) |
| **Helius Business** | $499 | dedicated nodes, higher priority | marginal over Developer for our volume |
| **Dune / Flipside** | $0 tier | build our own dev wallet reputation queries | 15-25% rug catch (dev repeat-offender) |
| **QuickNode Premium** | $49-$299 | RPC speedups, some indexed endpoints | speed only, not rug signal |
| **Paid Telegram alpha** | $50-500 | insider migration calls | highly variable, not quantifiable |

### Break-even at current $1-3 stakes

Current rug damage: **31 trades × ~$3 = $93 over 8 weeks = ~$46/month ceiling**.

| service | cost/mo | max possible savings (100% catch) | realistic savings (50% catch) | net monthly |
|---|---:|---:|---:|---:|
| Birdeye Starter | $99 | $46 | $23 | **-$76** |
| RugCheck API | $49 | $46 | $23 | **-$26** |
| Helius Developer | $49 | $0 (doesn't filter rugs, only improves speed) | — | **-$49** |
| Dune free tier | $0 | $46 | $12 | **+$12** |

**At current stakes, only free services make sense.** Any paid subscription is burning more than the rug damage it avoids.

### Break-even at $25 stakes (target Phase 2 scale)

Damage scales linearly: **31 trades × ~$25 × 8 wks = $775/mo**.

| service | cost/mo | realistic savings (50% catch) | net monthly |
|---|---:|---:|---:|
| Birdeye Starter | $99 | $388 | **+$289** |
| RugCheck API | $49 | $388 | **+$339** |
| Birdeye + RugCheck (combined) | $148 | $465 (assuming some overlap) | **+$317** |

**At $25 stakes, paid services clearly pay for themselves** — if catch rate holds.

### Break-even at $50 stakes (Phase 3 scale)

Damage scales to **~$1,550/mo** catastrophic potential. All three services combined cost ~$150/mo with potential savings of $800-1000/mo.

### The actual gate

Paid services are only worth buying if:
1. We have **confidence to scale to ≥$25 stakes**, AND
2. The edge on winners (+$21.73 at $1-3 stakes → ~$200/mo at $25 stakes) is preserved when paid filters are added, AND
3. The rug catch rate holds in live conditions (not just backtest)

**None of these conditions are currently established.** To establish them we would need to pay for data first and run a controlled canary — pay-to-learn with no guarantee.

---

## Recommendation

### Don't buy paid services yet
The math only works at stake sizes we don't have confidence to deploy. Buying Birdeye Starter at $1 stakes is pure research spend.

### The copy-trades-bot path sidesteps this entirely
Copy-trading a known-profitable wallet inherits their filtering — they have done the dev/security/social work by being a real trader that survived. We don't need paid rug detection because the wallet signal implies the token passed theirs.

### If you want to explore paid data without pivoting
One rational test: **$49/mo Helius Developer trial for one month.** Not because it fixes rugs, but because sub-5s detection latency would address the *entry timing* problem independently (one of the two real issues — rugs being the other). If the latency improvement flips average entry ROI by even 5pp, it's a different conversation.

Or: **build a free Dune-based dev-wallet repeat-rug checker** as a weekend project. Zero cost, tests the highest-value hypothesis (do rug deployers repeat) at full data access before committing to a paid subscription.

### What I would NOT spend money on
- Birdeye Business ($299) — overkill
- Helius Business ($499) — we don't have volume for dedicated infra
- Paid Telegram alpha groups — not quantifiable, high scam risk
- Any service >$100/mo at current stakes

---

## Files

### Edge study / analysis
- `edge_study/features.csv` — 601-row feature matrix for ML
- `edge_study/extract_features.py` — ETL from history.json + logs
- `edge_study/train_classifier.py` — 3-tier classifier training
- `edge_study/report.md` — ML verdict (marginal edge via delay_s)
- `edge_study/lifecycle_extract.py` — Birdeye OHLCV fetcher (595 mints cached)
- `edge_study/lifecycle_analyze.py` — edge vs strategy diagnostic
- `edge_study/lifecycle_report.md` — lifecycle findings
- `edge_study/lifecycle_backtest.py` — 270-combo strategy grid
- `edge_study/lifecycle_backtest.md` — grid results
- `edge_study/catastrophic_analysis.py` — rug vs winner OHLCV diff
- `edge_study/catastrophic_report.md` — no-clean-signature verdict
- `edge_study/FINDINGS.md` — this file

### Data
- `edge_study/lifecycle/*.json` — 595 cached OHLCV files (Birdeye 1m, 4-5h windows)
- `edge_study/catastrophic_cache/*.json` — 85 Birdeye security responses (all empty — paid-tier blocked)
- `history.json` — 3982 raw events (signal_accept, execution_attempt, partial_realize, trade_close, etc.)
