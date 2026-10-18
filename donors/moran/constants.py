"""
constants.py - Shared constants for the sniperbot.

P4.1: Single source of truth for exit-ladder constants. simulation_mode.py,
replay_v2.py, replay_analyzer.py, and replay_experiment_runner.py all import
from here so live behavior and post-hoc analysis cannot drift.
"""

import os

# Paper trading starting budget (USD).
MAX_TEST_BUDGET_USD = float(os.getenv("MAX_TEST_BUDGET_USD", "100.0"))

# Legacy stop-loss constant (deprecated; use DEFAULT_DOWNSIDE_STOP_PCT instead).
STOP_LOSS = float(os.getenv("STOP_LOSS", "0.12"))

# ── Exit ladder (SSOT) ───────────────────────────────────────────────────────
# Initial trailing-stop distance from entry, pre-stage1.
DEFAULT_DOWNSIDE_STOP_PCT = float(os.getenv("DEFAULT_DOWNSIDE_STOP_PCT", "0.12"))
# Hard cap on unrealized loss — force-sell above this.
MAX_POSITION_LOSS_PCT     = float(os.getenv("MAX_POSITION_LOSS_PCT",   "0.20"))
# Trail distance below MFE peak once stage1 fires.
TRAIL_FROM_MFE_PCT        = float(os.getenv("TRAIL_FROM_MFE_PCT",      "0.06"))
# ROI level at which to tighten stop to breakeven (pre-stage1 only).
BREAKEVEN_TRIGGER_PCT     = float(os.getenv("BREAKEVEN_TRIGGER_PCT",   "0.15"))

# TP ladder: ROI threshold → fraction of remaining qty to sell.
TP1_PCT  = float(os.getenv("TP1_PCT",  "0.20"))
TP1_SELL = float(os.getenv("TP1_SELL", "0.30"))
TP2_PCT  = float(os.getenv("TP2_PCT",  "0.40"))
TP2_SELL = float(os.getenv("TP2_SELL", "0.30"))
TP3_PCT  = float(os.getenv("TP3_PCT",  "2.00"))
TP3_SELL = float(os.getenv("TP3_SELL", "0.20"))

# Analysis-only alias (several replay scripts use the shorter name).
DEFAULT_STOP_PCT = DEFAULT_DOWNSIDE_STOP_PCT
FAILSAFE_PCT     = MAX_POSITION_LOSS_PCT


# ── Canonical win definition (P4.2 SSOT) ─────────────────────────────────────
# A trade is a "win" iff realized pnl_usd is strictly positive (after fees).
# All reporting — HUD, scorecard, replay, feature_builder — must use this.
# feature_builder may still emit an additional "big_win_label" for ML training
# (e.g. roi_pct > 10), but that is a distinct ML-target column, not a win rate.
def is_win(close_event: dict) -> bool:
    """True iff close_event represents a realized win (pnl_usd > 0 after fees)."""
    try:
        return float(close_event.get("pnl_usd") or 0.0) > 0.0
    except (TypeError, ValueError):
        return False


# ── Clopper–Pearson 95% CI (P4.5) ────────────────────────────────────────────
# Exact binomial CI. At n=30 a win rate of 50% has CI ~[31%, 69%] — wide enough
# to show that short-sample win-rate comparisons are mostly noise.
def clopper_pearson_ci(k: int, n: int, alpha: float = 0.05) -> tuple:
    """Return (lo, hi) for a Binomial(k/n) at confidence 1-alpha. Both as fractions."""
    if n <= 0:
        return (0.0, 0.0)
    k = max(0, min(int(k), int(n)))
    try:
        from scipy.stats import beta
    except ImportError:
        # Graceful fallback: Wilson score (less exact, but stdlib-only).
        import math as _m
        if n == 0:
            return (0.0, 0.0)
        z = 1.959963984540054   # 95% normal quantile
        p = k / n
        denom = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / denom
        half = (z / denom) * _m.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        return (max(0.0, centre - half), min(1.0, centre + half))
    lo = 0.0 if k == 0 else float(beta.ppf(alpha / 2.0, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(1.0 - alpha / 2.0, k + 1, n - k))
    return (lo, hi)


# ── Contamination filter (P4.6 SSOT) ─────────────────────────────────────────
# A trade is "contaminated" and must be excluded from ALL post-hoc analysis iff:
#   sell_attempt_ts == 0.0  → no sell TX was attempted (wallet_raw=0 at gate / ghost close)
#   proceeds_usd    == 0.0  → no real proceeds recorded (duplicate of ghost)
#   roi_pct         <= -99  → sub-100% ROI = corrupted accounting
# sell_attempt_ts=None (field absent in old records) is NOT contaminated.
# replay_v2.py and replay_analyzer.py must both call this — a prior divergent
# implementation would silently cause win-rate numbers to differ between tools.
def is_contaminated(trade) -> bool:
    """Duck-typed: accepts any object with sell_attempt_ts, proceeds_usd, roi_pct."""
    sat = getattr(trade, "sell_attempt_ts", None)
    if sat == 0.0:
        return True
    pu = getattr(trade, "proceeds_usd", None)
    if pu is not None and pu == 0.0:
        return True
    roi = getattr(trade, "roi_pct", None)
    if roi is not None and roi <= -99.0:
        return True
    return False


# ── Suspect-price constants (P4.7 SSOT) ──────────────────────────────────────
# Entry prices that indicate graduation-curve / reference-price contamination.
# A trade entered at one of these values is using a theoretical estimate, not
# a real DEX fill — the fill_px was unavailable at entry time.
SUSPECT_PRICES = (6.162627048e-05, 1.3e-05)
SUSPECT_TOL    = 1e-4   # within 0.01% relative tolerance counts as suspect

def is_suspect_price(px) -> bool:
    """Shared between replay_v2 and trade_attribution — see SUSPECT_PRICES."""
    try:
        px = float(px)
    except (TypeError, ValueError):
        return False
    if px <= 0:
        return False
    for sp in SUSPECT_PRICES:
        if abs(px - sp) / sp <= SUSPECT_TOL:
            return True
    return False


# ── MFE sanity cap (P4.9) ────────────────────────────────────────────────────
# Pre-fix trades with near-zero entry_px produce MFE in the millions; capping
# prevents a single bad record from pegging averages. Historically the cap was
# silent — now callers receive (capped, was_capped) so `mfe_capped` can be an
# explicit column and bias-aware readers can filter capped rows out.
MFE_SANITY_CAP_PCT = 500.0

def cap_mfe(raw) -> tuple:
    """Return (capped_value, was_capped_bool). raw in pct (e.g. 32.5 = +32.5%)."""
    try:
        v = abs(float(raw or 0.0))
    except (TypeError, ValueError):
        return (0.0, False)
    if v > MFE_SANITY_CAP_PCT:
        return (MFE_SANITY_CAP_PCT, True)
    return (v, False)
