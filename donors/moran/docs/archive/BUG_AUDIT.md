# BUG AUDIT — HARD_CAP_EXIT / -106% ROI / Ghost Close Chain

**Date:** 2026-04-07  
**Status:** AUDIT ONLY — no fixes applied  
**Severity:** CRITICAL  
**Observed symptom:** Buy confirmed, `HARD_CAP_EXIT` fires at T+1.3s, `realized_roi_pct = -106.06%`, `wallet_raw=0`, ghost close, `RECON=GAP`.

---

## Executive Summary

A confirmed bug causes `HARD_CAP_EXIT` to fire ~1–2 seconds after entry, before tokens have settled in the wallet. The price comparison uses a stale or pool-distorted mark that reads 20%+ below the Jupiter-quoted entry price at an age when no real price discovery has occurred. Because `wallet_raw=0` (tokens not yet settled), the sell gate records a ghost close with `proceeds_override=0.0`, yielding `pnl = 0 - basis - fees` and `ROI = -106%`. The position is then terminalized by ghost reconciliation, leaving actual tokens unmanaged.

---

## Q1 — Does `HARD_CAP_EXIT` have guards for exec_marks > 0, wallet balance, or minimum hold time?

**Answer: No. It has zero guards beyond a nonzero qty check.**

**Location:** `simulation_mode.py:6314–6333`

```python
# Line 6314 — HARD CAPITAL CAP block
_entry_px = float(tr.entry_px or 0.0)
_cur_px   = float(tr.current_px or tr.last_price or 0.0)
if _entry_px > 0 and _cur_px > 0:
    _loss_pct = (_entry_px - _cur_px) / _entry_px          # line 6321
    if _loss_pct >= MAX_POSITION_LOSS_PCT and float(tr.qty_remaining or 0.0) > 1e-9:
        ...
        await self._sell_fraction(tr, 1.0, "HARD_CAP_EXIT") # line 6330
```

`MAX_POSITION_LOSS_PCT = 0.20` (line 35). No check for:
- `executable_mark_observations > 0` — can fire on entry_px copied as first mark
- `total_mark_observations > 0` — same
- `wallet_raw > 0` — can fire before buy TX tokens settle
- minimum hold time — can fire at T+1.3s

**Contrast with `BLIND_NEGATIVE_EXIT` at line 6341**, which has an explicit `_blind_age >= 20.0` guard:
```python
if (_blind_total_marks == 0 and _blind_age >= 20.0   # 20s minimum hold enforced
        and _entry_px > 0 and _cur_px > 0 and _cur_px < _entry_px
        and float(tr.qty_remaining or 0.0) > 1e-9):
```

---

## Q2 — At T+1.3s after entry, can `tr.current_px` be 20%+ below entry_px?

**Answer: Yes. Three converging paths.**

**Path A — Raydium pool price vs Jupiter quote spread**

The buy executes via Jupiter (Jupiter-aggregated route). `entry_px` is set from the Jupiter quote output (line ~1037). The first mark price fetched from Raydium's AMM directly can be 15–25% below the Jupiter fill price due to:
- Price impact of the buy itself (post-buy the reserve ratio shifts)
- AMM spot price vs Jupiter-aggregated route
- Pump.fun → Raydium migration: pool may not have stabilized

**Path B — `tr.current_px` initialized to `entry_px` at position creation**

At position creation (line ~1024), `unrealized_roi_pct=0.0` and `tr.current_px` is initialized to `entry_px`. At T+1.3s, the first live mark from any source (DexScreener, RPC, or Jupiter) arrives. If that mark is 20%+ below the Jupiter fill price, the `_loss_pct` threshold triggers.

**Path C — `_cur_px = float(tr.current_px or tr.last_price or 0.0)`**

`tr.current_px or tr.last_price` — if `current_px` was never updated (mark delay), `last_price` is used. Both may reflect the pre-buy pool price, which could be substantially below the fill price on a pump.

`fresh_mark_seen_since_restore` (line 4173) is set when any mark arrives, but it only guards TP replay (line 5198) — it does NOT guard `HARD_CAP_EXIT`.

---

## Q3 — When `HARD_CAP_EXIT` reaches `_live_sell_gate()`, what happens when `wallet_raw=0`?

