# BUG AUDIT 2 — Mark Price Truth / +133% HUD ROI / -106% Rapid Dump

**Date:** 2026-04-08  
**Status:** AUDIT ONLY — no fixes applied  
**Severity:** CRITICAL  
**Observed symptoms:**  
- Active position showed +133% ROI in HUD while Phantom showed +1.6%
- Bot attempted TP sells at HUD price, got `tx_verification_timeout`
- Eventually forced `RAPID_DUMP_EXIT` at -16.6%
- `exit_price` records as 0.000000 on many closes
- `-106.06%` ROI still occurring on some `RAPID_DUMP_EXIT` closes

---

## Q1 — MARK PRICE SOURCE MISMATCH

### What each method queries

**`_executable_mark_price()` (line 2654–2687) — Jupiter Quote API**

```python
amount_base_units = qty_tokens * (10 ** _TOKEN_DECIMALS)   # _TOKEN_DECIMALS = 6 hardcoded
quote = await get_jupiter_quote(
    input_mint=mint,
    output_mint=_WSOL,
    amount=amount_base_units,
    slippage_bps=EXECUTABLE_MARK_SLIPPAGE_BPS,   # 500 = 5%
)
out_lamports = int(quote.get("outAmount", 0))
position_value_usd = (out_lamports / 1e9) * sol_usd
return position_value_usd / qty_tokens             # price per WHOLE token
```

- Endpoint: `https://quote-api.jup.ag/v6/quote` (Jupiter Quote API v6 / v7)
- Routes through **best available Raydium pool** for a migrated token
- Returns actual executable proceeds accounting for slippage and real AMM liquidity
- **Correct for post-migration tokens** — Jupiter routes away from pump.fun bonding curve once Raydium pool has liquidity

**`_token_price()` (line 2576–2589) — Jupiter Price API v2**

```python
async with s.get(f"https://api.jup.ag/price/v2?ids={mint}", timeout=1.0) as r:
    row = (d.get("data") or {}).get(mint)
    return float(row["price"])
```

- Endpoint: `https://api.jup.ag/price/v2` (Jupiter Price API — mid-market reference)
- Returns a **mid-market reference price**, NOT an executable price
- Does NOT specify which AMM/pool to use — Jupiter aggregates from all sources it knows about
- **PROBLEM**: For a token recently migrated from pump.fun → Raydium, Jupiter Price API v2 may still serve the pump.fun bonding curve price. The bonding curve price at completion reflects the $69K bonding curve exhaust point, which is significantly higher than the initial Raydium pool price (post-migration dump). This gap can persist for minutes while Jupiter re-indexes the Raydium pool as the primary source.
- Does NOT account for price impact — mid-market price, not real exit value

**`_dexscreener_price()` (line 2591–2619) — DexScreener Raydium-preferred**

```python
url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
pairs.sort(key=lambda p: float(((p.get("liquidity") or {}).get("usd") or 0)), reverse=True)
for p in pairs:
    dex = str(p.get("dexId") or "").lower()
    if "raydium" in dex:
        return float(px)  # ← First returns Raydium price
for p in pairs:
    if "pump" in str(p.get("dexId") or "").lower():
        continue          # ← Explicitly skips pump.fun pairs
    return float(px)
```

- Explicitly prefers Raydium dex, skips pump.fun
- Sorted by liquidity descending — returns highest-liquidity pair
- **Correct for post-migration tokens** — will show actual Raydium pool price

### The fallback chain in `_mark_price_with_retries()` (line 3022)

```
Priority 1: _executable_mark_price()    → Jupiter Quote API (Raydium, correct)
Priority 2: _token_price()              → Jupiter Price API v2 (may serve pump.fun price)
Priority 3: _dexscreener_price()        → DexScreener Raydium-preferred (correct)
```

**Root cause of +133% vs +1.6%**: When the executable mark fails (e.g., Jupiter quote API timeout or rate limit), the fallback is `_token_price()` — Jupiter Price API v2. For a recently migrated token, this API returns the pump.fun bonding curve reference price, which is significantly higher than the actual Raydium pool price where any real trade would execute. The Price API has no slippage modeling; the actual Raydium pool has initial dump pressure from migration.

The mark cache TTL is 500ms (`MARK_PRICE_CACHE_TTL_MS = 500`, line 330). If the inflated pump.fun price gets cached, it persists for 500ms per cycle. If the executable mark continues to fail, each cycle re-fetches from `_token_price()` (inflated).

