#!/usr/bin/env python3
"""Extract one row per completed trade from history.json → data/trades.csv.

Join rule: for each `trade_close`, find the matching `execution_attempt`
(ok=True, same mint, |ts-entry_ts| <= 60s), and the most recent
`signal_accept` <= entry_ts+5s. Missing fields are written as empty.
Stdlib only.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "history.json"
OUT = ROOT / "data" / "trades.csv"

COLUMNS = [
    "mint", "entry_ts", "close_ts", "hold_time_s",
    "pnl_usd", "roi_pct", "mfe_pct", "mae_pct", "close_reason",
    "detect_to_exec_ms", "execution_latency_ms",
    "entry_price", "exit_price",
    "realized_basis_usd", "proceeds_usd",
    "candidate_age_ms", "opportunity_age_ms",
    "size_usd", "entry_score", "holder_pct_top5", "delay_s",
    "entry_jito_tip_lamports",
    "executable_mark_observations",
    "late_entry", "mark_price_source", "entry_price_source",
    "decision_breakdown_price_meta_rug_ms",
    "decision_breakdown_rpc_wait_ms",
    "decision_breakdown_rugcheck_primary_ms",
    "source",
]


def _as_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def main():
    events = json.loads(HISTORY.read_text())

    exec_by_mint = defaultdict(list)
    exec_any_by_mint = defaultdict(list)
    signal_by_mint = defaultdict(list)
    for e in events:
        t = e.get("type")
        if t == "execution_attempt":
            exec_any_by_mint[e.get("mint")].append((float(e.get("ts") or 0), e))
            if e.get("ok"):
                exec_by_mint[e.get("mint")].append((float(e.get("ts") or 0), e))
        elif t == "signal_accept":
            signal_by_mint[e.get("mint")].append((float(e.get("ts") or 0), e))

    rows = []
    for e in events:
        if e.get("type") != "trade_close":
            continue
        mint = e.get("mint")
        entry_ts = _as_float(e.get("entry_ts")) or 0.0

        # Nearest execution_attempt within 60s of entry_ts
        ex = {}
        best_dt = None
        for ts, ev in exec_by_mint.get(mint, []):
            dt = abs(ts - entry_ts)
            if dt <= 60 and (best_dt is None or dt < best_dt):
                best_dt, ex = dt, ev

        # Most recent signal_accept <= entry_ts + 5s
        sig = {}
        best_ts = None
        for ts, ev in signal_by_mint.get(mint, []):
            if ts <= entry_ts + 5 and (best_ts is None or ts > best_ts):
                best_ts, sig = ts, ev

        # Nearest execution_attempt of ANY status (for holder_pct_top5,
        # which only attaches to the first attempt for a mint).
        ex_any = {}
        best_dt_any = None
        for ts, ev in exec_any_by_mint.get(mint, []):
            dt = abs(ts - entry_ts)
            if dt <= 120 and (best_dt_any is None or dt < best_dt_any):
                best_dt_any, ex_any = dt, ev

        breakdown = ex.get("decision_breakdown_ms") or {}
        entry_price = (_as_float(ex.get("fill_price_usd"))
                       or _as_float(ex.get("reference_price_usd"))
                       or _as_float(sig.get("price_usd")))

        # entry_score is emitted as a dict {total, time_to_graduate_pts, ...}
        # on signal_accept. Unpack its total; also prefer its embedded holder_pct_top5.
        _score_dict = sig.get("entry_score") if isinstance(sig.get("entry_score"), dict) else {}
        _score_total = _as_float(_score_dict.get("total"))
        _top5 = _as_float(
            ex.get("holder_pct_top5")
            or ex_any.get("holder_pct_top5")
            or sig.get("holder_pct_top5")
            or _score_dict.get("holder_pct_top5")
        )

        rows.append({
            "mint": mint,
            "entry_ts": entry_ts,
            "close_ts": _as_float(e.get("ts")),
            "hold_time_s": _as_float(e.get("hold_time_seconds")),
            "pnl_usd": _as_float(e.get("pnl_usd")),
            "roi_pct": _as_float(e.get("roi_pct")),
            "mfe_pct": _as_float(e.get("mfe_pct")),
            "mae_pct": _as_float(e.get("mae_pct")),
            "close_reason": e.get("close_reason"),
            "detect_to_exec_ms": _as_float(ex.get("detect_to_exec_ms")),
            "execution_latency_ms": _as_float(ex.get("execution_latency_ms")),
            "entry_price": entry_price,
            "exit_price": _as_float(e.get("exit_price")),
            "realized_basis_usd": _as_float(e.get("realized_basis_usd")),
            "proceeds_usd": _as_float(e.get("proceeds_usd")),
            "candidate_age_ms": _as_float(ex.get("candidate_age_ms")
                                          or sig.get("candidate_age_ms")
                                          or e.get("candidate_age_ms")),
            "opportunity_age_ms": _as_float(e.get("opportunity_age_ms")
                                            or ex.get("opportunity_age_ms")),
            "size_usd": _as_float(ex.get("size_usd")),
            "entry_score": _score_total,
            "holder_pct_top5": _top5,
            "delay_s": _as_float(sig.get("delay_s")),
            "entry_jito_tip_lamports": _as_float(e.get("entry_jito_tip_lamports")),
            "executable_mark_observations": _as_float(e.get("executable_mark_observations")),
            "late_entry": e.get("late_entry") if e.get("late_entry") is not None else ex.get("late_entry"),
            "mark_price_source": e.get("mark_price_source"),
            "entry_price_source": e.get("entry_price_source") or sig.get("entry_price_source") or ex.get("entry_price_source"),
            "decision_breakdown_price_meta_rug_ms": _as_float(breakdown.get("price_meta_rug_ms")),
            "decision_breakdown_rpc_wait_ms": _as_float(breakdown.get("rpc_wait_ms")),
            "decision_breakdown_rugcheck_primary_ms": _as_float(breakdown.get("rugcheck_primary_ms")),
            "source": ex.get("source") or sig.get("source"),
        })

    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in COLUMNS})

    # Quick coverage summary
    def nonnull(key):
        return sum(1 for r in rows if r.get(key) not in (None, "", 0))
    print(f"[extract_trades] wrote {len(rows)} trades → {OUT}")
    for k in ["entry_price", "detect_to_exec_ms", "entry_score",
              "holder_pct_top5", "delay_s", "executable_mark_observations",
              "opportunity_age_ms", "late_entry", "entry_price_source",
              "candidate_age_ms"]:
        print(f"  {k:<35} nonnull={nonnull(k)}/{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
