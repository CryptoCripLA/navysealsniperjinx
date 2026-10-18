# Sniperbot Stabilization Task Tracker

## Audit 2026-04-22 — Phased Remediation Plan

Full findings in the 2026-04-22 chat audit. 50+ distinct issues across `simulation_mode.py`, `live_canary.py`, `sniper_hud_api.py`, `app.js`, and the analysis scripts. Phases are ordered by blast-radius: Phase 1 is the only one that gates stake-scaling; later phases harden operations and analysis trust.

**Ground rules while fixing:**
- Bot must be stopped before editing `simulation_mode.py` / `live_canary.py`; restart only after the phase is validated on paper for 20+ candidates.
- Every code change in Phases 1–3 gets a matching entry in `data/variable_changes.jsonl` via `variable_logger.py`.
- No change is "done" until grep confirms the old pattern is gone from the file.
- Do NOT push to origin/main until Phase 1 lands in full and 20-trade paper validation passes.

---

### Phase 0 — Safety gate (do first, one sitting)

- [x] 0.1 Commit the 4 modified + 3 untracked files so current state is recoverable (`simulation_mode.py`, `feature_builder.py`, `hard-reset.sh`, `IMPLEMENTATION_PLAN.md`, `VARIABLES_REGISTRY.md`, `data/`, `variable_logger.py`).
- [x] 0.2 Confirm bot is stopped (`pgrep -f simulation_mode.py` returns nothing) before Phase 1 begins.
- [x] 0.3 Snapshot `runtime_state.json` + `history.json` to `data/pre_audit_fix_2026-04-22/` so a rollback has a known-good base.

### Phase 1 — Critical money-path (blocks stake scaling)

Every item here is a confirmed real-money loss vector.

- [x] 1.1 **Fix Jito signature extraction** — `live_canary.py:926-927`. Jito returns `bundle_id`, not a tx sig; first 64 bytes of a VersionedTransaction are not the signature. Rework to either (a) sign-then-read `tx.signatures[0]` before submit, or (b) poll Jito `getBundleStatuses` for the landed sig. Every Jito-routed buy/sell verification is broken until this fix ships.
- [x] 1.2 **Implement `get_token_balance(mint)`** — `live_canary.py:822`. Called by `classify_verification_timeout`, currently `NameError` at runtime. Use `getTokenAccountsByOwner` for the wallet pubkey + mint, sum `uiAmount`.
- [x] 1.3 **Buy slippage default 1% → 5%** — `live_canary.py:942` `max_slippage_pct: float = 0.01` → `0.05`. Canonical is 5%.
- [x] 1.4 **Sell slippage default 2% → 3% (TP) / 10% (forced)** — `live_canary.py:1096`. Take a `kind=` param and select.
- [x] 1.5 **Book Jito tip into PnL** — `live_canary.py:354-362` `record_canary_trade(pnl_usd)` → add `tip_lamports` and deduct `tip_lamports * sol_price_usd / 1e9`. Propagate from both the buy and sell call-sites.
- [x] 1.6 **Bundle landing confirmation** — `live_canary.py:833-867`. After `submit_jito_bundle` returns `bundle_id`, poll `getBundleStatuses` for up to 15s; if not landed, treat as failure and fall through to RPC path.
- [x] 1.7 **Fix base-unit price mismatch in `_live_sell_gate`** — `simulation_mode.py:6068-6069` and `:2068-2069`. `actual_price = actual_proceeds_usd / whole_tokens_sold` — make sure `raw_to_sell` is first converted to whole tokens via `/10**TOKEN_DECIMALS`.
- [x] 1.8 **Guard entry_px against sentinel** — `simulation_mode.py:1534, 1582` raise to 1e-9 min and tag position `NO_ENTRY_PRICE`; reject all HARD_CAP/trail/TP calcs on tagged positions until first real mark. Callers: `:6930, 6942, 7051, 7035, 3136, 3268`.
- [x] 1.9 **HARD_CAP stale-mark guard** — `simulation_mode.py:8220-8270`. Add `if tr.mark_stale or (now - tr.last_price_update_at) > STALE_MARK_EXIT_TIMEOUT_SECONDS: skip HARD_CAP`.
- [x] 1.10 **Daily-loss cap enforced in paper** — `simulation_mode.py:5700-5705`. Call `record_canary_trade(pnl)` unconditionally, or mirror logic into a paper-scoped counter that also halts new entries at `DAILY_LOSS_CAP_USD`.
- [x] 1.11 **Validate `price > 0` before delayed-entry enqueue** — `simulation_mode.py:3980-3990`. Reject enqueue if `reference_price` is None/0 rather than writing sentinel downstream.

