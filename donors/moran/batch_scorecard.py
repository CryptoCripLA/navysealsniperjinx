#!/usr/bin/env python3
"""
batch_scorecard.py — one-page trade quality summary.

Usage:
    python3 batch_scorecard.py            # last 20 closes
    python3 batch_scorecard.py --n 50     # last 50 closes
    python3 batch_scorecard.py --all      # all closes in history
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
HISTORY_FILE = BASE / "history.json"
# Common log file candidates (stderr sink for the bot process)
LOG_CANDIDATES = [
    BASE / "logs" / "logs_sim.err",
    BASE / "logs_sim.err",
    BASE / "sim.err",
    BASE / "bot.err",
    BASE / "nohup.out",
]

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def _c(text, colour):
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else str(text)


# ── Load data ─────────────────────────────────────────────────────────────────
def load_closes(n: int | None) -> list[dict]:
    if not HISTORY_FILE.exists():
        sys.exit(f"history.json not found at {HISTORY_FILE}")
    try:
        history = json.loads(HISTORY_FILE.read_text())
    except Exception as e:
        sys.exit(f"Failed to parse history.json: {e}")

    closes = [ev for ev in history if ev.get("type") == "trade_close"]
    if not closes:
        sys.exit("No trade_close events found in history.json")

    closes.sort(key=lambda ev: float(ev.get("ts") or 0))
    if n is not None:
        closes = closes[-n:]
    return closes


def grep_pre_entry_rejections() -> dict[str, int]:
    """Count PRE_ENTRY_PRICE_SURGE / PRE_ENTRY_PRICE_DUMP from any available log file."""
    counts = {"surge": 0, "dump": 0}
    log_file = next((p for p in LOG_CANDIDATES if p.exists()), None)
    if log_file is None:
        return counts
    try:
        text = log_file.read_text(errors="replace")
        counts["surge"] = len(re.findall(r"PRE_ENTRY_PRICE_SURGE", text))
        counts["dump"]  = len(re.findall(r"PRE_ENTRY_PRICE_DUMP", text))
    except Exception:
        pass
    return counts


# ── Stats helpers ─────────────────────────────────────────────────────────────
def _f(val, default=0.0) -> float:
    try:
        return float(val or default)
    except (TypeError, ValueError):
        return default


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "sum": 0.0, "avg": 0.0, "min": 0.0, "max": 0.0}
    return {
        "n":   len(values),
        "sum": sum(values),
        "avg": sum(values) / len(values),
        "min": min(values),
        "max": max(values),
    }


# ── Sections ──────────────────────────────────────────────────────────────────
def section(title: str):
    width = 62
    print()
    print(_c(f"  {title}", BOLD))
    print("  " + "─" * (width - 2))


def fmt_pnl(v: float) -> str:
    s = f"${v:+.4f}"
    return _c(s, GREEN if v >= 0 else RED)


def fmt_roi(v: float) -> str:
    s = f"{v:+.1f}%"
    return _c(s, GREEN if v >= 0 else RED)


def fmt_pct(v: float) -> str:
    s = f"{v:.1f}%"
    return _c(s, GREEN if v >= 35 else RED)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Trade batch scorecard")
    parser.add_argument("--n", type=int, default=20, metavar="N",
                        help="Number of most-recent closes to analyse (default: 20)")
    parser.add_argument("--all", action="store_true",
                        help="Analyse all closes in history (overrides --n)")
    args = parser.parse_args()

    n = None if args.all else args.n
    closes = load_closes(n)

    label = "all" if args.all else f"last {len(closes)}"
    ts_first = closes[0].get("ts", 0)
    ts_last  = closes[-1].get("ts", 0)

    # ── Header ────────────────────────────────────────────────────────────────
    print()
    print(_c(f"  BATCH SCORECARD — {label.upper()} CLOSES", BOLD))
    if ts_first and ts_last:
        import datetime
        d0 = datetime.datetime.fromtimestamp(ts_first, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
        d1 = datetime.datetime.fromtimestamp(ts_last,  datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"  {d0} → {d1} UTC")

    # ── Overall metrics ───────────────────────────────────────────────────────
    section("OVERALL")
    # P4.2: canonical win = pnl_usd > 0 (breakeven is not a win).
    wins   = [c for c in closes if _f(c.get("pnl_usd")) >  0]
    losses = [c for c in closes if _f(c.get("pnl_usd")) <= 0]
    win_rate = len(wins) / len(closes) * 100
    total_pnl = sum(_f(c.get("pnl_usd")) for c in closes)
    rois = [_f(c.get("roi_pct")) for c in closes]
    avg_roi = sum(rois) / len(rois)
    holds = [_f(c.get("hold_time_seconds")) for c in closes]
    avg_hold = sum(holds) / len(holds) if holds else 0

    # P4.5: 95% Clopper–Pearson CI so the user can see sample-size uncertainty.
    from constants import clopper_pearson_ci
    _wr_lo, _wr_hi = clopper_pearson_ci(len(wins), len(closes))

    print(f"  Closes analysed : {len(closes)}  (wins: {len(wins)}  losses: {len(losses)})")
    print(f"  Win rate        : {fmt_pct(win_rate)}  (CI95 [{_wr_lo*100:.1f}%, {_wr_hi*100:.1f}%], target: 35%)")
    print(f"  Total P&L       : {fmt_pnl(total_pnl)}")
    print(f"  Avg ROI         : {fmt_roi(avg_roi)}")
    print(f"  Avg hold time   : {avg_hold:.0f}s")

    # ── Close reason breakdown ─────────────────────────────────────────────────
    section("CLOSE REASON BREAKDOWN")
    reason_map: dict[str, list[dict]] = {}
    for c in closes:
        r = c.get("close_reason") or "unknown"
        reason_map.setdefault(r, []).append(c)

    # Sort by total P&L impact (most negative first = biggest drains)
    reasons_sorted = sorted(
        reason_map.items(),
        key=lambda kv: sum(_f(c.get("pnl_usd")) for c in kv[1])
    )

    col_w = max(len(r) for r in reason_map) + 2
    header = f"  {'Reason':<{col_w}}  {'N':>4}  {'Avg ROI':>8}  {'Total P&L':>10}"
    print(header)
    print("  " + "·" * (len(header) - 2))
    for reason, evts in reasons_sorted:
        n_r    = len(evts)
        pnls_r = [_f(e.get("pnl_usd")) for e in evts]
        rois_r = [_f(e.get("roi_pct")) for e in evts]
        tot    = sum(pnls_r)
        avg_r  = sum(rois_r) / len(rois_r)
        print(f"  {reason:<{col_w}}  {n_r:>4}  {fmt_roi(avg_r):>8}  {fmt_pnl(tot):>10}")

    # ── HARD_CAP detail ───────────────────────────────────────────────────────
    hc = [c for c in closes if "HARD_CAP" in str(c.get("close_reason", ""))]
    section("HARD_CAP EXITS")
    if hc:
        hc_rois = [_f(c.get("roi_pct")) for c in hc]
        hc_pnls = [_f(c.get("pnl_usd")) for c in hc]
        hc_pct  = len(hc) / len(closes) * 100
        hc_colour = RED if hc_pct > 15 else YELLOW if hc_pct > 10 else RESET
        print(f"  Count    : {_c(str(len(hc)), hc_colour)}  ({hc_pct:.1f}% of closes — threshold: 15%)")
        print(f"  Avg ROI  : {fmt_roi(sum(hc_rois)/len(hc_rois))}")
        print(f"  Total P&L: {fmt_pnl(sum(hc_pnls))}")
        print(f"  Worst    : {fmt_roi(min(hc_rois))}")
    else:
        print("  None in this window.")

    # ── PRE_ENTRY rejections ──────────────────────────────────────────────────
    section("PRE-ENTRY REJECTIONS (log grep)")
    pre = grep_pre_entry_rejections()
    log_found = next((p for p in LOG_CANDIDATES if p.exists()), None)
    if log_found:
        print(f"  Log file : {log_found.name}")
        print(f"  Surge    : {pre['surge']}  (fill > 1.3× reference)")
        print(f"  Dump     : {pre['dump']}   (fill < 0.4× reference)")
    else:
        print(f"  No log file found. Checked: {', '.join(p.name for p in LOG_CANDIDATES)}")

    # ── MFE capture ───────────────────────────────────────────────────────────
    section("MFE CAPTURE  (trades that reached MFE > 20%)")
    # P4.9: use shared cap helper so capped rows are counted + reported explicitly.
    from constants import cap_mfe as _cap_mfe, MFE_SANITY_CAP_PCT as _MFE_CAP
    def _mfe(c):
        v, capped = _cap_mfe(c.get("mfe_pct"))
        return v, capped
    big_mfe  = [c for c in closes if _mfe(c)[0] > 20.0]
    _capped_count = sum(1 for c in closes if _mfe(c)[1])
    if _capped_count:
        print(f"  NOTE: {_capped_count} closes hit the {_MFE_CAP:.0f}% sanity cap — MFE stats may understate extremes.")
    if big_mfe:
        captured_rois = [_f(c.get("roi_pct")) for c in big_mfe]
        mfe_vals      = [_mfe(c)[0] for c in big_mfe]
        positive      = [c for c in big_mfe if _f(c.get("roi_pct")) >= 0]
        capture_rate  = len(positive) / len(big_mfe) * 100

        print(f"  Count              : {len(big_mfe)}")
        print(f"  Exited positive    : {len(positive)} ({capture_rate:.0f}%)")
        print(f"  Avg MFE            : +{sum(mfe_vals)/len(mfe_vals):.1f}%")
        print(f"  Avg exit ROI       : {fmt_roi(sum(captured_rois)/len(captured_rois))}")
        print(f"  Best exit          : {fmt_roi(max(captured_rois))}")
        print(f"  Worst exit (gave back): {fmt_roi(min(captured_rois))}")

        # Show worst give-backs (MFE high but ROI terrible = trail fired too early)
        givebacks = sorted(big_mfe, key=lambda c: _f(c.get("roi_pct")))[:3]
        if givebacks:
            print()
            print("  Worst MFE give-backs:")
            for c in givebacks:
                mint_short = (c.get("mint") or "?")[:12]
                print(f"    {mint_short}  MFE={min(abs(_f(c.get('mfe_pct'))), _MFE_CAP):+.1f}%  exit={fmt_roi(_f(c.get('roi_pct')))}  reason={c.get('close_reason','?')}")
    else:
        print("  No trades reached MFE > 20% in this window.")

    # ── Verdict ───────────────────────────────────────────────────────────────
    section("VERDICT")
    pass_wr  = win_rate >= 35.0
    pass_pnl = total_pnl > 0.0
    passed   = pass_wr and pass_pnl

    if passed:
        print(_c("  PASS", GREEN + BOLD))
        print(f"  Win rate {win_rate:.1f}% ≥ 35%  |  P&L {fmt_pnl(total_pnl)} > $0")
    else:
        print(_c("  FAIL", RED + BOLD))
        fails = []
        if not pass_wr:
            fails.append(f"win rate {win_rate:.1f}% < 35%")
        if not pass_pnl:
            fails.append(f"total P&L {fmt_pnl(total_pnl)} ≤ $0")
        print(f"  Criteria failed: {', '.join(fails)}")

        # Top 2 drain reasons
        drain_sorted = sorted(
            reason_map.items(),
            key=lambda kv: sum(_f(c.get("pnl_usd")) for c in kv[1])
        )
        print()
        print("  Top drain reasons:")
        for reason, evts in drain_sorted[:2]:
            drain = sum(_f(c.get("pnl_usd")) for c in evts)
            avg_r = sum(_f(c.get("roi_pct")) for c in evts) / len(evts)
            print(f"    [{len(evts)}×] {reason:<30}  {fmt_pnl(drain)}  avg {fmt_roi(avg_r)}")

    print()


if __name__ == "__main__":
    main()
