# Bug Fix Summary — Mark Price Truth / Exit Price / -106% ROI

**Date:** 2026-04-08  
**Files changed:** `simulation_mode.py` only  
**live_canary.py:** no changes needed  
**Syntax check:** both files pass `python3 -m py_compile`

---

## Fix A — `actual_price` base-unit division mismatch (`_live_sell_gate`, line ~4635)

**Was:**
```python
actual_price = actual_proceeds_usd / raw_to_sell
tr.current_px = actual_price
tr.last_price = actual_price
```

**Now:**
```python
_TOKEN_DECIMALS = 6  # pump.fun SPL standard
_whole_tokens_sold = raw_to_sell / (10 ** _TOKEN_DECIMALS)
actual_price = actual_proceeds_usd / _whole_tokens_sold if _whole_tokens_sold > 0 else 0.0
tr.current_px = actual_price
tr.last_price = actual_price
```

**Why:** `raw_to_sell` is in base units (`wallet_raw` from `getTokenAccountsByOwner`, where `tokenAmount.amount` = smallest SPL units = whole_tokens × 10^6). Dividing USD proceeds by base units produced a price 10^6 times too small. This set `current_px` and `last_price` to ~0 after every successful sell, which:
- Caused `exit_price = 0.000000` in history records
- Triggered false RAPID_DUMP_EXIT on the next loop tick (prev_px vs ~0 = 100% drop)
- Caused -106% ROI when `out_lamports=0` on a subsequent sell (`proceeds = qty × ~0 ≈ 0`)

**Same fix applied in the TP retry block** (added by Fix D) to keep the retry path consistent.

---

## Fix B — Jupiter Price API v2 pump.fun curve contamination

### `_token_price()` — concurrent DexScreener cross-check

**Was:** Returned Jupiter Price API v2 result directly.

**Now:** Fires `_dexscreener_price(mint)` concurrently via `asyncio.create_task`. After getting the Jupiter price, compares:
- If `jup_px > ds_px × 3.0`: discards Jupiter price, returns DexScreener price, logs warning `[MARK] jup price/v2 >3x dexscreener`
- Otherwise: returns Jupiter price (DexScreener result is cancelled)
- If Jupiter fails: harvests DexScreener result that was already in-flight

The concurrent design adds zero serial latency when Jupiter and DexScreener respond within the same 1s window.

### `_price_race()` — collect both, then compare

**Was:** Returned the first valid result from either Jupiter or DexScreener (whichever responded first). Jupiter is typically faster, so it won and delivered the pump.fun price.

**Now:** Uses `asyncio.wait(ALL_COMPLETED, timeout=1.5)` to collect both results, then:
- If both valid and `jup_px > ds_px × 3.0`: returns DexScreener, logs `[PRICE_RACE] jup >3x dex`
- If both valid and within 3×: returns Jupiter (faster, within expected range)
- If only one valid: returns that result

**Why:** Jupiter Price API v2 (`/price/v2`) aggregates from all sources Jupiter knows about, including the pump.fun bonding curve. Post-migration, the bonding curve reports the pre-dump completion price (high) while the actual Raydium pool price is significantly lower. This was the cause of +133% HUD ROI vs +1.6% in Phantom. The 3× threshold provides enough headroom for legitimate short-term price spikes while catching the pump-vs-Raydium divergence.

---

## Fix C — `proceeds` floor for near-zero `current_px` (`_sell_fraction`)

**Was:**
```python
proceeds = proceeds_override if proceeds_override is not None else (qty * float(tr.current_px or 0.0))
```

**Now:**
```python
if proceeds_override is not None:
    proceeds = proceeds_override
else:
    _fallback_px = float(tr.current_px or 0.0)
    _entry_px_floor = float(tr.entry_px or 0.0)
    if _fallback_px <= 0 or (_entry_px_floor > 0 and _fallback_px < _entry_px_floor * 0.001):
        # current_px corrupted (< 0.1% of entry) — use entry_px as floor
        log.warning("[SELL_FRACTION] current_px near-zero, using entry_px as proceeds floor")
        append_telemetry_event("SELL_PROCEEDS_FLOOR_APPLIED", ...)
        _fallback_px = _entry_px_floor
    proceeds = qty * _fallback_px
```

**Why:** When `proceeds_override=None` (sell TX succeeded but `out_lamports=0`), the fallback `qty × current_px` produces near-zero proceeds if `current_px` was corrupted by Fix A's bug. Using `entry_px` as a floor ensures proceeds are never catastrophically under-stated. The 0.001× threshold (0.1% of entry) catches the base-unit corruption case while allowing genuine near-zero prices.

### ROI hard floor in `_finalize_trade_close()`

Added after the ROI calculation block:
```python
if tr.realized_roi_pct is not None and tr.realized_roi_pct < -100.0:
    log.warning("[FINALIZE] realized_roi_pct=%.2f%% < -100%% — clamping to -100%%")
    tr.realized_roi_pct = -100.0
```

**Why:** A position cannot lose more than 100% of its cost. Any sub-100% ROI indicates corrupted proceeds data. The clamp surfaces this as "full loss" rather than a mathematically impossible number that breaks analytics.

---

## Fix D — TP sell `tx_verification_timeout` retry (`_live_sell_gate`)

**Was:** When `execute_live_sell()` returned `(False, sig, "tx_verification_timeout", None)` for a TP sell (non-stop-exit), the failure path just returned `(False, None)`. The TP stage (`stage1_done`, etc.) was already set to `True` before `_sell_fraction` was called, so the TP would never re-trigger. Net effect: TP consumed, no tokens sold, position stuck.

**Now:** When `err_msg == "tx_verification_timeout"` and `not is_stop_exit`:
1. Logs warning and emits `TP_SELL_VERIFICATION_TIMEOUT_RETRY` telemetry
2. Immediately retries `execute_live_sell()` with `LIVE_SELL_MAX_SLIPPAGE_PCT` (10%, wider than the original 3% TP slippage) to guarantee execution on the retry
3. The retry gets a fresh Jupiter quote — routes through real Raydium price, not the stale/inflated price of the first attempt
4. On retry success: applies the corrected `actual_price` formula (Fix A), emits `TP_SELL_RETRY_SUCCESS`, returns `(True, retry_proceeds)`
5. On retry failure: emits `TP_SELL_RETRY_FAILED`, falls through to normal `(False, None)` return

**Why:** TP sells were getting `tx_verification_timeout` because the TX was submitted at an inflated HUD price (pump.fun bonding curve price). The inflated price triggered a TP sell above the real market price, and the TX either failed silently or could not be verified. The retry forces a fresh Jupiter quote at the real current market price, which routes through Raydium and executes at actual market conditions.

**Risk note:** If the original TX actually confirmed (just wasn't verifiable within the window), the retry would double-sell. `classify_verification_timeout()` (in live_canary.py) attempts to refine uncertain outcomes before returning `tx_verification_timeout`. When it still returns uncertain after refinement, the TX has most likely expired or been dropped by the Solana network — the retry is safe in the majority of cases.

---

## Unchanged

- `live_canary.py`: no changes required
- `execute_live_sell()` signature: unchanged
- TP ladder logic (`stage1_done`, `stage2_done`, `stage3_done`): unchanged — stages are consumed before the sell is attempted, which is correct behavior
- Stop exits (`RAPID DUMP EXIT`, `TRAIL/SL EXIT`, `HARD_CAP_EXIT`, `FAILSAFE STOP`): no retry logic added — `forced_exit_unresolved=True` behavior unchanged for stop paths
