#!/usr/bin/env python3
"""
replay_experiment_runner.py — Grid-search parameter variants against historical trades.

Tests combinations of trail_pct, hard_cap_pct, min_hold_seconds, and be_trigger_pct
against MFE/MAE envelopes from history.json trade_close events. Ranks results by
total P&L and highlights the top 3 combinations.

LIMITATION: Same as exit_lab.py — we only have summary stats per trade (MFE, MAE,
hold_time, exit ROI), not tick-by-tick marks. Counterfactuals model the best-case
exit for each rule given the observed extremes. Use relative ranking between
configurations, not absolute P&L, for decisions.

Usage:
    python3 replay_experiment_runner.py --n 50
    python3 replay_experiment_runner.py --n 50 --param trail_pct
    python3 replay_experiment_runner.py --all
"""

import argparse
import json
import sys
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent
HISTORY_FILE = BASE / "history.json"

_MFE_CAP = 500.0

# ── ANSI ─────────────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def _c(text, colour):
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else str(text)


# ── Parameter grid ───────────────────────────────────────────────────────────
PARAM_GRID = {
    "trail_pct":        [0.04, 0.05, 0.06, 0.07, 0.08],
    "hard_cap_pct":     [0.10, 0.15, 0.20, 0.25],
    "min_hold_seconds": [0, 8, 12, 20],
    "be_trigger_pct":   [0.04, 0.06, 0.08, 0.10],
}

# Current live config baseline — P4.1: pulled from constants.py so it can't drift.
from constants import TRAIL_FROM_MFE_PCT, MAX_POSITION_LOSS_PCT, BREAKEVEN_TRIGGER_PCT

CURRENT = {
    "trail_pct":        TRAIL_FROM_MFE_PCT,
    "hard_cap_pct":     MAX_POSITION_LOSS_PCT,
    "min_hold_seconds": 0,
    "be_trigger_pct":   BREAKEVEN_TRIGGER_PCT,
}


# ── Trade envelope ───────────────────────────────────────────────────────────
@dataclass
class Envelope:
    mint: str
    entry_px: float
    high_px: float
    low_px: float
    mfe_pct: float
    mae_pct: float
    roi_pct: float
    hold_s: float
    fees_usd: float
    size_usd: float
    close_reason: str
    pnl_usd: float


def load_envelopes(n: Optional[int]) -> list[Envelope]:
    if not HISTORY_FILE.exists():
        sys.exit(f"history.json not found at {HISTORY_FILE}")
    history = json.loads(HISTORY_FILE.read_text())
    closes = [ev for ev in history if ev.get("type") == "trade_close"]
    closes.sort(key=lambda ev: float(ev.get("ts") or 0))
    if n is not None:
        closes = closes[-n:]

    envelopes = []
    for c in closes:
        mfe = min(abs(float(c.get("mfe_pct") or 0)), _MFE_CAP)
        mae = float(c.get("mae_pct") or 0)
        basis = float(c.get("realized_basis_usd") or c.get("size_usd") or 0)
        qty = float(c.get("qty_closed") or 0)
        if qty <= 0 or basis <= 0:
            continue
        entry_px = basis / qty
        high_px = entry_px * (1 + mfe / 100.0)
        low_px = entry_px * (1 + mae / 100.0)
        envelopes.append(Envelope(
            mint=c.get("mint", "?"),
            entry_px=entry_px,
            high_px=high_px,
            low_px=low_px,
            mfe_pct=mfe,
            mae_pct=mae,
            roi_pct=float(c.get("roi_pct") or 0),
            hold_s=float(c.get("hold_time_seconds") or 0),
            fees_usd=float(c.get("fees_usd") or 0),
            size_usd=float(c.get("size_usd") or basis),
            close_reason=c.get("close_reason", "unknown"),
            pnl_usd=float(c.get("pnl_usd") or 0),
        ))
    return envelopes