The HUD displays `tr.unrealized_roi_pct` which is computed from `tr.last_price` (set to the wrong mark) — so the HUD faithfully displays the wrong mark as +133%.

---

## Q2 — ROI CALCULATION IN ACTIVE POSITIONS

### Where `unrealized_roi_pct` is computed

**`_paper_positions_snapshot()` (line 2022–2030):**

```python
mark_px = float(tr.last_price or tr.current_px or tr.entry_px or 0.0)
market_value_usd = max(0.0, float(tr.qty_remaining or 0.0) * mark_px) if mark_px > 0 else 0.0
remaining_fee_reserve_usd = max(0.0, float(tr.fees_usd or 0.0) - float(tr.realized_fees_usd or 0.0))
unrealized = market_value_usd - float(tr.remaining_basis_usd or 0.0) - remaining_fee_reserve_usd
unrealized_denom = float(tr.remaining_basis_usd or 0.0) + remaining_fee_reserve_usd
tr.unrealized_roi_pct = (unrealized / unrealized_denom) * 100.0 if unrealized_denom > 0 else 0.0
```

**Formula:** `ROI% = (qty_remaining × last_price - remaining_basis - fee_reserve) / (remaining_basis + fee_reserve) × 100`

**This uses `tr.last_price` first** (line 2025). `tr.last_price` is set by two separate paths:

1. `_refresh_trade_mark()` (line 4140): `tr.last_price = px` — from mark refresh (executable quote or Jupiter Price API fallback)
2. `_live_sell_gate()` (line 4637): `tr.last_price = actual_price` — **after a successful sell**, where:

```python
actual_price = actual_proceeds_usd / raw_to_sell   # line 4635 — UNIT MISMATCH (see Q4)
```

**`raw_to_sell` is in BASE UNITS** (smallest token units — wallet_raw from `_fetch_live_token_balance`, which returns `getTokenAccountsByOwner` parsed amount field, i.e. whole_tokens × 10^6 for 6-decimal SPL mints). Dividing USD proceeds by base units yields price per base unit — **10^6 times too small** for a 6-decimal token.

### HUD source

The HUD table at line 6272 in the metrics snapshot uses `tr.unrealized_roi_pct`:
```python
"roi_pct": round(tr.unrealized_roi_pct, 4),
```

`_annotate_position()` in `sniper_hud_api.py` (line 152) reads `unrealized_roi_pct` from the state snapshot and displays it as-is (with optional anomaly flags if ROI ≥ 1000 or mark is stale).

**Summary:** The HUD ROI uses `last_price` as the mark. `last_price` can be set to a wrong value by either:
- Q1 bug: pump.fun bonding curve price via Jupiter Price API v2 fallback → inflated ROI
- Q4 bug: `actual_proceeds_usd / raw_to_sell` (base units) → near-zero price → near -100% ROI

---

## Q3 — EXIT TRIGGER PRICE SOURCE

### HARD_CAP_EXIT (line 6341–6344)

```python
_entry_px = float(tr.entry_px or 0.0)
_cur_px   = float(tr.current_px or tr.last_price or 0.0)
_loss_pct = (_entry_px - _cur_px) / _entry_px
if _loss_pct >= MAX_POSITION_LOSS_PCT and float(tr.qty_remaining or 0.0) > 1e-9:
```

Uses `tr.current_px`. `tr.current_px` is set by `_refresh_trade_mark()` to the mark (same as HUD).

**If mark is inflated (pump.fun price, e.g. +133%):**  
`_loss_pct = (entry_px - inflated_px) / entry_px` is **negative** → HARD_CAP_EXIT does NOT fire even though the real price is down. This allows real losses to run undetected.

**If mark is near zero (unit mismatch after prior sell):**  
`_loss_pct ≈ 1.0 (100%)` → HARD_CAP_EXIT fires immediately on next loop tick after any successful partial TP sell. The guards we added (age ≥ 5s, exec_marks ≥ 1) would block this for the very early case, but NOT for a position that has been running and done a TP sell.

### RAPID_DUMP_EXIT (line 5173–5177)

```python
recent_drop = (tr.prev_px - tr.current_px) / tr.prev_px
if recent_drop >= RAPID_DUMP_PCT and tr.qty_remaining > 0:
    await self._sell_fraction(tr, tr.qty_remaining / tr.qty_total, "RAPID DUMP EXIT")
```

