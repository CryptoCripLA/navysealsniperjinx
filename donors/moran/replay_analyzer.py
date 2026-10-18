#!/usr/bin/env python3
"""
replay_analyzer.py — Standalone sniperbot trade replay and counterfactual analysis.

Reads history.json + runtime_state.json (read-only).
Writes REPLAY_REPORT.md with scenario comparisons and latency/MFE diagnostics.

Usage:
    python3 replay_analyzer.py
    python3 replay_analyzer.py --json        # also dump structured JSON
    python3 replay_analyzer.py --no-report   # print to stdout only
"""

import json
import sys
import math
import argparse
from pathlib import Path
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple, Any
from datetime import datetime, timezone

# ── File paths ────────────────────────────────────────────────────────────────
def _find(name: str) -> Path:
    candidates = [
        Path(__file__).parent / name,
        Path.home() / ".openclaw" / "workspace" / "solana-micro-sniper" / name,
        Path.home() / "solana-micro-sniper" / name,
        Path.cwd() / name,
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]

HISTORY_FILE  = _find("history.json")
STATE_FILE    = _find("runtime_state.json")
REPORT_FILE   = Path(__file__).parent / "REPLAY_REPORT.md"

# ── Bot defaults (P4.1: SSOT — imported from constants.py, shared with live) ──
from constants import (
    DEFAULT_STOP_PCT,
    FAILSAFE_PCT,
    TP1_PCT, TP1_SELL,
    TP2_PCT, TP2_SELL,
    TP3_PCT, TP3_SELL,
)
QUOTEABILITY_WINDOW_S = 35        # seconds before QUOTEABILITY_GUARD_EXIT fires

# close reasons that indicate "no real trade data" (no marks captured)
NO_MARK_REASONS = {
    "QUOTEABILITY_GUARD_EXIT",
    "QUOTEABILITY_GUARD_EXIT:no_pool",
    "QUOTEABILITY_GUARD_EXIT:transient",
    "STALE_MARK_TIMEOUT_EXIT",
    "AGE_GATE_EXIT",
    "FAILOVER_LIQUIDATION",
}

# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Trade:
    mint: str
    # Signal / detection
    signal_ts: Optional[float] = None
    signal_price: Optional[float] = None
    signal_source: Optional[str] = None
    candidate_age_ms: Optional[float] = None
    reference_price: Optional[float] = None
    # Execution
    exec_ts: Optional[float] = None
    exec_ok: Optional[bool] = None
    exec_error: Optional[str] = None
    exec_latency_ms: Optional[float] = None
    detect_to_exec_ms: Optional[float] = None
    ts_detected: Optional[float] = None
    exec_size_usd: Optional[float] = None
    exec_source: Optional[str] = None
    decision_breakdown: Optional[dict] = None
    # Partials
    partials: List[dict] = field(default_factory=list)
    # Close
    entry_ts: Optional[float] = None
    close_ts: Optional[float] = None
    hold_time_s: Optional[float] = None
    pnl_usd: Optional[float] = None
    roi_pct: Optional[float] = None
    close_reason: Optional[str] = None
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    exit_price: Optional[float] = None
    realized_basis_usd: Optional[float] = None
    proceeds_usd: Optional[float] = None
    sell_attempt_ts: Optional[float] = None   # None = field absent (older records)
    # Entry reference price (from execution_attempt.reference_price_usd)
    entry_price: Optional[float] = None


# ── Loaders ───────────────────────────────────────────────────────────────────
def load_history() -> List[dict]:
    if not HISTORY_FILE.exists():
        print(f"[WARN] {HISTORY_FILE} not found", file=sys.stderr)
        return []
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception as e:
        print(f"[ERROR] history.json: {e}", file=sys.stderr)
        return []

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


# ── Trade reconstruction ──────────────────────────────────────────────────────
def reconstruct_trades(history: List[dict]) -> List[Trade]:
    """
    Group history events by mint and reconstruct Trade lifecycle objects.
    Each trade_close defines one trade; we attach the nearest matching
    signal_accept and execution_attempt by timestamp proximity.
    """
    by_mint: Dict[str, List[dict]] = defaultdict(list)
    for ev in history:
        mint = ev.get("mint")
        if mint:
            by_mint[mint].append(ev)

    trades: List[Trade] = []

    for mint, events in by_mint.items():
        events.sort(key=lambda e: float(e.get("ts") or 0))
        close_events = [e for e in events if e.get("type") == "trade_close"]

        for close_ev in close_events:
            t = Trade(mint=mint)
            entry_ts = float(close_ev.get("entry_ts") or 0)
            close_ts  = float(close_ev.get("ts") or 0)

            # ── close fields
            t.entry_ts        = entry_ts
            t.close_ts        = close_ts
            t.hold_time_s     = float(close_ev.get("hold_time_seconds") or 0)
            t.pnl_usd         = float(close_ev.get("pnl_usd") or 0)
            t.roi_pct         = float(close_ev.get("roi_pct") or 0)
            t.exit_price      = close_ev.get("exit_price")
            t.close_reason    = close_ev.get("close_reason") or "unknown"
            t.mfe_pct         = float(close_ev.get("mfe_pct") or 0)
            t.mae_pct         = float(close_ev.get("mae_pct") or 0)
            t.realized_basis_usd = float(close_ev.get("realized_basis_usd") or 0)
            t.proceeds_usd = float(close_ev.get("proceeds_usd") or 0.0)
            # sell_attempt_ts: store None when field is absent (older records predate it).
            # Only set to 0.0 when explicitly recorded as 0 (ghost close — no sell TX).
            _sat = close_ev.get("sell_attempt_ts")
            t.sell_attempt_ts = float(_sat) if _sat is not None else None

            # ── nearest signal_accept within ±5 min of entry
            sigs = [e for e in events if e.get("type") == "signal_accept"
                    and abs(float(e.get("ts") or 0) - entry_ts) <= 300]
            if sigs:
                sig = min(sigs, key=lambda e: abs(float(e.get("ts") or 0) - entry_ts))
                t.signal_ts      = float(sig.get("ts") or 0)
                t.signal_price   = sig.get("price_usd")
                t.signal_source  = sig.get("source")
                t.candidate_age_ms = sig.get("candidate_age_ms")
                t.reference_price  = sig.get("reference_price")

            # ── nearest execution_attempt within ±5 min of entry
            execs = [e for e in events if e.get("type") == "execution_attempt"
                     and abs(float(e.get("ts") or 0) - entry_ts) <= 300]
            if execs:
                ex = min(execs, key=lambda e: abs(float(e.get("ts") or 0) - entry_ts))
                t.exec_ts         = float(ex.get("ts") or 0)
                t.exec_ok         = ex.get("ok")
                t.exec_error      = ex.get("error")
                t.exec_latency_ms = ex.get("execution_latency_ms") or ex.get("latency_ms")
                t.detect_to_exec_ms = ex.get("detect_to_exec_ms")
                t.ts_detected     = ex.get("ts_detected")
                t.exec_size_usd   = ex.get("size_usd")
                t.exec_source     = ex.get("source")
                t.decision_breakdown = ex.get("decision_breakdown_ms")
                _ep = ex.get("reference_price_usd")
                t.entry_price = float(_ep) if _ep is not None and float(_ep) > 0 else None

            # ── partials between entry and close
            t.partials = [
                e for e in events
                if e.get("type") == "partial_realize"
                and entry_ts <= float(e.get("ts") or 0) <= close_ts + 2
            ]

            trades.append(t)

    return trades


