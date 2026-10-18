#!/usr/bin/env python3
"""
review_trades.py — Print last N trades with price timeline from snapshots.

Usage:
    python3 review_trades.py           # last 3 trades + price timeline
    python3 review_trades.py 7         # last 7 trades
    python3 review_trades.py --all     # all trades
    python3 review_trades.py --json    # raw trade JSON
    python3 review_trades.py --no-timeline   # skip snapshot timeline
    python3 review_trades.py --snapshots     # dump raw snapshot JSONL summary
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone


def _find_file(name: str) -> Path:
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


HISTORY_FILE = _find_file("history.json")
SNAPSHOT_FILE = _find_file("metrics_snapshots.jsonl")


def ts_fmt(ts: float) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%H:%M:%S UTC")


def hold_fmt(s: float) -> str:
    s = float(s or 0)
    if s < 60:
        return f"{s:.0f}s"
    return f"{int(s)//60}m{int(s)%60:02d}s"


def latency_fmt(stop: float, attempt: float, success: float) -> str:
    parts = []
    if stop and attempt and attempt > stop:
        parts.append(f"trigger→attempt: {(attempt - stop)*1000:.0f}ms")
    if attempt and success and success > attempt:
        parts.append(f"attempt→confirm: {(success - attempt)*1000:.0f}ms")
    if stop and success and success > stop:
        parts.append(f"total: {(success - stop)*1000:.0f}ms")
    return "  ".join(parts) if parts else "no latency data"


def load_snapshots() -> list[dict]:
    import gzip
    rows: list[dict] = []
    parent = SNAPSHOT_FILE.parent if SNAPSHOT_FILE.exists() else Path(__file__).parent
    for gz in sorted(parent.glob("metrics_snapshots.*.jsonl.gz")):
        try:
            with gzip.open(gz, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except Exception:
            continue
    if SNAPSHOT_FILE.exists():
        try:
            with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def snapshots_for_trade(mint: str, entry_ts: float, exit_ts: float, snapshots: list[dict]) -> list[dict]:
    """Return snapshots covering this trade's lifetime, filtered to this mint."""
    result = []
    for snap in snapshots:
        ts = float(snap.get("ts") or 0)
        if ts < entry_ts - 2 or ts > exit_ts + 2:
            continue
        for pos in snap.get("positions") or []:
            if pos.get("mint") == mint:
                result.append({"ts": ts, **pos})
                break
    return result