### Phase 2 — Concurrency & state integrity

- [x] 2.1 **`asyncio.Lock` around `self.trades` mutation** — `simulation_mode.py:1148, 1579, 2082, 1414`. Serialize enqueue / pop / state-transition.
- [x] 2.2 **Per-mint sell lock** — `live_canary.py:1092-1182`. `self._sell_locks[mint]` — never run two concurrent sells for the same mint.
- [x] 2.3 **GHOST_PENDING timeout** — `simulation_mode.py:1304-1315, 2246-2249`. Force-terminalize after `GHOST_PENDING_MAX_AGE_SECONDS = 300` if recheck never fires.
- [x] 2.4 **Serialize TP stage flags** — `simulation_mode.py:7054-7066` → state.json. Restore at `:2606`.
- [x] 2.5 **Restore `entry_ts` on state load** — `simulation_mode.py:2606`. Reject restores with `entry_ts == 0`.
- [x] 2.6 **`_sell_verify_poll` vs forced-exit race** — `:6277-6352` vs `:8345-8360`. Gate forced-exit on `sell_pending_verify_ts == 0`.
- [x] 2.7 **Atomic read of `runtime_state.json`** — `:687`. Acquire fcntl read-lock to match write side at `:753-758`.
- [x] 2.8 **Offload `append_history` rename off event loop** — `:821`. `await loop.run_in_executor(None, _tmp.replace, HISTORY_FILE)`.
- [x] 2.9 **Partial-fill accounting** — `simulation_mode.py:6425-6427`. Guard on `qty_before > 0 and qty > 0`; don't credit `qty_closed` when Jupiter returned `outAmount=0`.

### Phase 3 — Exit-logic correctness

- [x] 3.1 **TP ladder vs trailing-stop priority** — `simulation_mode.py:7054` vs `:7032`. Either raise TP1 above trail-arm (25%) or change intent: pick one doctrine and enforce it.
- [x] 3.2 **Stop ladder 0–29s gap** — `:1119, 1550, 6797` hardcoded `entry_px*0.85`. Replace with `DEFAULT_DOWNSIDE_STOP_PCT`.
- [x] 3.3 **Align delayed-entry liquidity threshold with MIN_LIQUIDITY_USD** — `:4780` `_LIQ_THRESHOLD_USD=5000` vs `MIN_LIQUIDITY_USD=25000`. Use the stricter one on entry.
- [x] 3.4 **Wire or remove `PRE_ENTRY_SURGE_RATIO_MAX`** — `:46-49`. Currently dead config.
- [x] 3.5 **`ENTRY_SCORE_THRESHOLD` enforced on all entry paths** — `:4678-4690` delayed, `:4468-4527` migration retry, and the immediate-entry branch.
- [x] 3.6 **Rate-limit / 429 backoff on RPC pool** — `:104-114`. Exponential backoff per-endpoint; skip endpoint for 10s after a 429.
- [x] 3.7 **Requeue backoff on Jupiter price failure** — `:4483-4522`. Exponential, not immediate.
- [x] 3.8 **Migration-retry queue-full telemetry** — `:4751` `put_nowait` silently drops; emit `MIGRATION_RETRY_QUEUE_FULL` event.

### Phase 4 — Analysis trustworthiness (gate for any threshold change)