def all_exec_latencies(history: List[dict]) -> List[float]:
    """All detect_to_exec_ms values from execution_attempt records."""
    return [
        float(e["detect_to_exec_ms"])
        for e in history
        if e.get("type") == "execution_attempt"
        and e.get("detect_to_exec_ms") is not None
    ]

def all_exec_submit_latencies(history: List[dict]) -> List[float]:
    """execution_latency_ms (Jupiter+submit+verify) from execution_attempt records."""
    return [
        float(e["execution_latency_ms"])
        for e in history
        if e.get("type") == "execution_attempt"
        and e.get("execution_latency_ms") is not None
        and float(e.get("execution_latency_ms") or 0) > 0
    ]


# ── Statistics helpers ────────────────────────────────────────────────────────
def percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    idx = (len(s) - 1) * p / 100.0
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)

def scenario_stats(results: List[Tuple[float, float]]) -> dict:
    """results = list of (roi_pct, pnl_usd)"""
    if not results:
        return dict(n=0, wins=0, losses=0, win_rate=0.0, avg_roi=0.0,
                    avg_pnl=0.0, expectancy=0.0, total_pnl=0.0,
                    best_roi=0.0, worst_roi=0.0)
    rois = [r for r, _ in results]
    pnls = [p for _, p in results]
    wins   = [r for r in rois if r > 0]
    losses = [r for r in rois if r <= 0]
    return dict(
        n=len(results),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(results) * 100,
        avg_roi=sum(rois) / len(rois),
        avg_pnl=sum(pnls) / len(pnls),
        expectancy=sum(pnls) / len(pnls),
        total_pnl=sum(pnls),
        best_roi=max(rois),
        worst_roi=min(rois),
    )


# ── Counterfactual: stop loss ─────────────────────────────────────────────────
def cf_stop_loss(trades: List[Trade], stop_pct: float) -> List[Tuple[float, float]]:
    """
    Hard stop at -stop_pct × 100 from entry.
    If MAE ≤ -(stop_pct*100): trade exits at -stop_pct (capped loss).
    Otherwise: actual outcome.
    Only runs on trades with non-zero MAE or non-QUOTEABILITY close reason.
    """
    results = []
    for t in trades:
        if t.roi_pct is None:
            continue
        size = t.realized_basis_usd or t.exec_size_usd or 0.5
        stop_roi = -(stop_pct * 100)
        mae = t.mae_pct if t.mae_pct is not None else 0.0
        if mae <= stop_roi:
            # MAE breached stop level; stop would have fired
            # Use stop_roi unless actual exit was already better
            sim_roi = max(stop_roi, t.roi_pct)
            sim_pnl = size * sim_roi / 100
        else:
            sim_roi = t.roi_pct
            sim_pnl = t.pnl_usd if t.pnl_usd is not None else size * sim_roi / 100
        results.append((sim_roi, sim_pnl))
    return results

def cf_trailing_stop_from_mfe(trades: List[Trade], trail_pct: float) -> List[Tuple[float, float]]:
    """
    Trailing stop fires trail_pct below MFE peak.
    Exit ROI ≈ (1 + MFE/100) * (1 - trail_pct) - 1
    Only for trades where we have meaningful MFE data (nonzero MFE or non-quoteability exit).
    Actual exit wins if actual ROI > trailing-stop ROI.
    """
    results = []
    for t in trades:
        if t.roi_pct is None or t.mfe_pct is None:
            continue
        size = t.realized_basis_usd or t.exec_size_usd or 0.5
        mfe = t.mfe_pct / 100.0
        # What the trailing stop from peak would deliver
        trail_exit_roi = ((1 + mfe) * (1 - trail_pct) - 1) * 100
        # Trailing stop is only better if it would have fired before actual exit
        # and delivered a better result; otherwise keep actual
        sim_roi = max(trail_exit_roi, t.roi_pct) if mfe > trail_pct else t.roi_pct
        sim_pnl = size * sim_roi / 100
        results.append((sim_roi, sim_pnl))
    return results


# ── Counterfactual: latency filter ───────────────────────────────────────────
def cf_latency_filter(trades: List[Trade], max_ms: float) -> List[Trade]:
    """
    Keep only trades where we have no latency data OR detection → exec ≤ max_ms.
    (Unknown latency = can't filter = keep)
    """
    return [
        t for t in trades
        if t.detect_to_exec_ms is None or t.detect_to_exec_ms <= max_ms
    ]


# ── Counterfactual: latency-aware ROI (P4.10) ────────────────────────────────
# The raw counterfactuals above ignore fill latency: they assume we buy at the
# signal price. In reality, by the time the buy lands (typically 1–8 s after
# detection) the token has moved. For pump.fun graduates in the first ~30 s
# we observed roughly 2%/s of upside drift during the walk-up phase. The
# constant below is a rough-but-published haircut rate — flag it on every
# report so readers don't mistake modelled ROI for observed ROI.
LATENCY_HAIRCUT_PCT_PER_SEC = float(
    # Env-tunable: if future studies refine this, bump via env without a code
    # change. Default 2%/s matches the 2026-04-18 walk-up observation window.
    __import__("os").environ.get("LATENCY_HAIRCUT_PCT_PER_SEC", "2.0")
)

