#!/usr/bin/env python3
"""
replay_v2.py — Post-fix trade replay with updated filters.
 
Filters applied vs replay_analyzer.py:
  - Contamination: sell_attempt_ts=0.0 OR proceeds_usd=0.0 OR roi_pct <= -99%
  - Post-fix cutoff: close_ts > 1775682000 (2026-04-08 21:00 UTC)
    (rapid-dump guard + sell-retry 6001 landed; code was clean after this point)
  - price_suspect flag: entry_price within 0.01% of 6.162627048e-05 or 1.3e-05
    (graduation curve constant or legacy reference price constant — treat separately)
 
Reports:
  - Win rate on clean post-fix trades
  - MFE capture rate
  - Trail stop -8% vs -6% counterfactual
  - Hold time distribution
  - price_suspect vs real-price-entry comparison
 
Output: REPLAY_REPORT_V2.md (same directory as this script)
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
 
# ── Constants ─────────────────────────────────────────────────────────────────
POST_FIX_CUTOFF_TS = 1775682000.0   # 2026-04-08 21:00 UTC
 
# P4.7: suspect-price constants live in constants.py (SSOT). Re-export for
# back-compat with any external tooling that imports replay_v2.SUSPECT_PRICES.
from constants import SUSPECT_PRICES, SUSPECT_TOL, is_suspect_price as _c_is_suspect
 
# Bot defaults — P4.1: single source of truth in constants.py
from constants import (
    DEFAULT_STOP_PCT,
    FAILSAFE_PCT,
    TP1_PCT, TP1_SELL,
    TP2_PCT, TP2_SELL,
    TP3_PCT, TP3_SELL,
)
QUOTEABILITY_WINDOW_S = 35
 
NO_MARK_REASONS = {
    "QUOTEABILITY_GUARD_EXIT",
    "QUOTEABILITY_GUARD_EXIT:no_pool",
    "QUOTEABILITY_GUARD_EXIT:transient",
    "STALE_MARK_TIMEOUT_EXIT",
    "AGE_GATE_EXIT",
    "FAILOVER_LIQUIDATION",
}
 
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
 
HISTORY_FILE = _find("history.json")
REPORT_FILE  = Path(__file__).parent / "REPLAY_REPORT_V2.md"
 
 
# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Trade:
    mint: str
    signal_ts: Optional[float] = None
    signal_price: Optional[float] = None
    signal_source: Optional[str] = None
    candidate_age_ms: Optional[float] = None
    reference_price: Optional[float] = None
    exec_ts: Optional[float] = None
    exec_ok: Optional[bool] = None
    exec_error: Optional[str] = None
    exec_latency_ms: Optional[float] = None
    detect_to_exec_ms: Optional[float] = None
    ts_detected: Optional[float] = None
    exec_size_usd: Optional[float] = None
    exec_source: Optional[str] = None
    decision_breakdown: Optional[dict] = None
    partials: List[dict] = field(default_factory=list)
    entry_ts: Optional[float] = None
    close_ts: Optional[float] = None
    hold_time_s: Optional[float] = None
    pnl_usd: Optional[float] = None
    roi_pct: Optional[float] = None
    close_reason: Optional[str] = None
    mfe_pct: Optional[float] = None      # P4.9: sanity-capped value (<= MFE_SANITY_CAP_PCT)
    mfe_pct_raw: Optional[float] = None   # P4.9: raw value as observed (may be millions for corrupted records)
    mfe_capped: bool = False              # P4.9: True iff cap was triggered — filter these out for averages
    mae_pct: Optional[float] = None
    exit_price: Optional[float] = None
    realized_basis_usd: Optional[float] = None
    proceeds_usd: Optional[float] = None
    sell_attempt_ts: Optional[float] = None
    entry_price: Optional[float] = None
    # V2 additions
    price_suspect: bool = False
    fill_price_usd: Optional[float] = None   # actual Jupiter fill price (None = pre-fix or unavailable)
    reference_price_is_real: Optional[bool] = None  # from execution_attempt event
 
 
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
 
 
# ── Reconstruction ────────────────────────────────────────────────────────────
# P4.7: _is_suspect_price delegates to the canonical impl in constants.py.
def _is_suspect_price(px: Optional[float]) -> bool:
    return _c_is_suspect(px) if px is not None else False
 
 
def reconstruct_trades(history: List[dict]) -> List[Trade]:
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
            close_ts = float(close_ev.get("ts") or 0)
 
            t.entry_ts           = entry_ts
            t.close_ts           = close_ts
            t.hold_time_s        = float(close_ev.get("hold_time_seconds") or 0)
            t.pnl_usd            = float(close_ev.get("pnl_usd") or 0)
            t.roi_pct            = float(close_ev.get("roi_pct") or 0)
            t.exit_price         = close_ev.get("exit_price")
            t.close_reason       = close_ev.get("close_reason") or "unknown"
            # P4.9: preserve raw MFE, expose cap as a flag column instead of silently clipping.
            from constants import cap_mfe as _cap_mfe
            _raw_mfe = float(close_ev.get("mfe_pct") or 0)
            _capped_mfe, _was_capped = _cap_mfe(_raw_mfe)
            t.mfe_pct            = _capped_mfe
            t.mfe_pct_raw        = _raw_mfe
            t.mfe_capped         = _was_capped
            t.mae_pct            = float(close_ev.get("mae_pct") or 0)
            t.realized_basis_usd = float(close_ev.get("realized_basis_usd") or 0)
            t.proceeds_usd       = float(close_ev.get("proceeds_usd") or 0.0)
            _sat = close_ev.get("sell_attempt_ts")
            t.sell_attempt_ts    = float(_sat) if _sat is not None else None
 
            _close_did = close_ev.get("decision_id") or ""
 
            # 2-3: prefer causal decision_id match; fall back to nearest-timestamp ±300s
            _sigs_by_id = [e for e in events if e.get("type") == "signal_accept" and _close_did and e.get("decision_id") == _close_did]
            sigs = _sigs_by_id or [e for e in events if e.get("type") == "signal_accept"
                    and abs(float(e.get("ts") or 0) - entry_ts) <= 300]
            if sigs:
                sig = sigs[0] if _sigs_by_id else min(sigs, key=lambda e: abs(float(e.get("ts") or 0) - entry_ts))
                t.signal_ts       = float(sig.get("ts") or 0)
                t.signal_price    = sig.get("price_usd")
                t.signal_source   = sig.get("source")
                t.candidate_age_ms = sig.get("candidate_age_ms")
                t.reference_price = sig.get("reference_price")
 
            _execs_by_id = [e for e in events if e.get("type") == "execution_attempt" and _close_did and e.get("decision_id") == _close_did]
            execs = _execs_by_id or [e for e in events if e.get("type") == "execution_attempt"
                     and abs(float(e.get("ts") or 0) - entry_ts) <= 300]
            if execs:
                ex = execs[0] if _execs_by_id else min(execs, key=lambda e: abs(float(e.get("ts") or 0) - entry_ts))
                t.exec_ts           = float(ex.get("ts") or 0)
                t.exec_ok           = ex.get("ok")
                t.exec_error        = ex.get("error")
                t.exec_latency_ms   = ex.get("execution_latency_ms") or ex.get("latency_ms")
                t.detect_to_exec_ms = ex.get("detect_to_exec_ms")
                t.ts_detected       = ex.get("ts_detected")
                t.exec_size_usd     = ex.get("size_usd")
                t.exec_source       = ex.get("source")
                t.decision_breakdown = ex.get("decision_breakdown_ms")
                # Prefer fill_price_usd (actual Jupiter fill) over reference_price_usd
                # (graduation curve constant for ~90% of pre-fix trades).
                _fill = ex.get("fill_price_usd")
                _ref  = ex.get("reference_price_usd")
                t.fill_price_usd = float(_fill) if _fill is not None and float(_fill) > 0 else None
                t.reference_price_is_real = ex.get("reference_price_is_real")
                if t.fill_price_usd is not None:
                    t.entry_price = t.fill_price_usd
                elif _ref is not None and float(_ref) > 0:
                    t.entry_price = float(_ref)
                else:
                    t.entry_price = None
 
            t.partials = [
                e for e in events
                if e.get("type") == "partial_realize"
                and entry_ts <= float(e.get("ts") or 0) <= close_ts + 2
            ]
 
            # V2: tag price_suspect. A trade is suspect when the entry price is
            # not backed by a real DEX observation. Three detection paths:
            #   1. fill_price_usd present → real Jupiter fill → never suspect.
            #   2. reference_price_is_real == False → execution used graduation
            #      curve estimate (catches any SOL price, not just $150).
            #   3. Absolute price match against known curve sentinels (legacy).
            if t.fill_price_usd is not None:
                t.price_suspect = False
            elif t.reference_price_is_real is not None and not t.reference_price_is_real:
                t.price_suspect = True
            else:
                t.price_suspect = _is_suspect_price(t.entry_price)
 
            trades.append(t)
 
    return trades
 
 
# ── Contamination filter (P4.6: SSOT in constants.py, shared with replay_analyzer) ─
from constants import is_contaminated as _is_contaminated
 
 
# ── Statistics ────────────────────────────────────────────────────────────────
def percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    idx = (len(s) - 1) * p / 100.0
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)
 
def pct_summary(values: List[float]) -> str:
    if not values:
        return "—"
    return (f"p25={percentile(values, 25):.1f} "
            f"p50={percentile(values, 50):.1f} "
            f"p75={percentile(values, 75):.1f} "
            f"p90={percentile(values, 90):.1f}")
 
def scenario_stats(results: List[Tuple[float, float]]) -> dict:
    if not results:
        return dict(n=0, wins=0, losses=0, win_rate=0.0, avg_roi=0.0,
                    avg_pnl=0.0, total_pnl=0.0, best_roi=0.0, worst_roi=0.0)
    rois = [r for r, _ in results]
    pnls = [p for _, p in results]
    # P4.2: canonical win = pnl > 0.
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return dict(
        n=len(results),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(results) * 100 if results else 0.0,
        avg_roi=sum(rois) / len(rois) if rois else 0.0,
        avg_pnl=sum(pnls) / len(pnls) if pnls else 0.0,
        total_pnl=sum(pnls),
        best_roi=max(rois) if rois else 0.0,
        worst_roi=min(rois) if rois else 0.0,
    )
 
 
# ── Counterfactuals ────────────────────────────────────────────────────────────
def cf_trailing_stop_from_mfe(trades: List[Trade], trail_pct: float) -> List[Tuple[float, float]]:
    results = []
    for t in trades:
        if t.roi_pct is None or t.mfe_pct is None:
            continue
        size = t.realized_basis_usd or t.exec_size_usd or 0.5
        mfe = t.mfe_pct / 100.0
        trail_exit_roi = ((1 + mfe) * (1 - trail_pct) - 1) * 100
        sim_roi = max(trail_exit_roi, t.roi_pct) if mfe > trail_pct else t.roi_pct
        sim_pnl = size * sim_roi / 100
        results.append((sim_roi, sim_pnl))
    return results
 
 
# ── Markdown helpers ───────────────────────────────────────────────────────────
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
    if v is None: return "—"
    return f"{v:.{decimals}f}{suffix}"
 
def fmt_pct(v: Optional[float]) -> str:
    return fmt_f(v, 1, "%")
 
def fmt_usd(v: Optional[float]) -> str:
    if v is None: return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:.4f}"
 
def ts_fmt(ts: Optional[float]) -> str:
    if not ts: return "—"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
 
 
# ── Report builder ─────────────────────────────────────────────────────────────
def build_report(history: List[dict]) -> str:
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cutoff_str = datetime.fromtimestamp(POST_FIX_CUTOFF_TS, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
 
    trades_all = reconstruct_trades(history)
    all_closed = [t for t in trades_all if t.roi_pct is not None]
 
    contaminated = [t for t in all_closed if _is_contaminated(t)]
    clean_all    = [t for t in all_closed if not _is_contaminated(t)]
 
    # Post-fix subset
    post_fix = [t for t in clean_all if (t.close_ts or 0) > POST_FIX_CUTOFF_TS]
    pre_fix  = [t for t in clean_all if (t.close_ts or 0) <= POST_FIX_CUTOFF_TS]
 
    # price_suspect split within post-fix
    post_suspect = [t for t in post_fix if t.price_suspect]
    post_clean   = [t for t in post_fix if not t.price_suspect]
 
    # Trades with nonzero MFE — exclude outliers above 500% cap (sentinel price artifacts)
    # and trades where entry_price is None (no reliable fill price to anchor the analysis)
    def _mfe_credible(t: Trade) -> bool:
        return (t.mfe_pct is not None
                and t.mfe_pct != 0
                and t.mfe_pct <= 500.0
                and t.entry_price is not None)

    post_mfe_all     = [t for t in post_fix   if _mfe_credible(t)]
    post_mfe_clean   = [t for t in post_clean  if _mfe_credible(t)]
 
    lines: List[str] = [
        "# Sniperbot Replay Analysis Report V2",
        f"_Generated: {now_str}_",
        f"_Post-fix cutoff: **{cutoff_str}** (rapid-dump guard + sell-retry-6001)_",
        "",
        "## Summary",
        "",
        f"| Metric | Count |",
        f"| ------ | ----- |",
        f"| Total history records | {len(history)} |",
        f"| Total reconstructed closes | {len(all_closed)} |",
        f"| Contaminated (excluded) | {len(contaminated)} |",
        f"| Clean closes (all time) | {len(clean_all)} |",
        f"| Pre-fix closes (before cutoff) | {len(pre_fix)} |",
        f"| **Post-fix closes (after cutoff)** | **{len(post_fix)}** |",
        f"| Post-fix: price_suspect | {len(post_suspect)} |",
        f"| Post-fix: real entry price | {len(post_clean)} |",
        "",
    ]
 
    # ── No data notice ─────────────────────────────────────────────────────────
    if len(history) == 0:
        lines += [
            "> **No data available.** `history.json` was not found on disk.",
            "> The bot must be run (and close at least one position) to generate history data.",
            "> This report will auto-populate with real data once the bot has run post-fix.",
            "",
            "---",
            "",
            "## Analysis Pending",
            "",
            "The following analyses are defined and will execute automatically once",
            "`history.json` exists with post-fix trade records:",
            "",
            "1. **Win rate** — clean post-fix trades only (no price_suspect contamination)",
            "2. **MFE capture rate** — how often the exit captures the peak unrealized gain",
            "3. **Trail stop counterfactual** — `-6%` vs `-8%` from MFE peak",
            "4. **Hold time distribution** — p25/p50/p75/p90",
            "5. **price_suspect comparison** — whether graduation-curve entry prices",
            "   distort win rate vs trades with real fill prices",
            "",
            f"Cutoff timestamp: `{POST_FIX_CUTOFF_TS}` ({cutoff_str})",
            "",
            f"Suspect price thresholds: `{SUSPECT_PRICES[0]:.10g}` (Raydium graduation curve",
            f"@ SOL≈$150), `{SUSPECT_PRICES[1]:.10g}` (legacy reference constant)",
            "",
        ]
        return "\n".join(lines)
 
    if len(post_fix) == 0:
        lines += [
            f"> **No post-fix trades found.** {len(clean_all)} clean trades exist but all predate",
            f"> the cutoff ({cutoff_str}).",
            "> Sections below report on all-time clean trades for reference.",
            "",
        ]
 
    # ── Section 1: Contamination breakdown ─────────────────────────────────────
    lines += ["## 1. Contamination Filter", ""]
    contam_reasons = Counter(t.close_reason for t in contaminated)
    if contam_reasons:
        rows = [[r or "unknown", str(c)] for r, c in contam_reasons.most_common()]
        lines += [
            md_table(["Close Reason", "Excluded Count"], rows),
            "",
        ]
    else:
        lines += ["_No contaminated trades._", ""]
 
    # ── Section 2: Post-fix win rate ───────────────────────────────────────────
    target = post_fix if post_fix else clean_all
    target_label = "Post-fix" if post_fix else "All-time (no post-fix data)"
 
    lines += [f"## 2. Win Rate — {target_label} Trades", ""]
 
    def win_rate_block(subset: List[Trade], label: str) -> List[str]:
        if not subset:
            return [f"_No {label} trades._", ""]
        # P4.2: canonical win = pnl_usd > 0.
        wins   = [t for t in subset if (t.pnl_usd or 0) > 0]
        losses = [t for t in subset if (t.pnl_usd or 0) <= 0]
        total_pnl = sum(t.pnl_usd or 0 for t in subset)
        avg_roi   = sum(t.roi_pct for t in subset) / len(subset)
        return [
            f"**{label}** — {len(subset)} trades",
            "",
            f"| Metric | Value |",
            f"| ------ | ----- |",
            f"| Wins / Losses | {len(wins)} / {len(losses)} |",
            f"| Win rate | {len(wins)/len(subset)*100:.1f}% |",
            f"| Total realized P&L | {fmt_usd(total_pnl)} |",
            f"| Average ROI | {fmt_pct(avg_roi)} |",
            f"| Best ROI | {fmt_pct(max(t.roi_pct for t in subset))} |",
            f"| Worst ROI | {fmt_pct(min(t.roi_pct for t in subset))} |",
            "",
        ]
 
    lines += win_rate_block(target, target_label)
 
    if post_fix:
        lines += ["### By price_suspect flag", ""]
        lines += win_rate_block(post_clean,   "Real entry price (post-fix)")
        lines += win_rate_block(post_suspect, "price_suspect (post-fix)")
 
        if post_suspect:
            lines += [
                "> **price_suspect trades** have entry_price ≈ graduation curve constant "
                f"(`{SUSPECT_PRICES[0]:.3g}`) or legacy reference `{SUSPECT_PRICES[1]:.3g}`. "
                "Their ROI figures are unreliable because the entry price is a reference estimate, "
                "not an actual fill price.",
                "",
            ]
 
    # ── Section 3: Close reason distribution ──────────────────────────────────
    lines += [f"## 3. Close Reason Distribution ({target_label})", ""]
 
    reason_rows = []
    for reason, cnt in Counter(t.close_reason for t in target).most_common():
        sub = [t for t in target if t.close_reason == reason]
        avg_roi = sum(t.roi_pct for t in sub) / len(sub) if sub else 0.0
        total_p = sum(t.pnl_usd or 0 for t in sub)
        reason_rows.append([reason or "unknown", str(cnt),
                             f"{cnt/len(target)*100:.1f}%",
                             fmt_pct(avg_roi), fmt_usd(total_p)])
 
    if reason_rows:
        lines += [
            md_table(["Close Reason", "Count", "% of Closes", "Avg ROI", "Total P&L"], reason_rows),
            "",
        ]
 
    # ── Section 4: Hold time distribution ─────────────────────────────────────
    lines += [f"## 4. Hold Time Distribution ({target_label})", ""]
 
    hold_times = [t.hold_time_s for t in target if t.hold_time_s is not None and t.hold_time_s > 0]
    if hold_times:
        ht = sorted(hold_times)
        lines += [
            md_table(
                ["Percentile", "Hold Time"],
                [
                    ["p25", f"{percentile(ht, 25):.0f}s ({percentile(ht, 25)/60:.1f} min)"],
                    ["p50 (median)", f"{percentile(ht, 50):.0f}s ({percentile(ht, 50)/60:.1f} min)"],
                    ["p75", f"{percentile(ht, 75):.0f}s ({percentile(ht, 75)/60:.1f} min)"],
                    ["p90", f"{percentile(ht, 90):.0f}s ({percentile(ht, 90)/60:.1f} min)"],
                    ["max", f"{max(ht):.0f}s ({max(ht)/60:.1f} min)"],
                ],
            ),
            "",
            f"- **Mean hold time:** {sum(ht)/len(ht):.0f}s ({sum(ht)/len(ht)/60:.1f} min)",
            f"- **< 60s:** {sum(1 for h in ht if h < 60)} / {len(ht)} ({sum(1 for h in ht if h < 60)/len(ht)*100:.1f}%)",
            f"- **< 5 min:** {sum(1 for h in ht if h < 300)} / {len(ht)} ({sum(1 for h in ht if h < 300)/len(ht)*100:.1f}%)",
            "",
        ]
    else:
        lines += ["_No hold time data._", ""]
 
    # ── Section 5: MFE capture rate ────────────────────────────────────────────
    lines += [f"## 5. MFE Capture Rate ({target_label})", ""]
 
    mfe_set = post_mfe_all if post_fix else [t for t in clean_all if t.mfe_pct is not None and t.mfe_pct != 0]
    mfe_label = target_label
 
    if mfe_set:
        mfe_values = [t.mfe_pct for t in mfe_set]
        roi_values = [t.roi_pct for t in mfe_set]
        # Capture rate: actual_exit_roi / mfe (how much of the peak we kept)
        capture_rates = []
        for t in mfe_set:
            if t.mfe_pct and t.mfe_pct > 0:
                cap = t.roi_pct / t.mfe_pct * 100  # % of MFE captured
                capture_rates.append(cap)
 
        _pos_capture = sum(1 for c in capture_rates if c > 0)
        _neg_capture = sum(1 for c in capture_rates if c <= 0)
        _median_cap = sorted(capture_rates)[len(capture_rates) // 2] if capture_rates else 0.0
        _mean_cap = sum(capture_rates) / len(capture_rates) if capture_rates else 0.0

        lines += [
            f"_Trades with nonzero MFE: {len(mfe_set)} / {len(target)}_",
            "",
            md_table(
                ["Metric", "Value"],
                [
                    ["Avg MFE", fmt_pct(sum(mfe_values)/len(mfe_values))],
                    ["Avg exit ROI (same trades)", fmt_pct(sum(roi_values)/len(roi_values))],
                    ["Median MFE capture", f"{_median_cap:.1f}%" if capture_rates else "—"],
                    ["Mean MFE capture (skewed by rugs)", f"{_mean_cap:.1f}%" if capture_rates else "—"],
                    ["Positive capture (kept some MFE)", str(_pos_capture)],
                    ["Negative capture (gave back all + more)", str(_neg_capture)],
                    ["Trades where exit > 0 (despite MFE > 0)", str(sum(1 for t in mfe_set if t.roi_pct > 0))],
                    ["Trades where exit < 0 (gave back all gains)", str(sum(1 for t in mfe_set if t.roi_pct <= 0))],
                ],
            ),
            "",
        ]
 
        if capture_rates:
            lines += [
                f"- **MFE capture p25/p50/p75:** {pct_summary(capture_rates)}",
                "",
            ]
    else:
        lines += [f"_No MFE data for {mfe_label} trades._", ""]
 
    # ── Section 6: Trail stop counterfactual ──────────────────────────────────
    lines += [f"## 6. Trail Stop Counterfactual — `-6%` vs `-8%` from MFE Peak", ""]
    lines += [f"_Using {mfe_label} trades with nonzero MFE ({len(mfe_set)} trades)_", ""]
 
    if mfe_set:
        cf6  = cf_trailing_stop_from_mfe(mfe_set, 0.06)
        cf8  = cf_trailing_stop_from_mfe(mfe_set, 0.08)
        s6   = scenario_stats(cf6)
        s8   = scenario_stats(cf8)
 
        # Actual stats on same mfe_set
        actual_results = [(t.roi_pct, t.pnl_usd or (t.realized_basis_usd or t.exec_size_usd or 0.5) * t.roi_pct / 100)
                         for t in mfe_set if t.roi_pct is not None]
        s_actual = scenario_stats(actual_results)
 
        lines += [
            md_table(
                ["Scenario", "N", "Wins", "Win Rate", "Avg ROI", "Total P&L"],
                [
                    ["Actual (as-traded)",
                     str(s_actual["n"]), str(s_actual["wins"]),
                     f"{s_actual['win_rate']:.1f}%",
                     fmt_pct(s_actual["avg_roi"]),
                     fmt_usd(s_actual["total_pnl"])],
                    ["Trail −6% from MFE",
                     str(s6["n"]), str(s6["wins"]),
                     f"{s6['win_rate']:.1f}%",
                     fmt_pct(s6["avg_roi"]),
                     fmt_usd(s6["total_pnl"])],
                    ["Trail −8% from MFE",
                     str(s8["n"]), str(s8["wins"]),
                     f"{s8['win_rate']:.1f}%",
                     fmt_pct(s8["avg_roi"]),
                     fmt_usd(s8["total_pnl"])],
                ],
            ),
            "",
        ]
 
        delta = s6["total_pnl"] - s8["total_pnl"]
        lines += [
            f"- **−6% captures {fmt_usd(delta)} more** than −8% "
            f"({'tighter stop wins' if delta > 0 else 'looser stop wins'})" if abs(delta) > 0.0001 else
            "- −6% and −8% produce identical P&L on this dataset",
            "",
        ]
    else:
        lines += ["_Insufficient MFE data for trail stop counterfactual._", ""]
 
    # ── Section 7: price_suspect analysis (post-fix only) ─────────────────────
    if post_fix and (post_suspect or post_clean):
        lines += ["## 7. price_suspect vs Real Entry Price Comparison", ""]
 
        def mini_stats(subset: List[Trade], label: str) -> List[str]:
            if not subset:
                return [f"_No {label} trades._", ""]
            wins = [t for t in subset if t.roi_pct > 0]
            hold_s = [t.hold_time_s for t in subset if t.hold_time_s]
            mfes   = [t.mfe_pct for t in subset if _mfe_credible(t)]
            return [
                f"**{label}** — {len(subset)} trades",
                "",
                md_table(
                    ["Metric", "Value"],
                    [
                        ["Win rate", f"{len(wins)/len(subset)*100:.1f}%"],
                        ["Avg ROI", fmt_pct(sum(t.roi_pct for t in subset)/len(subset))],
                        ["Total P&L", fmt_usd(sum(t.pnl_usd or 0 for t in subset))],
                        ["Avg MFE (nonzero)", fmt_pct(sum(mfes)/len(mfes) if mfes else None)],
                        ["Avg hold time", fmt_f(sum(hold_s)/len(hold_s) if hold_s else None, 0, "s")],
                        ["Entry prices", ", ".join(f"{t.entry_price:.4g}" if t.entry_price is not None else "None" for t in subset[:5]) + ("…" if len(subset) > 5 else "")],
                    ],
                ),
                "",
            ]
 
        lines += mini_stats(post_clean,   "Real entry price trades")
        lines += mini_stats(post_suspect, "price_suspect trades")
 
        if post_suspect and post_clean:
            w_clean   = sum(1 for t in post_clean   if t.roi_pct > 0) / len(post_clean)   * 100
            w_suspect = sum(1 for t in post_suspect if t.roi_pct > 0) / len(post_suspect) * 100
            delta_wr  = w_clean - w_suspect
            lines += [
                f"> price_suspect trades show **{abs(delta_wr):.1f}pp {'higher' if w_suspect > w_clean else 'lower'} win rate** "
                f"({w_suspect:.1f}% vs {w_clean:.1f}% real-price). "
                f"{'This is likely artificial — graduation curve entry price inflates ROI calculations.' if w_suspect > w_clean + 5 else 'Difference is small; price contamination may not be material for this batch.'}",
                "",
            ]
 
    # ── Section 8: data provenance ─────────────────────────────────────────────
    lines += ["## 8. Data Provenance", ""]
 
    ts_list = [t.close_ts for t in clean_all if t.close_ts]
    if ts_list:
        lines += [
            f"- **History file:** `{HISTORY_FILE}`",
            f"- **Earliest close:** {ts_fmt(min(ts_list))}",
            f"- **Latest close:** {ts_fmt(max(ts_list))}",
            f"- **Post-fix cutoff:** {cutoff_str}",
            "",
            "### Suspect price constants flagged",
            "",
            md_table(
                ["Constant", "Value", "Meaning"],
                [
                    ["graduation_curve", f"{SUSPECT_PRICES[0]:.10g}",
                     "Raydium init price at SOL≈$150 (pump.fun graduation)"],
                    ["legacy_reference", f"{SUSPECT_PRICES[1]:.10g}",
                     "Legacy reference price constant (pre-fix)"],
                ],
            ),
            "",
        ]
    else:
        lines += [f"- **History file:** `{HISTORY_FILE}` (not found or empty)", ""]
 
    return "\n".join(lines)
 
 
# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Replay analysis V2")
    parser.add_argument("--no-write", action="store_true",
                        help="Print to stdout instead of writing REPLAY_REPORT_V2.md")
    args = parser.parse_args()
 
    history = load_history()
    report  = build_report(history)
 
    if args.no_write:
        print(report)
    else:
        REPORT_FILE.write_text(report)
        print(f"[OK] Wrote {REPORT_FILE}")
        print(f"     History records: {len(history)}")
        total_closes = sum(1 for ev in history if ev.get("type") == "trade_close")
        print(f"     Trade close events: {total_closes}")
 
 
if __name__ == "__main__":
    main()