# ── Counterfactual simulation ────────────────────────────────────────────────
def simulate_trade(env: Envelope, trail_pct: float, hard_cap_pct: float,
                   min_hold_seconds: float, be_trigger_pct: float) -> tuple[float, str]:
    """Simulate a single trade under given parameters.

    Returns (counterfactual_roi_pct, exit_reason).

    Logic order (same priority as live bot):
    1. Hard cap: if MAE breaches -hard_cap_pct, exit at -hard_cap_pct
    2. Breakeven: if MFE >= be_trigger_pct, floor at 0% (breakeven)
    3. Trail: if MFE > trail_pct and hold_s >= min_hold_seconds,
       exit at high * (1 - trail_pct)
    4. Otherwise: use actual exit ROI (baseline)
    """
    fee_pct = (env.fees_usd / env.size_usd * 100.0) if env.size_usd > 0 else 0.0

    # Hard cap: if drawdown from entry exceeds hard_cap_pct, forced exit
    if env.mae_pct <= -(hard_cap_pct * 100):
        return -(hard_cap_pct * 100) - fee_pct, "HARD_CAP"

    # Breakeven stop: if MFE reached be_trigger, floor exit at breakeven (0% gross)
    if env.mfe_pct >= be_trigger_pct * 100:
        # Breakeven means we won't exit below 0% gross.
        # If the actual exit was negative, the BE stop would have caught it at ~0.
        # If actual exit was positive, trail or actual exit applies (whichever is better).
        floor_roi = -fee_pct  # breakeven = 0% gross minus fees

        # Trail from MFE peak
        if env.mfe_pct > trail_pct * 100 and env.hold_s >= min_hold_seconds:
            trail_exit_px = env.high_px * (1 - trail_pct)
            trail_roi = ((trail_exit_px - env.entry_px) / env.entry_px * 100.0 - fee_pct) if env.entry_px > 0 else 0.0
            return max(trail_roi, floor_roi), "TRAIL"

        # BE fired but trail didn't activate — exit at breakeven floor
        if env.roi_pct < floor_roi:
            return floor_roi, "BREAKEVEN"
        return env.roi_pct, "ACTUAL"

    # Trail without breakeven
    if env.mfe_pct > trail_pct * 100 and env.hold_s >= min_hold_seconds:
        trail_exit_px = env.high_px * (1 - trail_pct)
        trail_roi = ((trail_exit_px - env.entry_px) / env.entry_px * 100.0 - fee_pct) if env.entry_px > 0 else 0.0
        return trail_roi, "TRAIL"

    # No rule triggered — use actual outcome
    return env.roi_pct, "ACTUAL"


@dataclass
class RunResult:
    trail_pct: float
    hard_cap_pct: float
    min_hold_seconds: float
    be_trigger_pct: float
    total_pnl: float
    win_rate: float
    avg_roi: float
    hard_cap_count: int
    trades: int
    wins: int
    is_current: bool = False


def run_config(envelopes: list[Envelope], trail_pct: float, hard_cap_pct: float,
               min_hold_seconds: float, be_trigger_pct: float) -> RunResult:
    rois = []
    hard_cap_count = 0
    for env in envelopes:
        roi, reason = simulate_trade(env, trail_pct, hard_cap_pct, min_hold_seconds, be_trigger_pct)
        rois.append(roi)
        if reason == "HARD_CAP":
            hard_cap_count += 1

    avg_size = sum(e.size_usd for e in envelopes) / len(envelopes) if envelopes else 1.0
    pnls = [r / 100.0 * avg_size for r in rois]
    wins = sum(1 for r in rois if r > 0)

    return RunResult(
        trail_pct=trail_pct,
        hard_cap_pct=hard_cap_pct,
        min_hold_seconds=min_hold_seconds,
        be_trigger_pct=be_trigger_pct,
        total_pnl=sum(pnls),
        win_rate=(wins / len(rois) * 100) if rois else 0.0,
        avg_roi=(sum(rois) / len(rois)) if rois else 0.0,
        hard_cap_count=hard_cap_count,
        trades=len(rois),
        wins=wins,
        is_current=(trail_pct == CURRENT["trail_pct"]
                    and hard_cap_pct == CURRENT["hard_cap_pct"]
                    and min_hold_seconds == CURRENT["min_hold_seconds"]
                    and be_trigger_pct == CURRENT["be_trigger_pct"]),
    )


