#!/usr/bin/env python3
"""
latency_audit.py — Sniperbot forensic latency & exit-regime analysis.
 
Usage:
    python3 analysis/latency_audit.py history.json [runtime_state.json]
 
Outputs:
    1. Candidate-age bucket report  (0-2s, 2-5s, 5-10s, 10-15s, 15s+)
    2. Close-reason report with age stats
    3. Top-20 stale trades table
    4. Reserve-miss correlation (RESERVE_ALL_OWNERS_MISS events)
    5. Conclusion block: latency_primary / reserve_discovery_primary / exit_regime_primary
"""
 
import json
import sys
import math
from collections import defaultdict
from typing import Optional
 
 
# ─── helpers ───────────────────────────────────────────────────────────────────
 
def _safe(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default
 
 
def _median(lst):
    if not lst:
        return None
    s = sorted(lst)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
 
 
def _pct(num, denom):
    return (num / denom * 100) if denom else 0.0
 
 
def _fmt_ms(ms):
    if ms is None:
        return "  N/A"
    return f"{ms:8,.0f}ms"
 
 
def _age_bucket(ms):
    if ms is None:
        return "unknown"
    s = ms / 1000.0
    if s < 2:
        return "0-2s"
    if s < 5:
        return "2-5s"
    if s < 10:
        return "5-10s"
    if s < 15:
        return "10-15s"
    return "15s+"
 
 
BUCKET_ORDER = ["0-2s", "2-5s", "5-10s", "10-15s", "15s+", "unknown"]
 
 
# ─── load ──────────────────────────────────────────────────────────────────────
 
def load_history(path: str):
    with open(path) as f:
        raw = f.read().strip()
    # Support both JSON array and JSONL
    if raw.startswith("["):
        return json.loads(raw)
    else:
        return [json.loads(line) for line in raw.splitlines() if line.strip()]
 
 
def load_runtime_state(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Could not load runtime_state: {e}")
        return {}
 
 
# ─── join signal_accept + trade_close ──────────────────────────────────────────
 
def build_trade_records(events):
    """
    Join signal_accept → trade_close by mint.
    Returns list of dicts with all fields needed for analysis.
    """
    # Latest signal_accept per mint (in case of retries / re-opens)
    signals: dict = {}
    closes: list = []
    reserve_miss_mints: set = set()
 
    for ev in events:
        etype = ev.get("event_type") or ev.get("type") or ""
        mint = ev.get("mint") or ev.get("token") or ""
 
        if etype == "signal_accept":
            signals[mint] = ev
 
        elif etype == "trade_close":
            closes.append(ev)
 
        elif "RESERVE_ALL_OWNERS_MISS" in str(ev):
            reserve_miss_mints.add(mint)
 
        # Also scan raw log strings for reserve miss pattern
        raw = json.dumps(ev)
        if "RESERVE_ALL_OWNERS_MISS" in raw and mint:
            reserve_miss_mints.add(mint)
 
    records = []
    for close_ev in closes:
        mint = close_ev.get("mint") or close_ev.get("token") or ""
        sig = signals.get(mint, {})
 
        # candidate_age_ms: prefer explicit field, fall back to computing from ts
        age_ms = _safe(close_ev.get("candidate_age_ms") or sig.get("candidate_age_ms"), None)
 
        # Try computing from timestamps if not stored directly
        if age_ms is None or age_ms == 0.0:
            sig_ts = _safe(sig.get("ts") or sig.get("timestamp"), None)
            detect_ts = _safe(sig.get("ts_detected") or sig.get("detected_at"), None)
            entry_ts = _safe(close_ev.get("entry_ts"), None)
            if detect_ts and entry_ts and entry_ts > detect_ts:
                age_ms = (entry_ts - detect_ts) * 1000
            elif sig_ts and entry_ts and entry_ts > sig_ts:
                age_ms = (entry_ts - sig_ts) * 1000
 
        # PnL / ROI fields
        realized_pnl = _safe(close_ev.get("realized_pnl") or close_ev.get("pnl"))
        realized_roi = _safe(close_ev.get("realized_roi_pct") or close_ev.get("roi_pct"))
        mfe = _safe(close_ev.get("mfe_pct"))
        mae = _safe(close_ev.get("mae_pct"))
 
        # Close reason
        close_reason = (
            close_ev.get("close_reason")
            or close_ev.get("reason")
            or close_ev.get("exit_reason")
            or "UNKNOWN"
        )
 
        # Win = positive realized pnl
        is_win = realized_pnl > 0
 
        # Durability class
        durability = (
            close_ev.get("durability_class")
            or close_ev.get("mark_durability_class")
            or sig.get("durability_class")
            or "unknown"
        )
 
        # Entry price source
        entry_price_source = (
            close_ev.get("entry_price_source")
            or sig.get("entry_price_source")
            or "unknown"
        )
 
        records.append({
            "mint": mint,
            "age_ms": age_ms,
            "bucket": _age_bucket(age_ms),
            "realized_pnl": realized_pnl,
            "realized_roi": realized_roi,
            "mfe": mfe,
            "mae": mae,
            "close_reason": close_reason,
            "is_win": is_win,
            "durability_class": durability,
            "entry_price_source": entry_price_source,
            "had_reserve_miss": mint in reserve_miss_mints,
            "ts": _safe(close_ev.get("ts") or close_ev.get("timestamp")),
            "raw_close": close_ev,
            "raw_signal": sig,
        })
 
    return records, reserve_miss_mints
 
 
# ─── report 1: age buckets ──────────────────────────────────────────────────────
 
def report_age_buckets(records):
    by_bucket = defaultdict(list)
    for r in records:
        by_bucket[r["bucket"]].append(r)
 
    print("=" * 72)
    print("REPORT 1 — CANDIDATE AGE BUCKET ANALYSIS")
    print("=" * 72)
    hdr = f"{'Bucket':<10} {'N':>4} {'Wins':>5} {'WinR%':>7} {'AvgPnL':>9} {'MdnPnL':>9} {'AvgROI%':>8} {'AvgMFE%':>8} {'AvgMAE%':>8}  DominantReason"
    print(hdr)
    print("-" * 110)
 
    for bucket in BUCKET_ORDER:
        recs = by_bucket.get(bucket, [])
        if not recs:
            continue
        n = len(recs)
        wins = sum(1 for r in recs if r["is_win"])
        pnls = [r["realized_pnl"] for r in recs]
        rois = [r["realized_roi"] for r in recs]
        mfes = [r["mfe"] for r in recs]
        maes = [r["mae"] for r in recs]
 
        avg_pnl = sum(pnls) / n
        mdn_pnl = _median(pnls)
        avg_roi = sum(rois) / n
        avg_mfe = sum(mfes) / n
        avg_mae = sum(maes) / n
        win_rate = _pct(wins, n)
 
        # Dominant close reason
        reason_counts = defaultdict(int)
        for r in recs:
            reason_counts[r["close_reason"]] += 1
        dom_reason = max(reason_counts, key=reason_counts.get)
        dom_count = reason_counts[dom_reason]
 
        print(
            f"{bucket:<10} {n:>4} {wins:>5} {win_rate:>6.1f}% "
            f"{avg_pnl:>+9.4f} {mdn_pnl:>+9.4f} "
            f"{avg_roi:>+7.1f}% {avg_mfe:>+7.1f}% {avg_mae:>+7.1f}%  "
            f"{dom_reason} ({dom_count}/{n})"
        )
 
    print()
 
    # Age stats for wins vs losses
    win_ages = [r["age_ms"] for r in records if r["is_win"] and r["age_ms"] is not None]
    loss_ages = [r["age_ms"] for r in records if not r["is_win"] and r["age_ms"] is not None]
 
    print(f"  Wins  — n={len(win_ages):3d}  median={_fmt_ms(_median(win_ages))}  "
          f"avg={_fmt_ms(sum(win_ages)/len(win_ages) if win_ages else None)}")
    print(f"  Losses— n={len(loss_ages):3d}  median={_fmt_ms(_median(loss_ages))}  "
          f"avg={_fmt_ms(sum(loss_ages)/len(loss_ages) if loss_ages else None)}")
 
    if win_ages and loss_ages:
        delta = _median(loss_ages) - _median(win_ages)
        print(f"  Δ median (loss - win): {delta:+,.0f}ms  ({delta/1000:+.2f}s)")
    print()
 
 
# ─── report 2: close reason ────────────────────────────────────────────────────
 
def report_close_reasons(records):
    by_reason = defaultdict(list)
    for r in records:
        by_reason[r["close_reason"]].append(r)
 
    print("=" * 72)
    print("REPORT 2 — CLOSE REASON BREAKDOWN")
    print("=" * 72)
    hdr = f"{'Reason':<40} {'N':>4} {'Wins':>5} {'WinR%':>7} {'AvgAge':>10} {'MdnAge':>10} {'AvgPnL':>9} {'AvgMFE%':>8}"
    print(hdr)
    print("-" * 105)
 
    sorted_reasons = sorted(by_reason.items(), key=lambda x: -len(x[1]))
    for reason, recs in sorted_reasons:
        n = len(recs)
        wins = sum(1 for r in recs if r["is_win"])
        ages = [r["age_ms"] for r in recs if r["age_ms"] is not None]
        pnls = [r["realized_pnl"] for r in recs]
        mfes = [r["mfe"] for r in recs]
 
        avg_age = sum(ages) / len(ages) if ages else None
        mdn_age = _median(ages)
        avg_pnl = sum(pnls) / n
        avg_mfe = sum(mfes) / n
 
        print(
            f"{reason:<40} {n:>4} {wins:>5} {_pct(wins,n):>6.1f}%  "
            f"{_fmt_ms(avg_age)} {_fmt_ms(mdn_age)} "
            f"{avg_pnl:>+9.4f} {avg_mfe:>+7.1f}%"
        )
    print()
 
 
# ─── report 3: top-20 stale trades ─────────────────────────────────────────────
 
def report_stale_trades(records, top_n=20):
    print("=" * 72)
    print(f"REPORT 3 — TOP {top_n} STALE TRADES (by candidate_age_ms)")
    print("=" * 72)
    aged = [r for r in records if r["age_ms"] is not None]
    aged.sort(key=lambda r: -r["age_ms"])
 
    hdr = f"{'#':>3}  {'Mint':<46} {'Age':>10} {'ROI%':>7} {'PnL':>9} {'MFE%':>7} {'MAE%':>7}  {'CloseReason'}"
    print(hdr)
    print("-" * 130)
 
    for i, r in enumerate(aged[:top_n], 1):
        mint_short = r["mint"][:44] + ".." if len(r["mint"]) > 44 else r["mint"]
        print(
            f"{i:>3}.  {mint_short:<46} {_fmt_ms(r['age_ms'])} "
            f"{r['realized_roi']:>+6.1f}% {r['realized_pnl']:>+9.4f} "
            f"{r['mfe']:>+6.1f}% {r['mae']:>+6.1f}%  {r['close_reason']}"
        )
    print()
 
 
# ─── report 4: reserve-miss correlation ────────────────────────────────────────
 
def report_reserve_miss(records, reserve_miss_mints):
    print("=" * 72)
    print("REPORT 4 — RESERVE MISS CORRELATION")
    print("=" * 72)
 
    miss_recs = [r for r in records if r["had_reserve_miss"]]
    hit_recs = [r for r in records if not r["had_reserve_miss"]]
 
    def summary(recs, label):
        n = len(recs)
        if n == 0:
            print(f"  {label}: n=0  (no data)")
            return
        wins = sum(1 for r in recs if r["is_win"])
        pnls = [r["realized_pnl"] for r in recs]
        mfes = [r["mfe"] for r in recs]
        ages = [r["age_ms"] for r in recs if r["age_ms"] is not None]
        print(
            f"  {label:<35} n={n:3d}  win%={_pct(wins,n):5.1f}%  "
            f"avgPnL={sum(pnls)/n:+.4f}  avgMFE={sum(mfes)/n:+.1f}%  "
            f"medAge={_fmt_ms(_median(ages))}"
        )
 
        # Break by close reason
        reason_counts = defaultdict(int)
        for r in recs:
            reason_counts[r["close_reason"]] += 1
        top3 = sorted(reason_counts.items(), key=lambda x: -x[1])[:3]
        print(f"    Top reasons: " + ", ".join(f"{r}({c})" for r, c in top3))
 
    # Entry price source breakdown for reserve-miss trades
    source_counts = defaultdict(int)
    for r in miss_recs:
        source_counts[r["entry_price_source"]] += 1
 
    print(f"  Total unique mints with RESERVE_ALL_OWNERS_MISS: {len(reserve_miss_mints)}")
    print(f"  Trades matched to reserve-miss mints: {len(miss_recs)}")
    print()
    summary(miss_recs, "With reserve miss")
    summary(hit_recs,  "Without reserve miss")
 
    if source_counts:
        print(f"\n  Entry price sources for reserve-miss trades:")
        for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1]):
            print(f"    {src:<35} {cnt}")
    print()
 
 