**Answer:** `HARD_CAP_EXIT` is NOT in the `is_stop_exit` set → takes the **non-emergency path** → returns `(True, 0.0)` and emits `LIVE_SELL_GHOST_CLOSE`.

**`is_stop_exit` definition at line 4529:**
```python
is_stop_exit = tag in ("TRAIL/SL EXIT", "FAILSAFE STOP", "RAPID DUMP EXIT",
                       "STALE_MARK_TIMEOUT_EXIT", "QUOTEABILITY_GUARD_EXIT", "AGE_GATE_EXIT")
```

`"HARD_CAP_EXIT"` is **not** in this set.

**Non-emergency path (`is_stop_exit == False`) at line 4582–4591:**
```python
if wallet_raw == 0:
    log.warning("[LIVE_SELL] wallet_balance=0 mint=%s tag=%s — confirmed ghost, paper close allowed", ...)
    append_telemetry_event(
        "LIVE_SELL_GHOST_CLOSE", mint=tr.mint,
        detail=f"tag={tag} wallet_raw=0 confirmed_zero=True", severity="warn",
    )
    return True, 0.0   # ← sold_ok=True, proceeds_override=0.0
```

At T+1.3s, the buy TX is confirmed on-chain (the bot already has it in state), but the token balance has not yet propagated to the RPC balance fetch. `wallet_raw = 0` is returned by the balance fetch → ghost close fires.

---

## Q4 — How does `proceeds_override=0.0` produce `ROI = -106%`?

**Answer:** The PnL formula subtracts full basis + fees from zero proceeds. The ROI formula divides that negative PnL by cost.

**`_sell_fraction()` at line 4703–4714:**
```python
proceeds_override: Optional[float] = None
if self.execution_mode == "live":
    sold_ok, proceeds_override = await self._live_sell_gate(tr, qty, qty_before, tag)
    if not sold_ok:
        return

proceeds = proceeds_override if proceeds_override is not None else (qty * float(tr.current_px or 0.0))
# proceeds = 0.0

basis_sold = float(tr.remaining_basis_usd or 0.0) * (qty / qty_before)
# basis_sold ≈ $100 (full position)

remaining_fee_reserve = max(0.0, float(tr.fees_usd or 0.0) - float(tr.realized_fees_usd or 0.0))
fee_sold = remaining_fee_reserve * (qty / qty_before)
# fee_sold ≈ $6

pnl = proceeds - basis_sold - fee_sold
# pnl = 0.0 - $100 - $6 = -$106
```

**`_finalize_trade_close()` ROI fallback at line 4277–4278:**
```python
_finalize_proceeds = float(tr.proceeds_usd or 0.0)   # = 0.0
# _finalize_proceeds > 0 branch skipped

tr.realized_roi_pct = (float(tr.realized_pnl or 0.0) / _finalize_cost) * 100.0
# = (-106.0 / 100.0) * 100.0 = -106.0%
```

The `_finalize_proceeds > 0` branch at line 4275 would compute `(proceeds/cost - 1) * 100`. Since `proceeds_usd=0.0`, this branch is skipped. The fallback formula at line 4278 is `pnl / cost`, which includes fees in the numerator → yields sub -100% ROI.

---

## Q5 — What is `RECON=GAP` and why does ghost reconciliation fire after the ghost close?

**Answer:** After the ghost close, the position remains "closed" in state but tokens are unmanaged. Ghost reconciliation finds `wallet_raw=0` and terminates the position without any history.json entry.

**`_periodic_ghost_reconciliation_loop()` at line 1898:**
- Runs every `GHOST_RECON_INTERVAL_SECONDS = 30` seconds (line 97) after an initial delay of `GHOST_RECON_INITIAL_DELAY_SECONDS = 45` seconds (line 96)
- Minimum position age before checking: `GHOST_RECON_MIN_POSITION_AGE_SECONDS = 45` (line 98)

**Flow after ghost close:**
1. `HARD_CAP_EXIT` fires at T+1.3s → `_live_sell_gate()` returns `(True, 0.0)` → `_sell_fraction()` records PnL with proceeds=0 → `_finalize_trade_close(tr, "HARD_CAP_EXIT")` marks position closed
2. Position status = `"closed"` in memory; `qty_remaining = 0.0`; but **no actual sell TX was submitted**
3. Actual tokens remain in wallet (they settled eventually after T+1.3s)
4. ~45s later, `_periodic_ghost_reconciliation_loop` checks open positions — but this position is already `closed` in state, so it may not be in `open_trades` list