Uses `tr.current_px` for both prev (captured previous tick as `tr.prev_px = tr.current_px` at line 5185) and current comparison. `tr.prev_px` is updated to `tr.current_px` AFTER the dump check.

**If a partial TP sell sets `tr.current_px ≈ 0` (unit mismatch):**  
On the very next loop iteration:
- `tr.prev_px` = previous mark price (e.g., $0.0001) — set in the prior tick
- `tr.current_px` ≈ 0 (from `actual_price = actual_proceeds_usd / raw_to_sell`)
- `recent_drop = ($0.0001 - 0) / $0.0001 = 100%`
- RAPID_DUMP_EXIT fires immediately even if the real market hasn't moved

**Both exits use the same `tr.current_px` as the HUD.** When the mark is wrong, exits trigger at wrong thresholds — inflated mark suppresses exits (allows real losses), near-zero mark causes false exits.

---

## Q4 — THE -106.06% EXACT CAUSE FOR RAPID_DUMP_EXIT

### The primary cause: unit mismatch in `actual_price` calculation

**`_live_sell_gate()` (line 4630–4637):**

```python
if success:
    actual_proceeds_usd: Optional[float] = None
    if out_lamports and out_lamports > 0:
        actual_proceeds_usd = (out_lamports / 1_000_000_000) * sol_usd
        # Back-compute actual execution price and update mark so paper accounting is accurate
        actual_price = actual_proceeds_usd / raw_to_sell          # ← BUG HERE
        tr.current_px = actual_price
        tr.last_price = actual_price
```

**`raw_to_sell` is in BASE UNITS (smallest token units):**
```python
raw_to_sell = wallet_raw          # from _fetch_live_token_balance → getTokenAccountsByOwner → amount field
```
`_fetch_live_token_balance()` (line 4404–4416) parses the `tokenAmount.amount` field from SPL jsonParsed encoding. This field is the raw integer in smallest units. For a 6-decimal pump.fun SPL token: `wallet_raw = whole_tokens × 10^6`.

**The unit mismatch:**
```
actual_price = actual_proceeds_usd / raw_to_sell
             = USD / (whole_tokens × 10^6)
             = (price_per_whole_token) / 10^6
```

For example, if the position has 1,000 whole tokens and the TP1 sell of 250 tokens yields $20:
- `raw_to_sell` = 250 × 10^6 = 250,000,000 (base units)
- `actual_proceeds_usd` = $20
- `actual_price` = $20 / 250,000,000 = $8 × 10^-8 per base unit

But `entry_px` and all position tracking uses price per **whole token** (e.g., $0.00008 per whole token). So `actual_price` is $8 × 10^-8 — six orders of magnitude too small.

`tr.current_px = $8 × 10^-8` → **recorded as exit_price = 0.000000** ✓ matches user observation.

### How this causes -106% on subsequent RAPID_DUMP_EXIT

**Chain (for positions with at least one successful partial sell):**

```
Step 1: TP1 sell succeeds
        actual_price = actual_proceeds_usd / raw_to_sell ≈ 0 (base unit division)
        tr.current_px = ≈ 0
        tr.last_price  = ≈ 0

Step 2: _refresh_trade_mark() may run and restore correct current_px
        — but if it fails (timeout, rate limit) or runs AFTER step 3:

Step 3: RAPID_DUMP_EXIT check on next monitor_positions_loop tick
        tr.prev_px = old mark (e.g. $0.00008)  ← set in prior tick
        tr.current_px ≈ 0                       ← from step 1 unit mismatch
        recent_drop = ($0.00008 - 0) / $0.00008 = 100%
        → RAPID_DUMP_EXIT fires (even if real market hasn't moved)

Step 4: _sell_fraction() called with tag="RAPID DUMP EXIT"
        _live_sell_gate(): is_stop_exit=True
        Attempts real on-chain sell

Step 5a: Sell succeeds, out_lamports > 0
         actual_proceeds_usd = (out_lamports / 1e9) × sol_usd   (real USD)
         proceeds = actual_proceeds_usd   (correct)
         pnl = proceeds - basis - fees   (correct)
         → ROI ≈ -16.6% (real loss)     — NOT -106% in this path

Step 5b: Sell succeeds, but out_lamports = 0
         (Jupiter quote returned outAmount=0 on extreme illiquidity or rapid dump)
         actual_proceeds_usd = None
         returns (True, None) from _live_sell_gate()
         In _sell_fraction:
           proceeds_override = None
           proceeds = qty × tr.current_px ≈ 0    ← current_px still near 0
           pnl = 0 - basis_sold - fee_sold
               ≈ 0 - $100 - $6 = -$106
         ROI = pnl / cost = -$106 / $100 = -106%  ← confirmed path

Step 5c: Sell fails (tx_verification_timeout)
         returns False → _sell_fraction returns early, no PnL recorded
         → NOT -106% in this path, position remains open
```