- [x] 4.1 **Single source of truth for exit constants** — move `DEFAULT_STOP_PCT, TP1/2/3, TRAIL_FROM_MFE_PCT, BE_TRIGGER_PCT, HARD_CAP_PCT` into `constants.py`. Every replay/analysis script imports from there. Delete inline defaults in `replay_v2.py:40-44`, `replay_analyzer.py:42-47`, `exit_lab.py:40-44`, `replay_experiment_runner.py:54-59`.
- [x] 4.2 **Canonical win-rate definition** — pick one (`pnl_usd > 0` or `roi_pct > 10`), document in `constants.py`, fix `feature_builder.py:129`, `batch_scorecard.py:145`, `replay_analyzer.py:500`, `app.js:70`, and HUD display to match.
- [x] 4.3 **Split predictive vs explanatory model features** — `model_ranker.py:28-34`. Create `NUMERIC_AT_ENTRY` and `NUMERIC_POST_HOC`. Report AUC separately; headline metric is entry-only.
- [x] 4.4 **Survivorship denominator** — track qualified-but-not-entered candidates (veto reasons already in history). Report "trades per 100 candidates" alongside win rate.
- [x] 4.5 **95% CIs on all win rates** — add Clopper-Pearson to `last25_analysis.py`, `batch_scorecard.py`, `trade_attribution.py`. Flag any N < 30.
- [x] 4.6 **Centralize contamination filter** — deduplicate `replay_v2.py:231-238` vs `replay_analyzer.py:429-436`. Move to `constants.py` or `extract_trades.py` output.
- [x] 4.7 **Deduplicate suspect-price constants** — `replay_v2.py:124-130` vs `trade_attribution.py:25-26`.
- [x] 4.8 **Justify bucket boundaries** — `feature_builder.py:23-36, 74-84, 87-94`. Cite edge-study or replace with adaptive (quantile) bucketing.
- [x] 4.9 **MFE 500% cap → flag column** — `replay_v2.py:158, exit_lab.py:72, batch_scorecard.py:214`. Don't silently cap; add `mfe_capped: bool`.
- [x] 4.10 **Latency-aware ROI** — factor `detect_to_exec_ms` into expected outcome; don't just filter in `replay_analyzer.py:302-310`.

### Phase 5 — HUD & operational hardening

- [x] 5.1 **HUD stops writing `runtime_state.json`** — `sniper_hud_api.py:108-137, 2021-2032, 2082-2084`. Move operator commands to `data/operator_commands.jsonl` (append-only, consumed by trading loop).
- [x] 5.2 **Escape HTML in HUD rendering** — `app.js:381-390, 461-465, 603, 622`. Replace `innerHTML` + `text()` with DOM `textContent` or add a `escapeHtml()` helper applied to every untrusted field (token name, reason, mint).
- [x] 5.3 **`hard-reset.sh` graceful shutdown** — `hard-reset.sh:28-33`. Send SIGTERM, wait 10s for ack via `runtime_state.shutting_down`, then SIGKILL. Snapshot `.env` before overwrite; rollback if restart fails.
- [x] 5.4 **`metrics_snapshots.jsonl` streaming + rotation** — `sniper_hud_api.py:2267-2292`. Tail the file, don't `read_text`. Rotate at 50MB to `.jsonl.gz`.
- [x] 5.5 **Log rotation** — `logs/logs_hud.out` (31MB) and `logs/logs_sim.err` (17MB). Logrotate config or built-in `RotatingFileHandler`.
- [x] 5.6 **Operator commands queue → append-only log** — `sniper_hud_api.py:135` `q[-100:]` truncation silently drops; log every accepted command to `data/operator_commands.jsonl`.
- [x] 5.7 **Mint base58 validation on `/control`** — `sniper_hud_api.py:2026-2032`.
- [x] 5.8 **WebSocket client cleanup** — `sniper_hud_api.py:2449-2462`. Heartbeat ping every 30s; drop stale clients.
- [x] 5.9 **Torn-read guard on history.json** — `sniper_hud_api.py:232-236`. fcntl shared-lock.
- [x] 5.10 **`status.sh` / `watch.sh` / `analyze.sh` shell hardening** — quote `env_get` output, fail loudly on missing log file.

### Phase 6 — Hygiene & medium

