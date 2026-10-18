#!/usr/bin/env python3
"""
trade_attribution.py — Structured "why did this trade win or lose?" analysis.

Assigns one entry bucket, one exit bucket, and one execution bucket per trade,
then aggregates P&L by bucket to surface where losses originate.

Usage:
    python3 trade_attribution.py            # last 30 closes
    python3 trade_attribution.py --n 50     # last 50 closes
    python3 trade_attribution.py --all      # all closes
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent
HISTORY_FILE = BASE / "history.json"

_MFE_CAP = 500.0
# P4.7: suspect-price constants live in constants.py (SSOT).
from constants import SUSPECT_PRICES as _SUSPECT_PRICES, SUSPECT_TOL as _SUSPECT_TOL, is_suspect_price as _c_is_suspect

# ── ANSI ─────────────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def _c(text, colour):
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else str(text)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _f(val, default=0.0) -> float:
    try:
        return float(val or default)
    except (TypeError, ValueError):
        return default


def _is_suspect_price(px: float) -> bool:
    # P4.7: canonical impl in constants.is_suspect_price.
    return _c_is_suspect(px)


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


# ── Data loading ─────────────────────────────────────────────────────────────
def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        sys.exit(f"history.json not found at {HISTORY_FILE}")
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception as e:
        sys.exit(f"Failed to parse history.json: {e}")


def _build_enriched_closes(history: list[dict], n: Optional[int]) -> list[dict]:
    """Load trade_close events, join with signal_accept/execution_attempt."""
    closes = [ev for ev in history if ev.get("type") == "trade_close"]
    closes.sort(key=lambda ev: float(ev.get("ts") or 0))
    if n is not None:
        closes = closes[-n:]

    # Build lookup indices by mint
    by_mint: dict[str, list[dict]] = defaultdict(list)
    for ev in history:
        mint = ev.get("mint")
        if mint:
            by_mint[mint].append(ev)

    enriched = []
    for c in closes:
        mint = c.get("mint", "")
        entry_ts = float(c.get("entry_ts") or 0)
        decision_id = c.get("decision_id") or ""
        events = by_mint.get(mint, [])

        # Join signal_accept
        sig = None
        if decision_id:
            sigs = [e for e in events if e.get("type") == "signal_accept" and e.get("decision_id") == decision_id]
            if sigs:
                sig = sigs[0]
        if sig is None:
            sigs = [e for e in events if e.get("type") == "signal_accept"
                    and abs(float(e.get("ts") or 0) - entry_ts) <= 300]
            if sigs:
                sig = min(sigs, key=lambda e: abs(float(e.get("ts") or 0) - entry_ts))

        # Join execution_attempt
        ex = None
        if decision_id:
            execs = [e for e in events if e.get("type") == "execution_attempt" and e.get("decision_id") == decision_id]
            if execs:
                ex = execs[0]
        if ex is None:
            execs = [e for e in events if e.get("type") == "execution_attempt"
                     and abs(float(e.get("ts") or 0) - entry_ts) <= 300]
            if execs:
                ex = min(execs, key=lambda e: abs(float(e.get("ts") or 0) - entry_ts))

        # Determine entry price for suspect detection
        fill_px = float(ex.get("fill_price_usd") or 0) if ex else 0.0
        ref_px = float(ex.get("reference_price_usd") or 0) if ex else 0.0
        ref_is_real = ex.get("reference_price_is_real") if ex else None
        entry_price = fill_px if fill_px > 0 else ref_px

        price_suspect = False
        if fill_px > 0:
            price_suspect = False
        elif ref_is_real is not None and not ref_is_real:
            price_suspect = True
        elif entry_price > 0:
            price_suspect = _is_suspect_price(entry_price)

        # Candidate age
        candidate_age_ms = None
        if ex and ex.get("candidate_age_ms") is not None:
            candidate_age_ms = float(ex["candidate_age_ms"])
        elif sig and sig.get("candidate_age_ms") is not None:
            candidate_age_ms = float(sig["candidate_age_ms"])
        elif sig and sig.get("delay_s") is not None:
            candidate_age_ms = float(sig["delay_s"]) * 1000

        # Entry score (pre-fetched during the 30s delayed-entry wait, attached to
        # signal_accept). Optional — older trades pre-dating the score system won't have it.
        entry_score = sig.get("entry_score") if isinstance(sig, dict) else None
        if not isinstance(entry_score, dict):
            entry_score = None

        signal_source = sig.get("source") if isinstance(sig, dict) else None
        signal_delay_s = None
        if isinstance(sig, dict) and sig.get("delay_s") is not None:
            try:
                signal_delay_s = float(sig["delay_s"])
            except (TypeError, ValueError):
                signal_delay_s = None

        enriched.append({
            "mint": mint,
            "ts": float(c.get("ts") or 0),
            "entry_ts": entry_ts,
            "pnl_usd": _f(c.get("pnl_usd")),
            "roi_pct": _f(c.get("roi_pct")),
            "mfe_pct": min(abs(_f(c.get("mfe_pct"))), _MFE_CAP),
            "mae_pct": _f(c.get("mae_pct")),
            "close_reason": c.get("close_reason") or "unknown",
            "hold_time_s": _f(c.get("hold_time_seconds")),
            "fees_usd": _f(c.get("fees_usd")),
            "size_usd": _f(c.get("realized_basis_usd") or c.get("size_usd")),
            "price_suspect": price_suspect,
            "candidate_age_ms": candidate_age_ms,
            "exec_ok": ex.get("ok") if ex else None,
            "exec_error": ex.get("error") if ex else None,
            "entry_score": entry_score,
            "signal_source": signal_source,
            "signal_delay_s": signal_delay_s,
            # min_liquidity_usd_active is the policy threshold, not actual token
            # liquidity — actual liquidity at entry is not recorded in history.
        })

    return enriched


# ── Attribution buckets ──────────────────────────────────────────────────────
def classify_entry(t: dict) -> str:
    if t["price_suspect"]:
        return "BAD_ENTRY_POST_PUMP"
    age_ms = t["candidate_age_ms"]
    if age_ms is not None and age_ms > 60_000:
        # Delayed-entry path intentionally waits for Jupiter to index the pool.
        # A Jupiter-fallback retry at T+45-120s is normal, not a late entry.
        delay_s = t.get("signal_delay_s")
        if t.get("signal_source") == "delayed_entry" and delay_s is not None and delay_s <= 120:
            return "ENTRY_OK"
        return "BAD_ENTRY_LATE"
    # BAD_ENTRY_LOW_LIQ: actual token liquidity at entry is not recorded in
    # history events (min_liquidity_usd_active is the policy threshold).
    # Bucket exists but cannot fire until liquidity is persisted.
    return "ENTRY_OK"


def classify_exit(t: dict) -> str:
    reason = t["close_reason"].upper()
    if "HARD_CAP" in reason:
        return "EXIT_HARD_CAP"
    if "GHOST_BUY" in reason:
        return "EXIT_GHOST"
    if "RAPID DUMP" in reason:
        return "EXIT_RAPID_DUMP"
    if "TRAIL" in reason or "SL EXIT" in reason or "VELOCITY_TRAIL" in reason:
        return "EXIT_TRAIL_POSITIVE" if t["roi_pct"] > 0 else "EXIT_TRAIL_NEGATIVE"
    if "QUOTEABILITY" in reason:
        return "EXIT_QUOTEABILITY"
    if "TTL_TIMEOUT" in reason or "STALE_MARK_TIMEOUT" in reason:
        return "EXIT_TTL"
    return "EXIT_OTHER"


def classify_exec(t: dict) -> str:
    if "GHOST_BUY" in t["close_reason"].upper():
        return "EXEC_SWAP_FAILED"
    return "EXEC_OK"


def compute_metrics(t: dict) -> dict:
    mfe = t["mfe_pct"]
    roi = t["roi_pct"]
    entry_bucket = classify_entry(t)
    exit_bucket = classify_exit(t)
    return {
        "mfe_captured_pct": (roi / mfe * 100) if mfe > 0 else 0.0,
        "gave_back_pct": (mfe - roi) if mfe > 0 else 0.0,
        "was_profitable": t["pnl_usd"] > 0,
        "loss_avoidable": (
            entry_bucket.startswith("BAD_ENTRY_")
            and exit_bucket in ("EXIT_HARD_CAP", "EXIT_RAPID_DUMP")
        ),
    }


# ── Display ──────────────────────────────────────────────────────────────────
def fmt_pnl(v: float) -> str:
    s = f"${v:+.4f}"
    return _c(s, GREEN if v >= 0 else RED)

def fmt_roi(v: float) -> str:
    s = f"{v:+.1f}%"
    return _c(s, GREEN if v >= 0 else RED)


def print_trade_table(trades: list[dict]):
    print()
    print(_c("  PER-TRADE ATTRIBUTION", BOLD))
    print("  " + "-" * 100)
    header = f"  {'Mint':<14} {'ROI%':>7} {'MFE%':>7} {'Entry':<20} {'Exit':<22} {'Exec':<16} {'Cap%':>6} {'Note'}"
    print(header)
    print("  " + "." * 100)

    for t in trades:
        entry_b = classify_entry(t)
        exit_b = classify_exit(t)
        exec_b = classify_exec(t)
        m = compute_metrics(t)

        mint_short = t["mint"][:12] if len(t["mint"]) > 12 else t["mint"]
        cap_str = f"{m['mfe_captured_pct']:.0f}" if t["mfe_pct"] > 0 else "-"
        notes = []
        if m["loss_avoidable"]:
            notes.append("AVOIDABLE")
        if m["gave_back_pct"] > 20:
            notes.append(f"gave_back={m['gave_back_pct']:.0f}%")
        note = " ".join(notes)

        print(f"  {mint_short:<14} {fmt_roi(t['roi_pct']):>7} {t['mfe_pct']:>6.1f}% {entry_b:<20} {exit_b:<22} {exec_b:<16} {cap_str:>6} {note}")


def print_aggregate(trades: list[dict]):
    # Entry buckets
    entry_buckets: dict[str, list[dict]] = defaultdict(list)
    exit_buckets: dict[str, list[dict]] = defaultdict(list)
    exec_buckets: dict[str, list[dict]] = defaultdict(list)
    avoidable_count = 0
    avoidable_pnl = 0.0
    exit_mfe_capture: dict[str, list[float]] = defaultdict(list)

    for t in trades:
        entry_b = classify_entry(t)
        exit_b = classify_exit(t)
        exec_b = classify_exec(t)
        m = compute_metrics(t)

        entry_buckets[entry_b].append(t)
        exit_buckets[exit_b].append(t)
        exec_buckets[exec_b].append(t)

        if m["loss_avoidable"]:
            avoidable_count += 1
            avoidable_pnl += t["pnl_usd"]

        if t["mfe_pct"] > 0:
            exit_mfe_capture[exit_b].append(m["mfe_captured_pct"])

    # Entry summary
    print()
    print(_c("  ENTRY ATTRIBUTION", BOLD))
    print("  " + "-" * 60)
    col = 22
    print(f"  {'Bucket':<{col}} {'N':>4} {'P&L':>12} {'Avg ROI':>9}")
    print("  " + "." * 60)
    entry_total_loss = 0.0
    for bucket in ["ENTRY_OK", "BAD_ENTRY_POST_PUMP", "BAD_ENTRY_LATE"]:
        group = entry_buckets.get(bucket, [])
        if not group:
            continue
        pnl = sum(t["pnl_usd"] for t in group)
        avg_roi = sum(t["roi_pct"] for t in group) / len(group)
        if bucket.startswith("BAD_"):
            entry_total_loss += pnl
        print(f"  {bucket:<{col}} {len(group):>4} {fmt_pnl(pnl):>12} {fmt_roi(avg_roi):>9}")

    # Exit summary
    print()
    print(_c("  EXIT ATTRIBUTION", BOLD))
    print("  " + "-" * 60)
    print(f"  {'Bucket':<{col}} {'N':>4} {'P&L':>12} {'Med Cap%':>9}")
    print("  " + "." * 60)
    exit_total_loss = 0.0
    exit_order = [
        "EXIT_TRAIL_POSITIVE", "EXIT_TRAIL_NEGATIVE", "EXIT_HARD_CAP",
        "EXIT_RAPID_DUMP", "EXIT_GHOST", "EXIT_QUOTEABILITY", "EXIT_TTL", "EXIT_OTHER",
    ]
    for bucket in exit_order:
        group = exit_buckets.get(bucket, [])
        if not group:
            continue
        pnl = sum(t["pnl_usd"] for t in group)
        med_cap = _median(exit_mfe_capture.get(bucket, []))
        if pnl < 0:
            exit_total_loss += pnl
        print(f"  {bucket:<{col}} {len(group):>4} {fmt_pnl(pnl):>12} {med_cap:>8.0f}%")

    # Execution summary
    print()
    print(_c("  EXECUTION ATTRIBUTION", BOLD))
    print("  " + "-" * 60)
    exec_fail = exec_buckets.get("EXEC_SWAP_FAILED", [])
    exec_ok = exec_buckets.get("EXEC_OK", [])
    exec_fail_fees = sum(t["fees_usd"] for t in exec_fail)
    print(f"  Successful executions : {len(exec_ok)}")
    print(f"  Swap failures (ghost) : {len(exec_fail)}")
    if exec_fail:
        print(f"  Fee cost from failures: {fmt_pnl(-exec_fail_fees)}")

    # Avoidable losses
    print()
    print(_c("  AVOIDABLE LOSSES", BOLD))
    print("  " + "-" * 60)
    print(f"  Bad entry -> bad exit : {avoidable_count} trades")
    if avoidable_count:
        print(f"  Avoidable P&L         : {fmt_pnl(avoidable_pnl)}")

    # MFE capture by exit type
    print()
    print(_c("  MEDIAN MFE CAPTURE BY EXIT TYPE", BOLD))
    print("  " + "-" * 60)
    for bucket in exit_order:
        caps = exit_mfe_capture.get(bucket, [])
        if not caps:
            continue
        med = _median(caps)
        colour = GREEN if med > 50 else YELLOW if med > 20 else RED
        print(f"  {bucket:<{col}} {_c(f'{med:.0f}%', colour):>8}  (n={len(caps)})")

    # Top recommendation
    print()
    print(_c("  RECOMMENDATION", BOLD))
    print("  " + "-" * 60)

    exec_loss = sum(t["pnl_usd"] for t in exec_fail) if exec_fail else 0.0
    losses = {
        "ENTRY": entry_total_loss,
        "EXIT": exit_total_loss,
        "EXECUTION": exec_loss,
    }
    worst = min(losses, key=losses.get)
    print(f"  Biggest loss source: {_c(worst, RED + BOLD)}  ({fmt_pnl(losses[worst])})")

    if worst == "ENTRY":
        bad_entry_count = sum(len(v) for k, v in entry_buckets.items() if k.startswith("BAD_"))
        print(f"  {bad_entry_count} trades entered with bad signals — tighten entry filters")
    elif worst == "EXIT":
        # Find worst exit bucket
        worst_exit = min(
            ((b, sum(t["pnl_usd"] for t in g)) for b, g in exit_buckets.items()),
            key=lambda x: x[1],
        )
        print(f"  Worst exit bucket: {worst_exit[0]} ({fmt_pnl(worst_exit[1])})")
    else:
        print(f"  {len(exec_fail)} swap failures — check execution pipeline")

    print()


def print_entry_score_distribution(trades: list[dict]):
    """Score distribution + component breakdowns.
    Trades without entry_score (pre-system or score-disabled) are skipped."""
    scored = [t for t in trades if isinstance(t.get("entry_score"), dict)]
    print()
    print(_c("  ENTRY SCORE DISTRIBUTION", BOLD))
    print("  " + "-" * 60)
    if not scored:
        print(f"  Scored trades: 0  (no trades in this window carry an entry_score)")
        print()
        return

    totals = [_f((t["entry_score"] or {}).get("total")) for t in scored]
    avg_total = sum(totals) / len(totals) if totals else 0.0
    print(f"  Scored trades: {len(scored)} / {len(trades)}")
    print(f"  Avg score:     {avg_total:.1f}")
    print()

    # Buckets: 0-39 low, 40-59 medium, 60-79 high, 80+ strong
    bucket_defs = [
        ("0-39  (low)   ", 0, 40),
        ("40-59 (medium)", 40, 60),
        ("60-79 (high)  ", 60, 80),
        ("80+   (strong)", 80, 10_000),
    ]
    print(f"  {'Bucket':<16} {'N':>4} {'Avg ROI':>9}")
    print("  " + "." * 40)
    for label, lo, hi in bucket_defs:
        group = [t for t in scored if lo <= _f((t["entry_score"] or {}).get("total")) < hi]
        if not group:
            print(f"  {label:<16} {0:>4} {'-':>9}")
            continue
        avg_roi = sum(t["roi_pct"] for t in group) / len(group)
        print(f"  {label:<16} {len(group):>4} {fmt_roi(avg_roi):>9}")

    # Component averages
    print()
    print(f"  {'Component':<22} {'Avg':>6}  {'Full-credit trades'}")
    print("  " + "." * 55)
    components = [
        ("graduation_pts", "time_to_graduate_pts", 40),
        ("dex_pts       ", "dexscreener_pts", 30),
        ("holder_pts    ", "holder_pts", 20),
        ("social_pts    ", "social_pts", 10),
    ]
    for label, key, max_pts in components:
        vals = [_f((t["entry_score"] or {}).get(key)) for t in scored]
        avg = sum(vals) / len(vals) if vals else 0.0
        full = sum(1 for v in vals if v >= max_pts)
        print(f"  {label:<22} {avg:>5.1f}  {full}/{len(scored)} got full {max_pts}")

    # Fetch-error tally — if high, graduation/social pts are mostly neutral defaults.
    errs = [(t["entry_score"] or {}).get("fetch_error") for t in scored]
    err_count = sum(1 for e in errs if e)
    if err_count:
        print()
        print(f"  getAsset fetch errors: {err_count}/{len(scored)} (scores used neutral fallback)")
    print()


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Trade attribution analysis")
    parser.add_argument("--n", type=int, default=30, metavar="N",
                        help="Number of most-recent closes to analyse (default: 30)")
    parser.add_argument("--all", action="store_true",
                        help="Analyse all closes (overrides --n)")
    args = parser.parse_args()

    n = None if args.all else args.n
    history = load_history()
    trades = _build_enriched_closes(history, n)

    if not trades:
        sys.exit("No trade_close events found in history.json")

    label = "all" if args.all else f"last {len(trades)}"
    total_pnl = sum(t["pnl_usd"] for t in trades)
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    wr = wins / len(trades) * 100
    # P4.5: 95% Clopper–Pearson CI on win rate.
    from constants import clopper_pearson_ci
    _wr_lo, _wr_hi = clopper_pearson_ci(wins, len(trades))

    print()
    print(_c(f"  TRADE ATTRIBUTION — {label.upper()} CLOSES", BOLD))
    print(f"  {len(trades)} trades | {fmt_pnl(total_pnl)} total | "
          f"{wins}W/{len(trades)-wins}L ({wr:.1f}% WR, CI95 [{_wr_lo*100:.1f}%, {_wr_hi*100:.1f}%])")

    print_trade_table(trades)
    print_aggregate(trades)
    print_entry_score_distribution(trades)


if __name__ == "__main__":
    main()
