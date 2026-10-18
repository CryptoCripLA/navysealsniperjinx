#!/usr/bin/env python3
"""
exit_lab.py — Counterfactual exit strategy simulator.

Reads history.json trade_close events, reconstructs each trade's price envelope
(entry_px, high_px, low_px, exit_px), and simulates what different exit rules
would have returned.

LIMITATION: We only have summary stats per trade (MFE, MAE, hold_time, exit ROI),
not tick-by-tick marks. Counterfactuals therefore model the *best possible* exit
for each rule given the observed extremes. This overstates strategy returns
(a -6% trail from a +40% MFE assumes the trail held through the entire move).
Use relative ranking between strategies, not absolute P&L, for decisions.

Usage:
    python3 exit_lab.py             # all trades with MFE > 0
    python3 exit_lab.py --n 50      # last 50 closes
    python3 exit_lab.py --all       # all closes including MFE=0
"""

import argparse
import json
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

BASE = Path(__file__).parent
HISTORY_FILE = BASE / "history.json"

# ── ANSI ──────────────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def _c(text, colour):
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else str(text)


# ── Trade envelope ────────────────────────────────────────────────────────────
@dataclass
class TradeEnvelope:
    mint: str
    entry_px: float
    exit_px: float
    high_px: float
    low_px: float
    mfe_pct: float          # P4.9: sanity-capped
    mae_pct: float
    roi_pct: float
    hold_s: float
    fees_usd: float
    size_usd: float
    close_reason: str
    pnl_usd: float
    mfe_pct_raw: float = 0.0   # P4.9: raw pre-cap value
    mfe_capped:  bool  = False  # P4.9: True iff cap fired — exclude from averages


def load_envelopes(n: Optional[int], include_zero_mfe: bool) -> list[TradeEnvelope]:
    if not HISTORY_FILE.exists():
        sys.exit(f"history.json not found at {HISTORY_FILE}")
    history = json.loads(HISTORY_FILE.read_text())
    closes = [ev for ev in history if ev.get("type") == "trade_close"]
    closes.sort(key=lambda ev: float(ev.get("ts") or 0))

    if n is not None:
        closes = closes[-n:]

    # P4.9: preserve raw MFE + flag when the sanity cap fires, so capped
    # records can be filtered out of averages rather than silently pulled down.
    from constants import cap_mfe as _cap_mfe
    envelopes = []
    for c in closes:
        _raw = float(c.get("mfe_pct") or 0)
        mfe, was_capped = _cap_mfe(_raw)
        mae = float(c.get("mae_pct") or 0)
        if not include_zero_mfe and mfe <= 0:
            continue

        basis = float(c.get("realized_basis_usd") or c.get("size_usd") or 0)
        qty = float(c.get("qty_closed") or 0)
        if qty <= 0 or basis <= 0:
            continue
        entry_px = basis / qty
        exit_px = float(c.get("exit_price") or 0) or entry_px
        high_px = entry_px * (1 + mfe / 100.0)
        low_px = entry_px * (1 + mae / 100.0)  # mae is negative
        fees = float(c.get("fees_usd") or 0)
        size = float(c.get("size_usd") or basis)

        envelopes.append(TradeEnvelope(
            mint=c.get("mint", "?"),
            entry_px=entry_px,
            exit_px=exit_px,
            high_px=high_px,
            low_px=low_px,
            mfe_pct=mfe,
            mae_pct=mae,
            roi_pct=float(c.get("roi_pct") or 0),
            hold_s=float(c.get("hold_time_seconds") or 0),
            fees_usd=fees,
            size_usd=size,
            close_reason=c.get("close_reason", "unknown"),
            pnl_usd=float(c.get("pnl_usd") or 0),
            mfe_pct_raw=abs(_raw),   # P4.9
            mfe_capped=was_capped,   # P4.9
        ))

    return envelopes