# ─── report 5: durability class breakdown ──────────────────────────────────────
 
def report_durability(records):
    print("=" * 72)
    print("REPORT 5 — DURABILITY CLASS BREAKDOWN")
    print("=" * 72)
    by_dur = defaultdict(list)
    for r in records:
        by_dur[r["durability_class"]].append(r)
 
    hdr = f"{'DurabilityClass':<40} {'N':>4} {'Wins':>5} {'WinR%':>7} {'AvgPnL':>9} {'AvgMFE%':>8} {'MedAge':>10}"
    print(hdr)
    print("-" * 95)
 
    for dur, recs in sorted(by_dur.items(), key=lambda x: -len(x[1])):
        n = len(recs)
        wins = sum(1 for r in recs if r["is_win"])
        pnls = [r["realized_pnl"] for r in recs]
        mfes = [r["mfe"] for r in recs]
        ages = [r["age_ms"] for r in recs if r["age_ms"] is not None]
        print(
            f"{dur:<40} {n:>4} {wins:>5} {_pct(wins,n):>6.1f}% "
            f"{sum(pnls)/n:>+9.4f} {sum(mfes)/n:>+7.1f}%  {_fmt_ms(_median(ages))}"
        )
    print()
 
 
# ─── conclusion block ──────────────────────────────────────────────────────────
 