def cf_latency_adjusted_roi(trades: List[Trade]) -> List[Tuple[float, float]]:
    """
    Return (adjusted_roi_pct, adjusted_pnl_usd) pairs with a linear haircut
    applied based on detect_to_exec_ms. Unknown latency → no haircut applied,
    same as the raw counterfactuals (can't model what we can't measure).
    """
    results: List[Tuple[float, float]] = []
    for t in trades:
        if t.roi_pct is None:
            continue
        size = t.realized_basis_usd or t.exec_size_usd or 0.5
        lat_s = (t.detect_to_exec_ms or 0.0) / 1000.0
        haircut_pct = lat_s * LATENCY_HAIRCUT_PCT_PER_SEC
        adj_roi = t.roi_pct - haircut_pct
        adj_pnl = size * adj_roi / 100.0
        results.append((adj_roi, adj_pnl))
    return results


# ── Counterfactual: partial TP ────────────────────────────────────────────────
def cf_partial_tp(trades: List[Trade], tp_pct: float, tp_sell_frac: float) -> List[Tuple[float, float]]:
    """
    Add a partial TP at +tp_pct (sell tp_sell_frac of position at tp_pct ROI).
    Remaining (1 - tp_sell_frac) closes at actual exit ROI.
    Only fires when MFE ≥ tp_pct.
    Blended ROI = tp_sell_frac * tp_pct + (1 - tp_sell_frac) * actual_roi
    """
    results = []
    for t in trades:
        if t.roi_pct is None or t.mfe_pct is None:
            continue
        size = t.realized_basis_usd or t.exec_size_usd or 0.5
        if t.mfe_pct >= tp_pct:
            # TP fires
            blended = tp_sell_frac * tp_pct + (1 - tp_sell_frac) * t.roi_pct
        else:
            blended = t.roi_pct
        sim_pnl = size * blended / 100
        results.append((blended, sim_pnl))
    return results


# ── Counterfactual: quoteability window ───────────────────────────────────────
def cf_quoteability_window(trades: List[Trade], window_s: float) -> List[Tuple[float, float]]:
    """
    Simulate different quoteability window durations.

    QUOTEABILITY_GUARD_EXIT fires when Jupiter can't quote within window_s after entry.
    - Shorter window → earlier exit but same outcome (no quote = no mark = -cost_of_trade)
    - Longer window → some transient failures might resolve; assumed to resolve at 0% ROI

    For 'no_pool' exits: pool literally doesn't exist yet; longer window won't help.
    For 'transient' exits: Jupiter glitch; longer window might resolve (optimistic +0% ROI).
    For base QUOTEABILITY_GUARD_EXIT: unclear; model as-is.

    This counterfactual shows the outcome sensitivity to window length.
    """
    results = []
    for t in trades:
        if t.roi_pct is None:
            continue
        size = t.realized_basis_usd or t.exec_size_usd or 0.5
        reason = t.close_reason or ""

        if reason == "QUOTEABILITY_GUARD_EXIT:no_pool":
            # No pool exists — window length irrelevant, same loss regardless
            results.append((t.roi_pct, t.pnl_usd or size * t.roi_pct / 100))

        elif reason == "QUOTEABILITY_GUARD_EXIT:transient":
            if window_s >= QUOTEABILITY_WINDOW_S:
                # Longer window: transient glitch resolves → assume break-even (0% ROI)
                # This is optimistic but reasonable for a transient failure
                results.append((0.0, 0.0))
            else:
                # Shorter window: exit sooner = same loss (still no quote)
                results.append((t.roi_pct, t.pnl_usd or size * t.roi_pct / 100))

        elif reason == "QUOTEABILITY_GUARD_EXIT":
            # Ambiguous; shorter or longer window unlikely to change outcome materially
            results.append((t.roi_pct, t.pnl_usd or size * t.roi_pct / 100))

        else:
            # Not a quoteability exit — unaffected by window
            results.append((t.roi_pct, t.pnl_usd if t.pnl_usd is not None else size * t.roi_pct / 100))

    return results


# ── Markdown formatting ───────────────────────────────────────────────────────
def md_table(headers: List[str], rows: List[List[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))

    def fmt(cells: List[str]) -> str:
        return "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    return "\n".join([fmt(headers), sep] + [fmt(r) for r in rows])

def fmt_f(v: Optional[float], decimals: int = 1, suffix: str = "") -> str:
    if v is None:
        return "—"
    return f"{v:.{decimals}f}{suffix}"

def fmt_pct(v: Optional[float]) -> str:
    return fmt_f(v, 1, "%")

def fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:.4f}"