# ── Strategy engine ───────────────────────────────────────────────────────────
# Each strategy returns a counterfactual exit ROI% given the trade envelope.
# The exit ROI is what we *would have* captured, subject to the limitation that
# we only know the MFE/MAE extremes, not the path. We model the best-case exit
# for each rule: if MFE reached the trail's activation threshold, the trail's
# stop price is the counterfactual exit.

def _roi_from_exit_pct(exit_pct: float, fees_pct: float) -> float:
    """Convert an exit % (relative to entry) to ROI after fees."""
    return exit_pct - fees_pct


def _counterfactual_roi(env: TradeEnvelope, exit_px: float) -> float:
    """Compute ROI% from a counterfactual exit price."""
    if env.entry_px <= 0:
        return 0.0
    gross_roi = (exit_px - env.entry_px) / env.entry_px * 100.0
    fee_pct = (env.fees_usd / env.size_usd * 100.0) if env.size_usd > 0 else 0.0
    return gross_roi - fee_pct


def strategy_baseline(env: TradeEnvelope) -> float:
    """As-traded: actual ROI from the close event."""
    return env.roi_pct


def strategy_fixed_trail_6(env: TradeEnvelope) -> float:
    """Fixed -6% trail from MFE peak. Activates at any MFE > 0."""
    if env.mfe_pct <= 0:
        return env.roi_pct
    # Trail stop = high_px * 0.94
    trail_exit_px = env.high_px * 0.94
    # Did MAE breach the trail? If low_px < trail_exit_px, the trail would
    # have fired at trail_exit_px. Otherwise we exited at the trail level.
    # Since we don't know the path order (MFE first or MAE first), we assume
    # the best case: MFE was reached, then price retraced to the trail stop.
    cf_exit_px = trail_exit_px
    return _counterfactual_roi(env, cf_exit_px)


def strategy_fixed_trail_8(env: TradeEnvelope) -> float:
    """Fixed -8% trail from MFE peak."""
    if env.mfe_pct <= 0:
        return env.roi_pct
    trail_exit_px = env.high_px * 0.92
    return _counterfactual_roi(env, trail_exit_px)


def strategy_tiered_trail(env: TradeEnvelope) -> float:
    """
    Tiered trail matching the proposed code change:
      MFE < 20%:  no trail (use initial stop = entry * 0.85 = -15%)
      MFE 20-50%: trail at high * 0.92 (-8%)
      MFE > 50%:  trail at high * 0.94 (-6%)
    """
    if env.mfe_pct < 20.0:
        # No trail fires. Best-case exit is actual exit (no trail improvement).
        # Worst case: initial stop at -15%.
        return env.roi_pct
    elif env.mfe_pct < 50.0:
        trail_exit_px = env.high_px * 0.92
    else:
        trail_exit_px = env.high_px * 0.94

    cf_roi = _counterfactual_roi(env, trail_exit_px)
    # The trail can only improve over baseline (it locks gains); it can't make
    # a losing trade worse since the initial stop still applies.
    return cf_roi


def strategy_time_tighten(env: TradeEnvelope) -> float:
    """
    Time-based tightening:
      First 60s: -10% trail from MFE
      After 60s: -6% trail from MFE
    """
    if env.mfe_pct <= 0:
        return env.roi_pct
    if env.hold_s <= 60.0:
        trail_exit_px = env.high_px * 0.90  # -10%
    else:
        trail_exit_px = env.high_px * 0.94  # -6%
    return _counterfactual_roi(env, trail_exit_px)