# ── Display ──────────────────────────────────────────────────────────────────
def fmt_pnl(v: float) -> str:
    s = f"${v:+.4f}"
    return _c(s, GREEN if v >= 0 else RED)

def fmt_roi(v: float) -> str:
    s = f"{v:+.1f}%"
    return _c(s, GREEN if v >= 0 else RED)

def fmt_wr(v: float) -> str:
    s = f"{v:.1f}%"
    return _c(s, GREEN if v >= 35 else RED)


def print_results(results: list[RunResult], param_filter: Optional[str], n_trades: int):
    # Sort by total P&L descending
    results.sort(key=lambda r: r.total_pnl, reverse=True)

    # Find baseline
    baseline = next((r for r in results if r.is_current), None)
    baseline_pnl = baseline.total_pnl if baseline else 0.0

    # If param filter, show which param is being varied
    if param_filter:
        print()
        print(_c(f"  PARAMETER SWEEP: {param_filter.upper()}", BOLD))
        print(f"  Other params fixed at current: trail={CURRENT['trail_pct']}, "
              f"cap={CURRENT['hard_cap_pct']}, hold={CURRENT['min_hold_seconds']}s, "
              f"be={CURRENT['be_trigger_pct']}")
    else:
        total_combos = len(results)
        print()
        print(_c(f"  FULL GRID SEARCH — {total_combos} COMBINATIONS", BOLD))

    print(f"  {n_trades} trades analysed")
    print()

    # Header
    col_w = 8
    header = (f"  {'#':>3}  {'Trail':>{col_w}} {'HCap':>{col_w}} {'Hold':>{col_w}} "
              f"{'BE':>{col_w}}  {'WR':>7}  {'Avg ROI':>8}  {'P&L':>11}  "
              f"{'vs Base':>11}  {'HC#':>4}  {'HC%':>6}  Note")
    print(header)
    print("  " + "." * (len(header) + 10))

    # Show top 20 + baseline (if not in top 20)
    show_limit = min(20, len(results))
    shown_baseline = False

    for i, r in enumerate(results[:show_limit]):
        rank = i + 1
        delta = r.total_pnl - baseline_pnl
        hc_pct = (r.hard_cap_count / r.trades * 100) if r.trades > 0 else 0

        notes = []
        if rank <= 3:
            notes.append(_c("TOP", GREEN + BOLD))
        if r.is_current:
            notes.append(_c("CURRENT", YELLOW + BOLD))
            shown_baseline = True

        marker = _c(f"{rank:>3}", GREEN + BOLD) if rank <= 3 else f"{rank:>3}"
        delta_str = fmt_pnl(delta) if not r.is_current else _c("  baseline", DIM)
        hc_colour = RED if hc_pct > 20 else YELLOW if hc_pct > 10 else GREEN

        print(
            f"  {marker}  {r.trail_pct:>{col_w}.2f} {r.hard_cap_pct:>{col_w}.2f} "
            f"{r.min_hold_seconds:>{col_w}.0f} {r.be_trigger_pct:>{col_w}.2f}  "
            f"{fmt_wr(r.win_rate):>7}  {fmt_roi(r.avg_roi):>8}  {fmt_pnl(r.total_pnl):>11}  "
            f"{delta_str:>11}  {r.hard_cap_count:>4}  "
            f"{_c(f'{hc_pct:.0f}%', hc_colour):>6}  {' '.join(notes)}"
        )

    # If baseline wasn't in top 20, show it separately
    if not shown_baseline and baseline:
        rank = next(i + 1 for i, r in enumerate(results) if r.is_current)
        hc_pct = (baseline.hard_cap_count / baseline.trades * 100) if baseline.trades > 0 else 0
        hc_colour = RED if hc_pct > 20 else YELLOW if hc_pct > 10 else GREEN
        print("  " + "." * 10)
        print(
            f"  {rank:>3}  {baseline.trail_pct:>{col_w}.2f} {baseline.hard_cap_pct:>{col_w}.2f} "
            f"{baseline.min_hold_seconds:>{col_w}.0f} {baseline.be_trigger_pct:>{col_w}.2f}  "
            f"{fmt_wr(baseline.win_rate):>7}  {fmt_roi(baseline.avg_roi):>8}  {fmt_pnl(baseline.total_pnl):>11}  "
            f"{_c('  baseline', DIM):>11}  {baseline.hard_cap_count:>4}  "
            f"{_c(f'{hc_pct:.0f}%', hc_colour):>6}  {_c('CURRENT', YELLOW + BOLD)}"
        )

    # Summary
    print()
    print(_c("  SUMMARY", BOLD))
    print("  " + "-" * 60)
    if baseline:
        print(f"  Current config rank: {next(i + 1 for i, r in enumerate(results) if r.is_current)} / {len(results)}")
        print(f"  Current P&L: {fmt_pnl(baseline.total_pnl)}  |  WR: {fmt_wr(baseline.win_rate)}  |  HC: {baseline.hard_cap_count}")

    best = results[0]
    if not best.is_current:
        delta = best.total_pnl - baseline_pnl
        print(f"  Best config: trail={best.trail_pct} cap={best.hard_cap_pct} "
              f"hold={best.min_hold_seconds:.0f}s be={best.be_trigger_pct}")
        print(f"  Best P&L: {fmt_pnl(best.total_pnl)}  ({fmt_pnl(delta)} vs current)")
    else:
        print(f"  {_c('Current config is already optimal for this dataset', GREEN + BOLD)}")

    # Top 3 recommendations
    top3 = [r for r in results[:3] if not r.is_current]
    if top3:
        print()
        print(_c("  CANDIDATES TO TEST", BOLD))
        for i, r in enumerate(top3):
            delta = r.total_pnl - baseline_pnl
            changes = []
            if r.trail_pct != CURRENT["trail_pct"]:
                changes.append(f"trail {CURRENT['trail_pct']}→{r.trail_pct}")
            if r.hard_cap_pct != CURRENT["hard_cap_pct"]:
                changes.append(f"cap {CURRENT['hard_cap_pct']}→{r.hard_cap_pct}")
            if r.min_hold_seconds != CURRENT["min_hold_seconds"]:
                changes.append(f"hold {CURRENT['min_hold_seconds']}→{r.min_hold_seconds:.0f}s")
            if r.be_trigger_pct != CURRENT["be_trigger_pct"]:
                changes.append(f"be {CURRENT['be_trigger_pct']}→{r.be_trigger_pct}")
            print(f"    {i+1}. {', '.join(changes)}  →  {fmt_pnl(delta)} improvement")

    print()


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Replay experiment runner — grid-search parameter variants")
    parser.add_argument("--n", type=int, default=50, metavar="N",
                        help="Number of most-recent closes to analyse (default: 50)")
    parser.add_argument("--all", action="store_true",
                        help="Analyse all closes (overrides --n)")
    parser.add_argument("--param", type=str, default=None,
                        choices=list(PARAM_GRID.keys()),
                        help="Sweep only this parameter (others fixed at current)")
    args = parser.parse_args()

    n = None if args.all else args.n
    envelopes = load_envelopes(n)
    if not envelopes:
        sys.exit("No valid trade envelopes found in history.json")

    n_trades = len(envelopes)

    if args.param:
        # Single parameter sweep — fix others at current
        results = []
        for val in PARAM_GRID[args.param]:
            params = dict(CURRENT)
            params[args.param] = val
            results.append(run_config(envelopes, **params))
    else:
        # Full grid search
        results = []
        combos = list(product(
            PARAM_GRID["trail_pct"],
            PARAM_GRID["hard_cap_pct"],
            PARAM_GRID["min_hold_seconds"],
            PARAM_GRID["be_trigger_pct"],
        ))
        for trail, cap, hold, be in combos:
            results.append(run_config(envelopes, trail, cap, hold, be))

    print_results(results, args.param, n_trades)


if __name__ == "__main__":
    main()