**Step 5b is the exact path to -106.06%**:
- `out_lamports = 0` (Jupiter `outAmount = 0` on the sell quote — token too illiquid to route, or severe dump to 0 liquidity)
- `actual_proceeds_usd = None`
- `proceeds = qty × tr.current_px` where `tr.current_px ≈ 0` from unit mismatch in prior sell
- `pnl = 0 - ~$100 - ~$6 = -$106`
- `ROI = -$106 / $100 = -106%`

**The exact -106.06% figure:**
- Cost basis ≈ $100 (size_usd)
- Fees (buy + sell) ≈ $6.06
- pnl = 0 - 100 - 6.06 = -106.06
- ROI = -106.06 / 100 = -106.06%

### Q4 supplemental: is `proceeds_usd` going negative?

No. `tr.proceeds_usd += proceeds` where `proceeds = qty × tr.current_px ≈ 0` → adds zero, not negative. `proceeds_usd` cannot go negative from this path.

### Q4 supplemental: is `fees_usd` larger than `proceeds_usd`?

Yes, this is the mechanism. When `proceeds ≈ 0`:
- `pnl = proceeds - basis_sold - fee_sold = 0 - $100 - $6 = -$106`
- The fee component ($6.06) explains why ROI is -106% and not -100%.

### Q4 supplemental: wrong qty units?

Not the primary path. `qty` in `_sell_fraction` is `frac × tr.qty_total` in WHOLE tokens (consistent with how positions are tracked). The unit mismatch is in `raw_to_sell / actual_price`, not in `qty`.

---

## Confirmed Bugs Summary

| # | Location | Bug | Effect |
|---|---|---|---|
| **A** | `simulation_mode.py:4635` | `actual_price = actual_proceeds_usd / raw_to_sell` where `raw_to_sell` is in base units (10^6× whole tokens) | Sets `tr.current_px` and `tr.last_price` to `price × 10^-6` after every successful sell. Causes cascade: exit_price=0.000000, false RAPID_DUMP_EXIT, -106% ROI |
| **B** | `simulation_mode.py:3042` | `_token_price()` (Jupiter Price API v2) used as fallback when exec mark fails — returns pump.fun bonding curve price for recently migrated tokens | HUD shows +133% ROI; TP triggers at wrong price; real losses not detected; HARD_CAP_EXIT suppressed |
| **C** | `simulation_mode.py:4711` | When `out_lamports=0` and `proceeds_override=None`, `proceeds = qty × tr.current_px` — if `current_px ≈ 0` from bug A, proceeds = 0 | `pnl = 0 - basis - fees = -$106`, ROI = -106% |

---

## Suggested Fixes (NOT applied — audit only)

### Fix A — Correct the `actual_price` unit mismatch (line 4635)

```python
# WRONG (current):
actual_price = actual_proceeds_usd / raw_to_sell

# RIGHT: raw_to_sell is in base units; divide by whole-token count instead
_TOKEN_DECIMALS = 6  # pump.fun standard
qty_whole_tokens = raw_to_sell / (10 ** _TOKEN_DECIMALS)
actual_price = actual_proceeds_usd / qty_whole_tokens if qty_whole_tokens > 0 else 0.0
```

### Fix B — Remove Jupiter Price API v2 from the mark fallback chain

Replace `_token_price()` (Jupiter Price API v2, pump.fun-contaminated) with `_dexscreener_price()` as the first non-exec fallback. The Raydium filter in `_dexscreener_price()` already protects against the pump.fun price. The mark chain becomes:
```
Exec quote → DexScreener (Raydium-preferred) → None (stale)
```
If both fail, accept stale mark rather than serving a wrong price.

### Fix C — Guard `proceeds` fallback against near-zero `current_px`

When `proceeds_override=None` (sell succeeded but no lamport amount returned), validate `tr.current_px` is within a reasonable range of `tr.entry_px` before using it as proceeds estimate. If current_px < entry_px × 0.01 (99%+ below entry), flag as suspicious and block the close until a fresh mark is obtained.