def strategy_partial_tp_trail(env: TradeEnvelope) -> float:
    """
    Partial TP at +20% (sell 30%), then -6% trail on remaining 70%.

    If MFE >= 20%:
      - 30% of position exits at entry_px * 1.20
      - 70% of position exits at high_px * 0.94 (trail from peak)
      - Blended ROI
    If MFE < 20%:
      - No partial fires, pure -6% trail on full position
    """
    if env.mfe_pct <= 0:
        return env.roi_pct

    fee_pct = (env.fees_usd / env.size_usd * 100.0) if env.size_usd > 0 else 0.0

    if env.mfe_pct >= 20.0:
        # Partial exit at +20%
        partial_roi = 20.0 - fee_pct * 0.30  # fee proportional to fraction
        # Trail on remainder
        trail_exit_px = env.high_px * 0.94
        remainder_gross = (trail_exit_px - env.entry_px) / env.entry_px * 100.0 if env.entry_px > 0 else 0.0
        remainder_roi = remainder_gross - fee_pct * 0.70
        # Blended
        return 0.30 * partial_roi + 0.70 * remainder_roi
    else:
        # No partial, just -6% trail
        trail_exit_px = env.high_px * 0.94
        return _counterfactual_roi(env, trail_exit_px)


# ── Strategy registry ─────────────────────────────────────────────────────────
STRATEGIES = [
    ("Baseline (as-traded)",           strategy_baseline),
    ("Tiered trail (20/50% MFE)",      strategy_tiered_trail),
    ("Fixed -6% trail",                strategy_fixed_trail_6),
    ("Fixed -8% trail",                strategy_fixed_trail_8),
    ("Time tighten (-10%/60s→-6%)",    strategy_time_tighten),
    ("Partial TP@+20% + -6% trail",    strategy_partial_tp_trail),
]


# ── Stats ─────────────────────────────────────────────────────────────────────
@dataclass
class StrategyResult:
    name: str
    rois: list[float] = field(default_factory=list)

    @property
    def n(self): return len(self.rois)

    @property
    def wins(self): return sum(1 for r in self.rois if r > 0)

    @property
    def win_rate(self): return (self.wins / self.n * 100.0) if self.n else 0.0

    @property
    def avg_roi(self): return (sum(self.rois) / self.n) if self.n else 0.0

    @property
    def total_pnl_approx(self):
        """Approximate total P&L assuming uniform position size."""
        # Use average size_usd from envelopes; will be set externally
        return sum(r / 100.0 * self._avg_size for r in self.rois)

    _avg_size: float = 0.5  # default, overridden at runtime


def _mfe_capture(rois: list[float], mfes: list[float]) -> float:
    """What fraction of available MFE was captured on average."""
    if not rois or not mfes:
        return 0.0
    captures = []
    for roi, mfe in zip(rois, mfes):
        if mfe > 0:
            captures.append(max(0.0, roi) / mfe)
    return (sum(captures) / len(captures) * 100.0) if captures else 0.0


# ── Display ───────────────────────────────────────────────────────────────────
def fmt_pnl(v: float) -> str:
    s = f"${v:+.2f}"
    return _c(s, GREEN if v >= 0 else RED)

def fmt_roi(v: float) -> str:
    s = f"{v:+.1f}%"
    return _c(s, GREEN if v >= 0 else RED)

def fmt_pct(v: float) -> str:
    return f"{v:.1f}%"

def section(title: str):
    print()
    print(_c(f"  {title}", BOLD))
    print("  " + "─" * 60)


def run_analysis(envelopes: list[TradeEnvelope], label: str):
    if not envelopes:
        print(f"  No trades to analyse for {label}.")
        return

    avg_size = sum(e.size_usd for e in envelopes) / len(envelopes)
    mfes = [e.mfe_pct for e in envelopes]

    results: list[StrategyResult] = []
    for name, fn in STRATEGIES:
        sr = StrategyResult(name=name)
        sr._avg_size = avg_size
        for env in envelopes:
            sr.rois.append(fn(env))
        results.append(sr)

    # Header
    section(f"{label}  ({len(envelopes)} trades, avg size ${avg_size:.2f})")

    col_name = 36
    header = (f"  {'Strategy':<{col_name}}  {'Win%':>6}  {'Avg ROI':>8}  "
              f"{'~P&L':>8}  {'MFE cap':>7}  {'vs base':>8}")
    print(header)
    print("  " + "·" * (len(header) - 2))

    baseline_pnl = results[0].total_pnl_approx if results else 0.0
    baseline_roi = results[0].avg_roi if results else 0.0

    for sr in results:
        cap = _mfe_capture(sr.rois, mfes)
        delta = sr.total_pnl_approx - baseline_pnl
        delta_str = fmt_pnl(delta) if sr.name != results[0].name else _c("  —", DIM)
        print(
            f"  {sr.name:<{col_name}}  {fmt_pct(sr.win_rate):>6}  "
            f"{fmt_roi(sr.avg_roi):>8}  {fmt_pnl(sr.total_pnl_approx):>8}  "
            f"{fmt_pct(cap):>7}  {delta_str:>8}"
        )

    # Best strategy
    best = max(results, key=lambda s: s.total_pnl_approx)
    if best.name != results[0].name:
        improvement = best.total_pnl_approx - baseline_pnl
        print()
        print(f"  Best: {_c(best.name, GREEN+BOLD)}  "
              f"({fmt_pnl(improvement)} vs baseline)")
    else:
        print()
        print(f"  Best: {_c('Baseline (current strategy wins)', YELLOW+BOLD)}")

    return results