def ts_fmt(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── Report builder ────────────────────────────────────────────────────────────
def build_report(history: List[dict], state: dict) -> Tuple[str, dict]:
    trades = reconstruct_trades(history)

    # Subset: all trades with actual close data
    all_closed = [t for t in trades if t.roi_pct is not None]

    # ── Contamination filter (P4.6: SSOT in constants.py — shared with replay_v2) ─
    from constants import is_contaminated as _is_contaminated
    contaminated = [t for t in all_closed if _is_contaminated(t)]
    closed = [t for t in all_closed if not _is_contaminated(t)]  # used throughout

    # Trades with meaningful mark data (MFE/MAE captured)
    marked = [t for t in closed if t.close_reason not in NO_MARK_REASONS
              or (t.mfe_pct and abs(t.mfe_pct) > 0)]
    # Trades with MFE data for TP counterfactuals
    mfe_trades = [t for t in closed if t.mfe_pct is not None and t.mfe_pct != 0]
    # All latency records across ALL execution_attempts (not just matched trades)
    detect_latencies = all_exec_latencies(history)
    submit_latencies = all_exec_submit_latencies(history)

    # ── Section 0: header ────────────────────────────────────────────────────
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Sniperbot Replay Analysis Report",
        f"_Generated: {now_str}_",
        f"_History records: {len(history)} | Total closes: {len(all_closed)} | "
        f"Excluded (contaminated): **{len(contaminated)}** | "
        f"Clean closes: **{len(closed)}** | "
        f"With mark data: {len(marked)} | With nonzero MFE: {len(mfe_trades)}_",
        "",
    ]

    # ── Section 1: raw dataset overview ─────────────────────────────────────
    lines += ["## 1. Dataset Overview", ""]

    # Contamination breakdown
    contam_reasons = Counter(t.close_reason for t in contaminated)
    contam_rows = []
    for reason, cnt in contam_reasons.most_common():
        contam_rows.append([reason or "unknown", str(cnt)])
    lines += [
        f"### Contamination Filter — {len(contaminated)} Trades Excluded",
        "",
        "> Contaminated = `sell_attempt_ts=0.0` (ghost close, no TX) OR `proceeds_usd=0.0` "
        "OR `roi_pct ≤ −99.0` (corrupted accounting). Excluded from ALL analysis below.",
        "",
    ]
    if contam_rows:
        lines.append(md_table(["Close Reason", "Excluded Count"], contam_rows))
    lines.append("")

    close_reasons = Counter(t.close_reason for t in closed)
    reason_rows = []
    for reason, cnt in close_reasons.most_common():
        subset = [t for t in closed if t.close_reason == reason]
        avg_roi = sum(t.roi_pct for t in subset) / len(subset)
        total_pnl = sum(t.pnl_usd or 0 for t in subset)
        pct_of_total = cnt / len(closed) * 100
        reason_rows.append([reason, str(cnt), f"{pct_of_total:.1f}%",
                             fmt_pct(avg_roi), fmt_usd(total_pnl)])

    lines.append("### Close Reason Distribution")
    lines.append("")
    lines.append(md_table(
        ["Close Reason", "Count", "% of Closes", "Avg ROI", "Total P&L"],
        reason_rows,
    ))
    lines.append("")

    # Overall realized P&L — P4.2: canonical win = pnl_usd > 0.
    wins   = [t for t in closed if (t.pnl_usd or 0) > 0]
    losses = [t for t in closed if (t.pnl_usd or 0) <= 0]
    total_pnl_all = sum(t.pnl_usd or 0 for t in closed)
    lines += [
        "### Realized Performance (All Closes)",
        "",
        f"- **Total trades closed:** {len(closed)}",
        f"- **Wins / Losses:** {len(wins)} / {len(losses)} "
        f"({len(wins)/len(closed)*100:.1f}% win rate)" if closed else "",
        f"- **Total realized P&L:** {fmt_usd(total_pnl_all)}",
        f"- **Average ROI per trade:** {fmt_pct(sum(t.roi_pct for t in closed)/len(closed) if closed else None)}",
        f"- **Average hold time:** {fmt_f(sum(t.hold_time_s or 0 for t in closed)/len(closed) if closed else None, 0, 's')}",
        "",
    ]

    # ── Section 2: latency distribution ─────────────────────────────────────
    lines += ["## 2. Latency Distribution", ""]

    if detect_latencies:
        dl = sorted(detect_latencies)
        n = len(dl)
        lines += [
            "### Detection → Execution Latency (`detect_to_exec_ms`)",
            "",
            f"_N = {n} execution attempts with latency data_",
            "",
            md_table(
                ["Percentile", "Latency", "Trades > this"],
                [
                    ["p25", f"{percentile(dl, 25):.0f} ms", f"{sum(1 for d in dl if d > percentile(dl, 25))}/{n}"],
                    ["p50", f"{percentile(dl, 50):.0f} ms", f"{sum(1 for d in dl if d > percentile(dl, 50))}/{n}"],
                    ["p75", f"{percentile(dl, 75):.0f} ms", f"{sum(1 for d in dl if d > percentile(dl, 75))}/{n}"],
                    ["p90", f"{percentile(dl, 90):.0f} ms", f"{sum(1 for d in dl if d > percentile(dl, 90))}/{n}"],
                    ["p99", f"{percentile(dl, 99):.0f} ms", f"{sum(1 for d in dl if d > percentile(dl, 99))}/{n}"],
                ],
            ),
            "",
            f"- **> 3 000 ms:** {sum(1 for d in dl if d > 3000)} / {n} "
            f"({sum(1 for d in dl if d > 3000)/n*100:.1f}%)",
            f"- **> 5 000 ms:** {sum(1 for d in dl if d > 5000)} / {n} "
            f"({sum(1 for d in dl if d > 5000)/n*100:.1f}%)",
            "",
        ]
    else:
        lines += ["_No `detect_to_exec_ms` data found._", ""]

    if submit_latencies:
        sl = sorted(submit_latencies)
        n = len(sl)
        lines += [
            "### Execution Submit Latency (`execution_latency_ms`)",
            "",
            f"_N = {n} execution attempts_",
            "",
            md_table(
                ["Percentile", "Latency"],
                [
                    ["p50", f"{percentile(sl, 50):.0f} ms"],
                    ["p75", f"{percentile(sl, 75):.0f} ms"],
                    ["p90", f"{percentile(sl, 90):.0f} ms"],
                    ["p99", f"{percentile(sl, 99):.0f} ms"],
                ],
            ),
            "",
        ]

    # ── Section 3: counterfactual summary table ──────────────────────────────
    lines += ["## 3. Counterfactual Scenarios", ""]
    lines += [
        "> **Note on scope:** Stop-loss and TP counterfactuals run on ALL closed trades.",
        "> Trades with `mfe_pct = 0` (QUOTEABILITY_GUARD_EXIT, STALE_MARK exits, etc.)",
        "> have no mark data — their outcome is unchanged across stop/TP scenarios.",
        "> Latency filter counterfactuals run on the execution_attempt population.",
        "",
    ]

    # Baseline
    baseline_results = [(t.roi_pct, t.pnl_usd or 0) for t in closed if t.roi_pct is not None]
    baseline = scenario_stats(baseline_results)

    # All scenarios
    scenarios: List[Tuple[str, str, dict, Optional[str]]] = []

    def add_scenario(label: str, description: str,
                     results: List[Tuple[float, float]],
                     note: Optional[str] = None):
        scenarios.append((label, description, scenario_stats(results), note))

    # Baseline
    add_scenario("Baseline (actual)", "Current bot behavior", baseline_results)

    # ── Stop loss variants (hard stop from entry)
    for pct in [0.06, 0.08, 0.10, 0.12, 0.15, 0.20]:
        label = f"Stop loss −{int(pct*100)}%"
        note = "← current" if pct == DEFAULT_STOP_PCT else None
        add_scenario(label, f"Hard floor at −{int(pct*100)}% ROI",
                     cf_stop_loss(closed, pct), note)

    # ── Trailing stop from MFE (only meaningful for marked trades)
    # −6% and −7% added: current −8% may be too loose given short avg hold times
    for pct in [0.06, 0.07, 0.08, 0.12, 0.15, 0.20]:
        label = f"Trail stop −{int(pct*100)}% from MFE"
        note = "← current est." if pct == DEFAULT_STOP_PCT else None
        add_scenario(label, f"Trailing stop {int(pct*100)}% below MFE peak",
                     cf_trailing_stop_from_mfe(mfe_trades, pct), note)

    # ── Latency filter
    for max_ms in [2000, 3000, 5000, 10000]:
        filtered = cf_latency_filter(closed, max_ms)
        kept = [(t.roi_pct, t.pnl_usd or 0) for t in filtered if t.roi_pct is not None]
        skipped = len(closed) - len(filtered)
        add_scenario(
            f"Latency filter ≤{max_ms}ms",
            f"Skip entries where detection→exec > {max_ms}ms",
            kept,
            f"skips {skipped}/{len(closed)} trades with known late latency",
        )

    # ── Partial TP
    for (tp_pct, tp_sell, label_extra) in [
        (0.20, 0.30, "new"),
        (0.30, 0.40, "TP1 current"),
        (0.50, 0.30, "new"),
        (1.00, 0.25, "new"),
    ]:
        label = f"Partial TP +{int(tp_pct*100)}% (sell {int(tp_sell*100)}%)"
        add_scenario(label, f"Exit {int(tp_sell*100)}% at +{int(tp_pct*100)}%",
                     cf_partial_tp(closed, tp_pct*100, tp_sell), label_extra)

    # ── Quoteability window
    for ws in [10, 20, 35, 60]:
        note = "← current" if ws == QUOTEABILITY_WINDOW_S else None
        add_scenario(
            f"Quoteability window {ws}s",
            f"Exit if no quote within {ws}s of entry",
            cf_quoteability_window(closed, ws),
            note,
        )

    # Build summary table
    sum_headers = ["Scenario", "N", "Wins", "Loss", "Win%", "Avg ROI", "Avg P&L", "Total P&L", "Note"]
    sum_rows = []
    for label, desc, stats, note in scenarios:
        sum_rows.append([
            label,
            str(stats["n"]),
            str(stats["wins"]),
            str(stats["losses"]),
            f"{stats['win_rate']:.1f}%",
            f"{stats['avg_roi']:.2f}%",
            fmt_usd(stats["avg_pnl"]),
            fmt_usd(stats["total_pnl"]),
            note or "",
        ])

    lines.append(md_table(sum_headers, sum_rows))
    lines.append("")

    # ── Section 4: stop loss deep dive ───────────────────────────────────────
    lines += ["## 4. Stop Loss Deep Dive", ""]
    lines += [
        "Trades where stop level matters = trades where MAE breaches the stop threshold.",
        f"Total closed trades: **{len(closed)}** | With nonzero MAE: "
        f"**{sum(1 for t in closed if t.mae_pct and t.mae_pct < 0)}**",
        "",
    ]

    sl_header = ["Stop Level", "Trades Stopped", "Avg ROI (stopped)", "Avg ROI (not stopped)", "Total P&L Δ vs baseline"]
    sl_rows = []
    baseline_total = sum(t.pnl_usd or 0 for t in closed)
    for pct in [0.06, 0.08, 0.10, 0.12, 0.15, 0.20]:
        stopped = [t for t in closed if t.mae_pct is not None and t.mae_pct <= -(pct * 100)]
        not_stopped = [t for t in closed if t.mae_pct is None or t.mae_pct > -(pct * 100)]
        results = cf_stop_loss(closed, pct)
        sim_total = sum(p for _, p in results)
        delta = sim_total - baseline_total
        sign = "+" if delta >= 0 else ""
        sl_rows.append([
            f"−{int(pct*100)}%{'  ←' if pct == DEFAULT_STOP_PCT else ''}",
            str(len(stopped)),
            fmt_pct(sum(t.roi_pct for t in stopped)/len(stopped) if stopped else None),
            fmt_pct(sum(t.roi_pct for t in not_stopped)/len(not_stopped) if not_stopped else None),
            f"{sign}${delta:.4f}",
        ])
    lines.append(md_table(sl_header, sl_rows))
    lines.append("")

    # ── Section 5: latency filter impact ─────────────────────────────────────
    lines += ["## 5. Latency Filter Impact", ""]

    # Show how many trades we know latency for (linked to execution_attempt)
    trades_with_latency = [t for t in closed if t.detect_to_exec_ms is not None]
    lines += [
        f"Closed trades with latency data: **{len(trades_with_latency)}** / {len(closed)}",
        f"_(Trades without latency data are **kept** under all filter scenarios.)_",
        "",
    ]
    lat_header = ["Max Latency", "Trades Kept", "Trades Skipped", "Win%", "Avg ROI", "Total P&L"]
    lat_rows = []
    for max_ms in [2000, 3000, 5000, 10000]:
        kept = cf_latency_filter(closed, max_ms)
        skipped = len(closed) - len(kept)
        kept_results = [(t.roi_pct, t.pnl_usd or 0) for t in kept if t.roi_pct is not None]
        st = scenario_stats(kept_results)
        lat_rows.append([
            f"≤{max_ms}ms",
            str(len(kept)),
            str(skipped),
            f"{st['win_rate']:.1f}%",
            f"{st['avg_roi']:.2f}%",
            fmt_usd(st["total_pnl"]),
        ])
    lines.append(md_table(lat_header, lat_rows))
    lines.append("")

    # ── Section 6: MFE vs exit — missed upside ───────────────────────────────
    lines += ["## 6. MFE vs Actual Exit — Missed Upside", ""]

    if mfe_trades:
        mfe_gaps = [(t.mfe_pct - t.roi_pct, t) for t in mfe_trades]
        mfe_gaps.sort(key=lambda x: -x[0])
        avg_mfe = sum(t.mfe_pct for t in mfe_trades) / len(mfe_trades)
        avg_roi = sum(t.roi_pct for t in mfe_trades) / len(mfe_trades)
        avg_gap = avg_mfe - avg_roi

        lines += [
            f"_Trades with nonzero MFE: **{len(mfe_trades)}**_",
            f"",
            f"- **Avg MFE:**      {fmt_pct(avg_mfe)}",
            f"- **Avg exit ROI:** {fmt_pct(avg_roi)}",
            f"- **Avg gap:**      {fmt_pct(avg_gap)}  _(upside left on the table per trade)_",
            "",
            "### Top 10 Trades by Missed Upside (MFE − Exit ROI)",
            "",
        ]

        top_rows = []
        for gap, t in mfe_gaps[:10]:
            top_rows.append([
                t.mint[:12] + "…",
                fmt_pct(t.mfe_pct),
                fmt_pct(t.roi_pct),
                fmt_pct(gap),
                t.close_reason or "—",
                f"{t.hold_time_s:.0f}s" if t.hold_time_s else "—",
            ])
        lines.append(md_table(
            ["Mint (prefix)", "MFE", "Exit ROI", "Gap", "Exit Reason", "Hold Time"],
            top_rows,
        ))
        lines.append("")

        # MFE buckets
        buckets = [
            (0, 10, "0–10%"),
            (10, 30, "10–30%"),
            (30, 75, "30–75%"),
            (75, 200, "75–200%"),
            (200, float("inf"), ">200%"),
        ]
        lines += ["### MFE Distribution Across Marked Trades", ""]
        bucket_rows = []
        for lo, hi, label in buckets:
            subset = [t for t in mfe_trades if lo <= t.mfe_pct < hi]
            if not subset:
                continue
            avg_exit = sum(t.roi_pct for t in subset) / len(subset)
            avg_gap_ = sum(t.mfe_pct - t.roi_pct for t in subset) / len(subset)
            bucket_rows.append([label, str(len(subset)),
                                 fmt_pct(sum(t.mfe_pct for t in subset)/len(subset)),
                                 fmt_pct(avg_exit), fmt_pct(avg_gap_)])
        lines.append(md_table(
            ["MFE Range", "Count", "Avg MFE", "Avg Exit ROI", "Avg Gap"],
            bucket_rows,
        ))
        lines.append("")
    else:
        lines += ["_No trades with nonzero MFE data._", ""]

    # ── Section 7: partial TP deep dive ──────────────────────────────────────
    lines += ["## 7. Partial Take-Profit Scenarios (on Marked Trades)", ""]
    lines += [
        f"_Basis: {len(mfe_trades)} trades with nonzero MFE. TP fires only when MFE ≥ TP level._",
        "",
    ]

    tp_header = ["TP Level", "Sell Frac", "TP Fires", "Avg Blended ROI", "Avg P&L", "Total P&L"]
    tp_rows = []
    for (tp_pct_raw, tp_sell, note) in [
        (20, 0.30, ""),
        (30, 0.40, "← current TP1"),
        (50, 0.30, ""),
        (75, 0.20, "← current TP2"),
        (100, 0.25, ""),
        (200, 0.20, "← current TP3"),
    ]:
        results = cf_partial_tp(mfe_trades, float(tp_pct_raw), tp_sell)
        fires = sum(1 for t in mfe_trades if t.mfe_pct >= tp_pct_raw)
        st = scenario_stats(results)
        tp_rows.append([
            f"+{tp_pct_raw}% {note}",
            f"{int(tp_sell*100)}%",
            f"{fires} / {len(mfe_trades)}",
            f"{st['avg_roi']:.2f}%",
            fmt_usd(st["avg_pnl"]),
            fmt_usd(st["total_pnl"]),
        ])
    lines.append(md_table(tp_header, tp_rows))
    lines.append("")

    # ── Section 8: quoteability analysis ────────────────────────────────────
    lines += ["## 8. Quoteability Exit Analysis", ""]

    q_exits = [t for t in closed if (t.close_reason or "").startswith("QUOTEABILITY_GUARD_EXIT")]
    q_no_pool   = [t for t in q_exits if t.close_reason == "QUOTEABILITY_GUARD_EXIT:no_pool"]
    q_transient = [t for t in q_exits if t.close_reason == "QUOTEABILITY_GUARD_EXIT:transient"]
    q_base      = [t for t in q_exits if t.close_reason == "QUOTEABILITY_GUARD_EXIT"]

    total_q_pnl = sum(t.pnl_usd or 0 for t in q_exits)
    lines += [
        f"**Quoteability exits: {len(q_exits)} / {len(closed)} "
        f"({len(q_exits)/len(closed)*100:.1f}% of all closes)**",
        "",
        f"- Base `QUOTEABILITY_GUARD_EXIT`:          {len(q_base)} trades  "
        f"(avg ROI {fmt_pct(sum(t.roi_pct for t in q_base)/len(q_base) if q_base else None)})",
        f"- `QUOTEABILITY_GUARD_EXIT:no_pool`:       {len(q_no_pool)} trades  "
        f"(avg ROI {fmt_pct(sum(t.roi_pct for t in q_no_pool)/len(q_no_pool) if q_no_pool else None)})",
        f"- `QUOTEABILITY_GUARD_EXIT:transient`:     {len(q_transient)} trades  "
        f"(avg ROI {fmt_pct(sum(t.roi_pct for t in q_transient)/len(q_transient) if q_transient else None)})",
        f"- **Total P&L from quoteability exits:    {fmt_usd(total_q_pnl)}**",
        "",
        "### Window Sensitivity",
        "",
        "_(Assumptions: `no_pool` exits are unaffected by window length. "
        "`transient` exits resolve at 0% ROI with longer window. Base exits unchanged.)_",
        "",
    ]

    q_header = ["Window", "Total Trades", "Win%", "Avg ROI", "Total P&L", "vs Current"]
    q_rows = []
    current_q_total = sum(p for _, p in cf_quoteability_window(closed, QUOTEABILITY_WINDOW_S))
    for ws in [10, 20, 35, 60, 120]:
        results = cf_quoteability_window(closed, float(ws))
        st = scenario_stats(results)
        delta = st["total_pnl"] - current_q_total
        sign = "+" if delta >= 0 else ""
        note = " ← current" if ws == QUOTEABILITY_WINDOW_S else ""
        q_rows.append([
            f"{ws}s{note}", str(st["n"]),
            f"{st['win_rate']:.1f}%",
            f"{st['avg_roi']:.2f}%",
            fmt_usd(st["total_pnl"]),
            f"{sign}${delta:.4f}",
        ])
    lines.append(md_table(q_header, q_rows))
    lines.append("")

    # ── Section 9: top 5 by MFE — what could have been ──────────────────────
    lines += ["## 9. Top 5 Trades by MFE — Outcome vs Potential", ""]

    if mfe_trades:
        top5 = sorted(mfe_trades, key=lambda t: t.mfe_pct, reverse=True)[:5]
        for i, t in enumerate(top5, 1):
            size = t.realized_basis_usd or t.exec_size_usd or 0.5
            # What trailing stop from MFE would have yielded
            trail_12_roi = ((1 + t.mfe_pct/100) * (1 - 0.12) - 1) * 100
            trail_20_roi = ((1 + t.mfe_pct/100) * (1 - 0.20) - 1) * 100
            # What a +20% partial TP would have added
            partial_20_roi = 0.30 * 20.0 + 0.70 * t.roi_pct if t.mfe_pct >= 20 else t.roi_pct

            lines += [
                f"### #{i}: `{t.mint[:16]}…`",
                "",
                md_table(
                    ["Metric", "Value"],
                    [
                        ["MFE (peak gain)", fmt_pct(t.mfe_pct)],
                        ["MAE (max drawdown)", fmt_pct(t.mae_pct)],
                        ["Actual exit ROI", fmt_pct(t.roi_pct)],
                        ["Actual P&L", fmt_usd(t.pnl_usd)],
                        ["Close reason", t.close_reason or "—"],
                        ["Hold time", f"{t.hold_time_s:.0f}s" if t.hold_time_s else "—"],
                        ["Source", t.exec_source or t.signal_source or "—"],
                        ["Detect→exec latency", f"{t.detect_to_exec_ms:.0f}ms" if t.detect_to_exec_ms else "—"],
                        ["─── Counterfactuals ───", ""],
                        ["Trail stop −12% from MFE → ROI", fmt_pct(trail_12_roi)],
                        ["Trail stop −20% from MFE → ROI", fmt_pct(trail_20_roi)],
                        ["Partial TP +20% (sell 30%) → blended", fmt_pct(partial_20_roi)],
                        ["Missed upside (MFE − exit)", fmt_pct(t.mfe_pct - t.roi_pct)],
                    ],
                ),
                "",
            ]
    else:
        lines += ["_No trades with nonzero MFE._", ""]

    # ── Section 10: MFE capture rate ─────────────────────────────────────────
    lines += ["## 10. MFE Capture Rate", ""]
    lines += [
        "> **Capture rate** = `realized_roi_pct / mfe_pct`. "
        "1.0 (100%) = exited exactly at peak. <0 = sold after price reversed past entry. "
        ">1.0 = exited above MFE (rounding / partial-fill artifact).",
        "",
    ]

    cap_trades = [t for t in closed if t.mfe_pct is not None and t.mfe_pct > 0]
    if cap_trades:
        cap_rates = [t.roi_pct / t.mfe_pct for t in cap_trades]
        cap_sorted = sorted(cap_rates)
        avg_cap = sum(cap_rates) / len(cap_rates)

        lines += [
            f"_Clean trades with MFE > 0: **{len(cap_trades)}**_",
            "",
            md_table(
                ["Metric", "Value"],
                [
                    ["Avg capture rate", f"{avg_cap:.2%}"],
                    ["p25 capture rate", f"{percentile(cap_sorted, 25):.2%}"],
                    ["p50 capture rate", f"{percentile(cap_sorted, 50):.2%}"],
                    ["p75 capture rate", f"{percentile(cap_sorted, 75):.2%}"],
                ],
            ),
            "",
        ]

        cap_buckets = [
            ("<0%",    lambda r: r < 0),
            ("0–25%",  lambda r: 0 <= r < 0.25),
            ("25–50%", lambda r: 0.25 <= r < 0.50),
            ("50–75%", lambda r: 0.50 <= r < 0.75),
            ("75–100%",lambda r: 0.75 <= r < 1.00),
            (">100%",  lambda r: r >= 1.00),
        ]
        hist_rows = []
        for bucket_label, predicate in cap_buckets:
            subset = [r for r in cap_rates if predicate(r)]
            hist_rows.append([bucket_label, str(len(subset)),
                              f"{len(subset)/len(cap_rates)*100:.1f}%"])
        lines.append(md_table(["Capture Rate Bucket", "Count", "% of Trades"], hist_rows))
        lines.append("")
    else:
        lines += ["_No clean trades with MFE > 0._", ""]

    # ── Section 11: hold time vs outcome ─────────────────────────────────────
    lines += ["## 11. Hold Time vs Outcome", ""]

    ht_buckets = [
        ("<5s",     0,   5),
        ("5–15s",   5,   15),
        ("15–30s",  15,  30),
        ("30–60s",  30,  60),
        ("60–120s", 60,  120),
        (">120s",   120, float("inf")),
    ]
    ht_rows = []
    for ht_label, lo, hi in ht_buckets:
        subset = [t for t in closed if t.hold_time_s is not None and lo <= t.hold_time_s < hi]
        if not subset:
            ht_rows.append([ht_label, "0", "—", "—", "—", "—"])
            continue
        wins_ht = [t for t in subset if t.roi_pct > 0]
        avg_roi_ht = sum(t.roi_pct for t in subset) / len(subset)
        avg_mfe_ht = sum(t.mfe_pct for t in subset if t.mfe_pct is not None) / max(1, sum(1 for t in subset if t.mfe_pct is not None))
        cap_sub = [t.roi_pct / t.mfe_pct for t in subset if t.mfe_pct and t.mfe_pct > 0]
        avg_cap_ht = sum(cap_sub) / len(cap_sub) if cap_sub else None
        ht_rows.append([
            ht_label,
            str(len(subset)),
            f"{len(wins_ht)/len(subset)*100:.1f}%",
            fmt_pct(avg_roi_ht),
            fmt_pct(avg_mfe_ht),
            f"{avg_cap_ht:.2%}" if avg_cap_ht is not None else "—",
        ])
    lines.append(md_table(
        ["Hold Time", "Count", "Win%", "Avg ROI", "Avg MFE", "Avg Capture Rate"],
        ht_rows,
    ))
    lines.append("")

    # ── Section 12: entry price deduplication ────────────────────────────────
    lines += ["## 12. Entry Price Deduplication Check", ""]
    lines += [
        "> An entry price appearing in >3 trades is suspicious — it may indicate a "
        "stuck reference price feed recycling the same value across unrelated tokens.",
        "",
    ]

    price_to_mints: Dict[float, List[str]] = defaultdict(list)
    for t in closed:
        if t.entry_price is not None and t.entry_price > 0:
            price_to_mints[t.entry_price].append(t.mint)

    flagged = {p: mints for p, mints in price_to_mints.items() if len(mints) > 3}
    total_flagged_trades = sum(len(m) for m in flagged.values())

    if flagged:
        lines += [
            f"**{len(flagged)} entry price(s) appear in >3 trades "
            f"({total_flagged_trades} / {len(closed)} clean trades affected)**",
            "",
        ]
        flag_rows = []
        for price in sorted(flagged, key=lambda p: -len(flagged[p])):
            mints = flagged[price]
            sample = ", ".join(f"`{m[:14]}…`" for m in mints[:4])
            if len(mints) > 4:
                sample += f" +{len(mints)-4} more"
            flag_rows.append([f"{price:.4e}", str(len(mints)), sample])
        lines.append(md_table(["Entry Price", "Trade Count", "Sample Mints"], flag_rows))
        lines.append("")
        lines += [
            "**Interpretation:** These prices are almost certainly stuck feed values, not real "
            "reference prices. Trades using a recycled entry price have unreliable entry/exit "
            "ROI comparisons and stop/TP trigger points.",
            "",
        ]
    else:
        count_with_ep = sum(1 for t in closed if t.entry_price is not None)
        lines += [
            f"No suspicious price repetition found "
            f"({count_with_ep} / {len(closed)} clean trades have entry price data).",
            "",
        ]

    # ── Section 13: strategy observations ────────────────────────────────────
    lines += ["## 13. Key Observations & Recommendations", ""]

    obs = []

    # QUOTEABILITY dominance
    q_pct = len(q_exits) / len(closed) * 100 if closed else 0
    if q_pct > 30:
        obs.append(
            f"**QUOTEABILITY_GUARD_EXIT is {q_pct:.0f}% of all closes** — "
            f"the dominant failure mode. These trades enter but Jupiter can't quote them "
            f"within `QUOTEABILITY_VALIDATION_WINDOW_SECONDS={QUOTEABILITY_WINDOW_S}s`. "
            f"Total P&L from these exits: {fmt_usd(total_q_pnl)}. "
            f"Primary fix: ensure entry only happens after route is confirmed (already in `_pre_entry_swap_route_check`)."
        )

    # Trail/SL exits
    trail_exits = [t for t in closed if t.close_reason == "TRAIL/SL EXIT"]
    if trail_exits and mfe_trades:
        avg_trail_mfe = sum(t.mfe_pct for t in trail_exits if t.mfe_pct) / sum(1 for t in trail_exits if t.mfe_pct)
        avg_trail_roi = sum(t.roi_pct for t in trail_exits) / len(trail_exits)
        obs.append(
            f"**Trail/SL exits leave significant upside on the table:** "
            f"{len(trail_exits)} TRAIL/SL EXIT trades have avg MFE={fmt_pct(avg_trail_mfe)} "
            f"but avg exit ROI={fmt_pct(avg_trail_roi)}. "
            f"Gap of {fmt_pct(avg_trail_mfe - avg_trail_roi)} suggests the trailing stop "
            f"triggers too aggressively near entry before the pump completes."
        )

    # Latency
    if detect_latencies:
        over3k = sum(1 for d in detect_latencies if d > 3000)
        if over3k > 0:
            obs.append(
                f"**{over3k}/{len(detect_latencies)} executions ({over3k/len(detect_latencies)*100:.0f}%) "
                f"had detection→exec latency > 3 000 ms.** "
                f"The latency filter counterfactuals above show the P&L impact of skipping late entries."
            )

    # MFE gap
    if mfe_trades:
        avg_gap = sum(t.mfe_pct - t.roi_pct for t in mfe_trades) / len(mfe_trades)
        if avg_gap > 20:
            obs.append(
                f"**Average MFE−exit gap is {fmt_pct(avg_gap)} across {len(mfe_trades)} marked trades.** "
                f"Adding an early partial TP (e.g. +20% sell 30%) would lock in gains before "
                f"the trailing stop fires on the way back down."
            )

    if not obs:
        obs.append("Insufficient data for automated observations — run longer sessions.")

    for o in obs:
        lines.append(f"- {o}")
        lines.append("")

    # ── Footer
    lines += [
        "---",
        "_Replay analysis is read-only. No simulation_mode.py logic was modified._",
        f"_Source: `{HISTORY_FILE}`_",
    ]

    report = "\n".join(lines)

    # Structured JSON output
    structured = {
        "generated_at": now_str,
        "dataset": {
            "total_history_records": len(history),
            "total_closes_raw": len(all_closed),
            "contaminated_excluded": len(contaminated),
            "clean_closes": len(closed),
            "marked_trades": len(marked),
            "mfe_trades": len(mfe_trades),
        },
        "baseline": baseline,
        "scenarios": [
            {"label": label, "description": desc, "stats": stats, "note": note}
            for label, desc, stats, note in scenarios
        ],
        "close_reason_distribution": dict(close_reasons.most_common()),
        "latency": {
            "detect_to_exec_ms": {
                "n": len(detect_latencies),
                "p50": percentile(detect_latencies, 50),
                "p90": percentile(detect_latencies, 90),
                "p99": percentile(detect_latencies, 99),
                "over_3000ms": sum(1 for d in detect_latencies if d > 3000),
                "over_5000ms": sum(1 for d in detect_latencies if d > 5000),
            },
            "execution_submit_ms": {
                "n": len(submit_latencies),
                "p50": percentile(submit_latencies, 50),
                "p90": percentile(submit_latencies, 90),
                "p99": percentile(submit_latencies, 99),
            },
        },
    }

    return report, structured


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Replay analyzer for sniperbot history.json")
    parser.add_argument("--json", action="store_true", help="Also write REPLAY_REPORT.json")
    parser.add_argument("--no-report", action="store_true", help="Print to stdout, skip file write")
    parser.add_argument("--stdout", action="store_true", help="Print report to stdout as well")
    args = parser.parse_args()

    print(f"Loading {HISTORY_FILE} …", file=sys.stderr)
    history = load_history()
    state   = load_state()

    if not history:
        print("[ERROR] No history data — run the bot first to generate history.json", file=sys.stderr)
        sys.exit(1)

    print(f"Reconstructing trades from {len(history)} events …", file=sys.stderr)
    report, structured = build_report(history, state)

    if args.no_report:
        print(report)
        return

    REPORT_FILE.write_text(report, encoding="utf-8")
    print(f"✓ Report written to {REPORT_FILE}", file=sys.stderr)

    if args.json:
        json_path = REPORT_FILE.with_suffix(".json")
        json_path.write_text(json.dumps(structured, indent=2), encoding="utf-8")
        print(f"✓ JSON written to {json_path}", file=sys.stderr)

    if args.stdout:
        print(report)


if __name__ == "__main__":
    main()