def report_conclusion(records, reserve_miss_mints):
    print("=" * 72)
    print("CONCLUSION — PRIMARY FAILURE MODE CLASSIFICATION")
    print("=" * 72)
 
    total = len(records)
    if total == 0:
        print("  No trade records found.")
        return
 
    wins = sum(1 for r in records if r["is_win"])
    losses = total - wins
    win_rate = _pct(wins, total)
 
    # --- Latency signal ---
    win_ages = [r["age_ms"] for r in records if r["is_win"] and r["age_ms"] is not None]
    loss_ages = [r["age_ms"] for r in records if not r["is_win"] and r["age_ms"] is not None]
    age_delta_ms = 0.0
    if win_ages and loss_ages:
        age_delta_ms = _median(loss_ages) - _median(win_ages)
 
    stale_losses = sum(1 for r in records if not r["is_win"] and r["age_ms"] and r["age_ms"] > 15_000)
    stale_loss_pct = _pct(stale_losses, losses) if losses else 0
 
    # latency_primary: delta > 3s OR >30% of losses are 15s+
    latency_primary = age_delta_ms > 3_000 or stale_loss_pct > 30
 
    # --- Reserve miss signal ---
    miss_losses = sum(1 for r in records if r["had_reserve_miss"] and not r["is_win"])
    miss_total = sum(1 for r in records if r["had_reserve_miss"])
    reserve_pct_of_losses = _pct(miss_losses, losses) if losses else 0
    reserve_discovery_primary = reserve_pct_of_losses > 20 or len(reserve_miss_mints) > 5
 
    # --- Exit regime signal ---
    quoteability_count = sum(1 for r in records if "QUOTEABILITY" in r["close_reason"])
    stale_mark_count = sum(1 for r in records if "STALE_MARK" in r["close_reason"])
    rapid_dump_count = sum(1 for r in records if "RAPID_DUMP" in r["close_reason"] or "DUMP" in r["close_reason"])
    guard_exit_count = sum(1 for r in records if "GUARD" in r["close_reason"])
    veto_exit_count = quoteability_count + stale_mark_count + guard_exit_count
 
    # MFE > 0 but closed red (signal quality, not entry timing)
    mfe_positive_closed_red = sum(
        1 for r in records
        if not r["is_win"] and r["mfe"] > 5.0
    )
    exit_regime_primary = veto_exit_count > losses * 0.4 or mfe_positive_closed_red > losses * 0.25
 
    # Dominant close reason
    reason_counts = defaultdict(int)
    for r in records:
        reason_counts[r["close_reason"]] += 1
    dom_reason = max(reason_counts, key=reason_counts.get) if reason_counts else "N/A"
    dom_count = reason_counts.get(dom_reason, 0)
 
    print(f"  Total trades analysed  : {total}")
    print(f"  Win / Loss             : {wins} / {losses}  ({win_rate:.1f}% win rate)")
    print(f"  Dominant close reason  : {dom_reason} ({dom_count}/{total})")
    print()
    print(f"  Age delta (median loss - win): {age_delta_ms:+,.0f}ms  ({age_delta_ms/1000:+.2f}s)")
    print(f"  Stale losses (>15s)    : {stale_losses}/{losses}  ({stale_loss_pct:.1f}% of losses)")
    print(f"  Reserve-miss losses    : {miss_losses}/{losses}  ({reserve_pct_of_losses:.1f}% of losses)")
    print(f"  QUOTEABILITY exits     : {quoteability_count}")
    print(f"  STALE_MARK exits       : {stale_mark_count}")
    print(f"  Guard/veto exits total : {veto_exit_count}")
    print(f"  Positive-MFE red closes: {mfe_positive_closed_red}")
    print()
 
    def verdict(flag):
        return "YES  ⚠" if flag else "no"
 
    print(f"  latency_primary          : {verdict(latency_primary)}")
    print(f"  reserve_discovery_primary: {verdict(reserve_discovery_primary)}")
    print(f"  exit_regime_primary      : {verdict(exit_regime_primary)}")
    print()
 
    # Narrative
    primaries = []
    if exit_regime_primary:
        primaries.append("exit-regime veto exits (QUOTEABILITY/STALE_MARK guards)")
    if latency_primary:
        primaries.append(f"candidate age latency ({age_delta_ms/1000:+.1f}s median gap)")
    if reserve_discovery_primary:
        primaries.append("reserve discovery failures affecting entry price")
 
    if not primaries:
        print("  Verdict: No single dominant failure mode detected. Losses distributed broadly.")
    elif len(primaries) == 1:
        print(f"  Verdict: PRIMARY driver = {primaries[0]}.")
    else:
        print("  Verdict: Multiple co-primary drivers:")
        for p in primaries:
            print(f"    • {p}")
    print()
 
 
# ─── entry point ───────────────────────────────────────────────────────────────
 
def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analysis/latency_audit.py history.json [runtime_state.json]")
        sys.exit(1)
 
    history_path = sys.argv[1]
    state_path = sys.argv[2] if len(sys.argv) > 2 else None
 
    print(f"\nLoading history from: {history_path}")
    events = load_history(history_path)
    print(f"Loaded {len(events)} events.")
 
    if state_path:
        print(f"Loading runtime state from: {state_path}")
        state = load_runtime_state(state_path)
    else:
        state = {}
 
    records, reserve_miss_mints = build_trade_records(events)
    print(f"Joined {len(records)} trade records ({len(reserve_miss_mints)} reserve-miss mints detected).\n")
 
    if not records:
        print("No trade_close events found. Nothing to analyse.")
        sys.exit(0)
 
    report_age_buckets(records)
    report_close_reasons(records)
    report_stale_trades(records)
    report_reserve_miss(records, reserve_miss_mints)
    report_durability(records)
    report_conclusion(records, reserve_miss_mints)
 
 
if __name__ == "__main__":
    main()