def print_big_mfe_breakdown(envelopes: list[TradeEnvelope]):
    """Show how each strategy handles trades with MFE > 20% specifically."""
    big = [e for e in envelopes if e.mfe_pct >= 20.0]
    if not big:
        return

    section(f"TRADES WITH MFE >= 20%  ({len(big)} trades)")

    avg_size = sum(e.size_usd for e in big) / len(big)
    mfes = [e.mfe_pct for e in big]

    col_name = 36
    header = (f"  {'Strategy':<{col_name}}  {'Win%':>6}  {'Avg ROI':>8}  "
              f"{'~P&L':>8}  {'MFE cap':>7}")
    print(header)
    print("  " + "·" * (len(header) - 2))

    for name, fn in STRATEGIES:
        rois = [fn(e) for e in big]
        wins = sum(1 for r in rois if r > 0)
        wr = wins / len(rois) * 100
        avg_r = sum(rois) / len(rois)
        approx_pnl = sum(r / 100.0 * avg_size for r in rois)
        cap = _mfe_capture(rois, mfes)
        print(f"  {name:<{col_name}}  {fmt_pct(wr):>6}  "
              f"{fmt_roi(avg_r):>8}  {fmt_pnl(approx_pnl):>8}  {fmt_pct(cap):>7}")

    # Show individual trades with biggest give-backs
    print()
    print("  Worst MFE give-backs (MFE ≥ 20%, sorted by actual ROI):")
    givebacks = sorted(big, key=lambda e: e.roi_pct)[:5]
    for e in givebacks:
        tiered = strategy_tiered_trail(e)
        print(f"    {e.mint[:12]}  MFE={e.mfe_pct:+.0f}%  actual={fmt_roi(e.roi_pct)}  "
              f"tiered={fmt_roi(tiered)}  hold={e.hold_s:.0f}s  {e.close_reason}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Counterfactual exit strategy simulator")
    parser.add_argument("--n", type=int, default=None, metavar="N",
                        help="Number of most-recent closes to analyse")
    parser.add_argument("--all", action="store_true",
                        help="Include trades with MFE = 0 (rugs, ghosts)")
    args = parser.parse_args()

    envelopes = load_envelopes(args.n, include_zero_mfe=args.all)
    if not envelopes:
        sys.exit("No trades found matching criteria.")

    print()
    print(_c("  EXIT LAB — COUNTERFACTUAL ANALYSIS", BOLD))
    print(_c("  NOTE: MFE-based counterfactuals assume best-case path ordering.", DIM))
    print(_c("  Use relative ranking between strategies, not absolute P&L.", DIM))

    # Full dataset
    run_analysis(envelopes, "ALL QUALIFYING TRADES")

    # Last 20 (recent market conditions)
    recent = envelopes[-20:]
    run_analysis(recent, "LAST 20 TRADES (recent conditions)")

    # Big-MFE breakdown
    print_big_mfe_breakdown(envelopes)

    print()


if __name__ == "__main__":
    main()