- [x] 6.1 **Price-impact estimator pre-buy** — `live_canary.py:961-1007`. Jupiter `priceImpactPct` from quote; abort if > configurable threshold.
- [x] 6.2 **SOL rent-exemption check before wrap** — `live_canary.py:938-994`. Require balance ≥ `lamports + 0.003 SOL`.
- [x] 6.3 **Timing counter reset** — `simulation_mode.py:6652-6662`. Reset every hour; print rolling averages.
- [x] 6.4 **Reset-safe `_cached_keypair`** — `live_canary.py:366-445`. `threading.Lock` around first load; zeroize `secret_bytes` after `Keypair.from_bytes`.
- [x] 6.5 **Jito tip-account rotation** — `live_canary.py:225-276`. Round-robin the 8 known tip accounts (via `_jito_tip_account` in `simulation_mode.py`).
- [x] 6.6 **Remove unused imports** — `live_canary.py:36-37` (`Signature`, `to_bytes_versioned`).
- [x] 6.7 **Wire `CANARY_FAILSAFE_STOP_PCT`** — `live_canary.py:51`. Tightens `absolute_failsafe_stop_pct` in live mode via `min(...)` in `_evaluate_stop_and_trail`.
- [x] 6.8 **Veto-log rotation robust to errors** — `simulation_mode.py:915-923`. Wrap rename in try/except; fall back to truncation.
- [x] 6.9 **Handle empty df in `last25_analysis.py:58`**.
- [x] 6.10 **Mark HANDOFF_DOCUMENT.md as historical**; IMPLEMENTATION_PLAN.md supersedes. *(2026-04-22: moved to `docs/archive/HANDOFF_DOCUMENT.md`.)*
- [x] 6.11 **Delete or exercise `canary_status` endpoint** — `sniper_hud_api.py:2071-2076`.
- [x] 6.12 **Single `/Users/.../source_of_truth` hardcoded path** — `sniper_hud_api.py:988-992`. Read from env or config.

### Phase 7 — Validation gate before stake scaling

- [ ] 7.1 Paper-trade 50 candidates post-Phase-1 — confirm no `NO_ENTRY_PRICE`, no double-sell telemetry, no stale HARD_CAP. *(OPERATOR — pending paper-trade session)*
- [ ] 7.2 Diff reported headline PnL vs wallet SOL delta × SOL price × fx — must reconcile to within 1% after Jito tip booking. *(OPERATOR — after P7.1)*
- [x] 7.3 Run `replay_v2.py` + `exit_lab.py` with the new `constants.py`; confirm exit-strategy rankings are unchanged or re-documented. → **2026-04-22: baseline frozen in `EXIT_LAB_REPORT.md` (Fixed -6% trail #1 in all slices).**
- [ ] 7.4 Only then re-open scaling gate (two PASS batches of 20 trades, WR ≥ 40%, HARD_CAP < 15%). *(OPERATOR — after P7.1)*

---

## Previously completed (pre-audit)

- [x] P0-1: RugCheck liquidity_lock bypass for new migrations (indexing_lag re-queue)
- [x] P0-2: Reduce WS hot-path state I/O (in-memory cache + 5s flush)
- [x] P1-1: Optimize mint parsing defaults (retries=3, timeout=3s, budget=5s, linear backoff)
- [x] P1-2: Improve log-based Raydium extraction (contextual mint patterns before RPC)
- [x] P2-1: Onchain authority bypass for lp_pct_threshold liquidity vetos
- [x] P2-2: Funnel observability (telemetry for budget/dup blocks, execution_deferred counter)
- [x] P3-1: Parallelize rug check + price + holder + onchain (all 5 concurrent)
- [x] P3-2: Reuse drift guard price if <2s old
- [x] P4-2: Health-weighted WS endpoint selection (uptime ratio + 30s cooldown)
- [x] P4-3: WS reconnect exponential backoff with jitter

## Skipped / deferred (pre-audit)

- [ ] P0-3: WS heartbeat robustness — SKIPPED per audit: fail-fast-to-reconnect is correct behavior.
- [ ] P3-3: Batch state writes in on_log_event — DEFERRED: each write_state is on a different early-return path.

## Manual steps

- [ ] P1-3: Update .env with tuned parse/retry values
- [ ] P4-1: Add Helius WSS endpoint to WS_URLS in .env

## Canary Go/No-Go (unchanged)

| Metric | Required | Method |
|---|---|---|
| Health > 80 | YES | HUD `/api/state` |
| Parse rate > 80% | YES | `entry_funnel.parsed_successfully / raw_candidates_seen` |
| WS disconnects < 5/hr | YES | `ws_disconnects_1h` |
| Execution reached | YES | `execution_attempted > 0` in first 30 min |
| No `liquidity_lock` dominance | YES | `terminal_blockers_summary` |
| Latency < 3s median | NICE | `candidate_timing` |