**The `RECON=GAP` label** in telemetry is emitted by `_ghost_reconciliation_terminalize()` at line 1884:
```python
append_telemetry_event(
    "GHOST_RECONCILIATION_TERMINALIZE",
    mint=tr.mint,
    detail=f"size_usd={tr.size_usd:.4f} qty_remaining={tr.qty_remaining:.4f} entry_ts={int(tr.entry_ts or 0)}",
    severity="warn",
)
```

**Critical consequence:** `_ghost_reconciliation_terminalize()` (line 1774) explicitly:
- Does NOT modify `realized_pnl` (line 1785)
- Does NOT write a `trade_close` to `history.json` (line 1787)
- Writes a `ghost_reconciliation_terminal` audit record (line 1862–1868)
- Pops the trade from `self.trades` (line 1894)

The actual tokens (which settled post T+1.3s) are never sold. They become stranded in wallet, untracked.

---

## Full Bug Chain

```
T+0.0s   Buy TX submitted
T+1.3s   monitor_positions_loop fires
         → tr.current_px = pool spot price (20%+ below Jupiter fill)
         → _loss_pct >= 0.20 (no guards)
         → HARD_CAP_EXIT fires
         → _live_sell_gate() called with tag="HARD_CAP_EXIT"
         → "HARD_CAP_EXIT" NOT in is_stop_exit → non-emergency path
         → _fetch_live_token_balance() returns 0 (tokens not yet settled)
         → LIVE_SELL_GHOST_CLOSE emitted
         → returns (True, 0.0)
         → proceeds_override = 0.0
         → pnl = 0 - $100 - $6 = -$106
         → ROI = -106%
         → _finalize_trade_close() called → position marked closed in state
         → NO sell TX submitted
T+??s    Tokens actually settle in wallet (unmanaged, untracked)
T+45s+   _periodic_ghost_reconciliation_loop runs
         → position already closed, may skip
         → OR finds position with wallet_raw=0 (closed state) → RECON=GAP
```

---

## Affected Code Locations

| Location | Line | Issue |
|---|---|---|
| `simulation_mode.py:35` | 35 | `MAX_POSITION_LOSS_PCT=0.20` — threshold without hold guard |
| `simulation_mode.py:6318–6322` | 6318–6322 | `HARD_CAP_EXIT` check: no exec_marks, no age, no wallet guard |
| `simulation_mode.py:4529–4530` | 4529–4530 | `is_stop_exit` set missing `"HARD_CAP_EXIT"` |
| `simulation_mode.py:4582–4591` | 4582–4591 | Ghost close on `wallet_raw=0` for non-stop path |
| `simulation_mode.py:4711` | 4711 | `proceeds = proceeds_override` — 0.0 causes sub-100% PnL |
| `simulation_mode.py:4277–4278` | 4277–4278 | ROI fallback: `pnl/cost` — includes fees → sub -100% |
| `simulation_mode.py:1898` | 1898 | Ghost recon fires post-close, position already removed |

---

## Suggested Fixes (NOT applied — audit only)

1. **Add minimum hold guard to `HARD_CAP_EXIT`:** Require `position_age_s >= 5.0` before firing. Five seconds is enough for tokens to settle and one real mark to arrive.
2. **Add exec_marks guard:** Require `executable_mark_observations >= 1` before comparing against `MAX_POSITION_LOSS_PCT`. The first executable mark confirms the position is actually observable.
3. **Add `"HARD_CAP_EXIT"` to `is_stop_exit`:** This ensures that if the ghost-close path is hit on an emergency exit, it takes the emergency path with emergency slippage and appropriate telemetry — not a silent paper close.
4. **Wallet balance pre-check in HARD_CAP_EXIT block:** Before calling `_sell_fraction`, fetch wallet balance inline. If `wallet_raw=0`, defer exit (schedule retry in 2–3s) rather than immediately ghost-closing.
5. **ROI floor for ghost closes:** When `proceeds_override=0.0` is returned (ghost close), do not use this for realized ROI. Instead mark ROI as `None` or `"UNRESOLVED"` and let ghost reconciliation determine the true outcome.