def print_timeline(timeline: list[dict]) -> None:
    if not timeline:
        print("  Timeline  no snapshots recorded during this trade")
        return

    # Sample up to 10 evenly-spaced points
    step = max(1, len(timeline) // 10)
    samples = timeline[::step]
    if timeline[-1] not in samples:
        samples.append(timeline[-1])

    print(f"  Timeline  ({len(timeline)} snapshots, showing {len(samples)}):")
    for s in samples:
        roi = float(s.get("roi_pct") or 0.0)
        src = (s.get("mark_source") or "?")[:12]
        stale = " STALE" if s.get("mark_stale") else ""
        sign = "+" if roi >= 0 else ""
        bar_len = min(15, max(1, abs(int(roi // 3))))
        bar = ("▲" if roi >= 0 else "▼") * bar_len
        print(f"           {ts_fmt(s['ts'])}  {sign}{roi:6.2f}%  {bar:<15}  [{src}{stale}]")


def print_trade(t: dict, idx: int, total: int, timeline: list[dict], show_timeline: bool) -> None:
    roi = float(t.get("roi_pct") or 0.0)
    pnl = float(t.get("pnl_usd") or 0.0)
    mfe = float(t.get("mfe_pct") or 0.0)
    mae = float(t.get("mae_pct") or 0.0)
    hold = float(t.get("hold_time_seconds") or 0.0)
    reason = t.get("close_reason") or "?"
    mint = t.get("mint") or "?"
    entry_ts = float(t.get("entry_ts") or 0.0)
    exit_ts = float(t.get("ts") or 0.0)
    stop_ts = float(t.get("stop_trigger_ts") or 0.0)
    attempt_ts = float(t.get("sell_attempt_ts") or 0.0)
    success_ts = float(t.get("sell_success_ts") or 0.0)
    proceeds = float(t.get("proceeds_usd") or 0.0)
    basis = float(t.get("realized_basis_usd") or 0.0)
    fees = float(t.get("fees_usd") or 0.0)
    qty_remaining = float(t.get("qty_remaining") or 0.0)

    win = roi >= 0
    sign = "+" if win else ""
    bar = "█" * min(20, max(1, abs(int(roi // 5)))) if roi != 0 else "·"
    D = "─" * 60

    print(f"\n{D}")
    print(f"  Trade {total - idx}  │  {ts_fmt(exit_ts)}  │  {mint[:16]}…{mint[-4:]}")
    print(D)
    print(f"  ROI       {sign}{roi:.2f}%   {'▲ WIN' if win else '▼ LOSS'}  {bar}")
    print(f"  P&L       {sign}${pnl:.4f}   (basis ${basis:.4f}  proceeds ${proceeds:.4f}  fees ${fees:.6f})")
    print(f"  Reason    {reason}")
    print(f"  Hold      {hold_fmt(hold)}   (entry {ts_fmt(entry_ts)} → exit {ts_fmt(exit_ts)})")
    print(f"  MFE/MAE   best: +{mfe:.2f}%   worst: -{mae:.2f}%")
    if qty_remaining > 0:
        print(f"  Moonbag   {qty_remaining:.2f} tokens remaining")
    if stop_ts or attempt_ts or success_ts:
        print(f"  Latency   {latency_fmt(stop_ts, attempt_ts, success_ts)}")
    if show_timeline:
        print_timeline(timeline)
    print(D)


def print_session_summary(snapshots: list[dict]) -> None:
    if not snapshots:
        return
    last = snapshots[-1]
    sess = last.get("session") or {}
    funnel = last.get("funnel") or {}
    wins = int(sess.get("closed_wins") or 0)
    losses = int(sess.get("closed_losses") or 0)
    total = wins + losses
    wr = f"{wins/total*100:.0f}%" if total else "—"
    print(f"\n{'═'*60}")
    print(f"  SESSION SNAPSHOT  │  {ts_fmt(float(last.get('ts') or 0))}")
    print(f"{'═'*60}")
    print(f"  Mode          {sess.get('execution_mode','?')}")
    print(f"  Open pos      {sess.get('open_positions', 0)}")
    print(f"  Unrealized    ${float(sess.get('total_unrealized_usd') or 0):.4f}")
    print(f"  Realized P&L  ${float(sess.get('realized_pnl_usd') or 0):.4f}")
    print(f"  W/L           {wins}/{losses}  ({wr} win rate)")
    print(f"  Budget left   ${float(sess.get('budget_usd') or 0):.4f}")
    print(f"  Qual cands    {funnel.get('qualified_candidates', 0)}")
    print(f"  No route veto {funnel.get('entry_no_swap_route', 0)}")
    print(f"  Drift veto    {funnel.get('entry_drift_rejected', 0)}")
    print(f"  Exec success  {funnel.get('execution_succeeded', 0)}")
    print(f"  Exec failed   {funnel.get('execution_failed', 0)}")
    print(f"  Quot exits    {int(sess.get('quoteability_guard_exits') or 0)}")
    print(f"{'═'*60}")


def main():
    args = sys.argv[1:]
    show_json = "--json" in args
    show_all = "--all" in args
    show_snapshots = "--snapshots" in args
    no_timeline = "--no-timeline" in args
    n = next((int(a) for a in args if a.isdigit()), 3)

    # --snapshots: just dump snapshot summary
    if show_snapshots:
        snaps = load_snapshots()
        print(f"Snapshot file: {SNAPSHOT_FILE}")
        print(f"Total snapshots: {len(snaps)}")
        if snaps:
            print_session_summary(snaps)
        return

    if not HISTORY_FILE.exists():
        print(f"[ERROR] history.json not found at {HISTORY_FILE}")
        sys.exit(1)

    try:
        records = json.loads(HISTORY_FILE.read_text())
    except Exception as e:
        print(f"[ERROR] Could not read history.json: {e}")
        sys.exit(1)

    closes = sorted(
        [r for r in records if r.get("type") == "trade_close"],
        key=lambda r: float(r.get("ts") or 0.0)
    )

    if not closes:
        print("No trade_close records found in history.json.")
        sys.exit(0)

    if show_json:
        print(json.dumps(closes if show_all else closes[-n:], indent=2))
        return

    snapshots = load_snapshots()
    recent = closes if show_all else closes[-n:]
    total = len(closes)
    wins = [t for t in closes if float(t.get("roi_pct") or 0) >= 0]
    total_pnl = sum(float(t.get("pnl_usd") or 0) for t in closes)

    print(f"\n{'═'*60}")
    print(f"  TRADE REVIEW  │  {len(recent)} of {total} trades")
    print(f"  All-time: {total}  │  W/L: {len(wins)}/{total-len(wins)}  "
          f"({len(wins)/total*100:.0f}% WR)  │  P&L: ${total_pnl:.4f}")
    if snapshots:
        print(f"  Snapshots: {len(snapshots)} records  │  {ts_fmt(float(snapshots[0].get('ts') or 0))} → {ts_fmt(float(snapshots[-1].get('ts') or 0))}")
    print(f"{'═'*60}")

    for idx, t in enumerate(reversed(recent)):
        mint = t.get("mint") or ""
        entry_ts = float(t.get("entry_ts") or 0.0)
        exit_ts = float(t.get("ts") or 0.0)
        timeline = snapshots_for_trade(mint, entry_ts, exit_ts, snapshots)
        print_trade(t, idx, total, timeline, show_timeline=not no_timeline)

    # Show current session state from most recent snapshot
    if snapshots:
        print_session_summary(snapshots)

    print()


if __name__ == "__main__":
    main()
