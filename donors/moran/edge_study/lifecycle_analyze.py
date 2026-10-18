"""
Overlay our entries/exits on the real OHLCV lifecycle for each mint
and grade: edge problem (bad entries) or strategy problem (good entries,
bad exits).

Outputs edge_study/lifecycle_report.md.

Definitions:
  migration_low   = min price in the first 10 candles after first-seen
  peak_ever       = max high in the full captured window
  our_entry_px    = close price of the candle containing entry_ts
  our_exit_px     = close price of the candle containing exit_ts
  post_entry_peak = max high between entry_ts and end of window
  post_exit_peak  = max high between exit_ts and end of window

Questions answered:
  1. % of our entries where post_entry_peak ≥ 2× our_entry_px  (edge ceiling)
  2. % of our losses where post_entry_peak ≥ 1.5× our_entry_px (strategy gap)
  3. median gap between our entry and post_entry_peak (ideal exit timing)
  4. median distance our_entry_px vs migration_low (entry quality)
  5. post-exit shape — did we exit and then the token kept running?
"""
import json
import statistics
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
LIFECYCLE_DIR = ROOT / "edge_study" / "lifecycle"
REPORT = ROOT / "edge_study" / "lifecycle_report.md"


def candle_at(candles: List[Dict], ts: int) -> Optional[Dict]:
    if not candles:
        return None
    best = None
    for c in candles:
        ct = c["unixTime"]
        if ct <= ts:
            best = c
        else:
            break
    return best or candles[0]


def analyze_mint(d: Dict) -> List[Dict]:
    candles = d.get("candles") or []
    if not candles:
        return []
    candles = sorted(candles, key=lambda c: c["unixTime"])

    # migration low = lowest low in first 10 candles
    head = candles[:10]
    migration_low = min(c["l"] for c in head) if head else None
    migration_ts = head[0]["unixTime"] if head else None
    peak_ever = max(c["h"] for c in candles)
    peak_ever_ts = max(candles, key=lambda c: c["h"])["unixTime"]

    out = []
    for t in d.get("trades", []):
        entry_ts = t["entry_ts"]
        exit_ts = t["exit_ts"]
        e_c = candle_at(candles, entry_ts)
        x_c = candle_at(candles, exit_ts)
        if not e_c or not x_c:
            continue
        entry_px = e_c["c"]
        exit_px = x_c["c"]
        if entry_px <= 0:
            continue

        post_entry = [c for c in candles if c["unixTime"] >= entry_ts]
        post_exit = [c for c in candles if c["unixTime"] > exit_ts]

        post_entry_peak = max((c["h"] for c in post_entry), default=entry_px)
        post_entry_peak_ts = max(post_entry, key=lambda c: c["h"])["unixTime"] if post_entry else entry_ts
        post_entry_trough = min((c["l"] for c in post_entry), default=entry_px)

        post_exit_peak = max((c["h"] for c in post_exit), default=exit_px)
        post_exit_peak_ts = max(post_exit, key=lambda c: c["h"])["unixTime"] if post_exit else exit_ts

        out.append({
            "mint": d["mint"],
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "our_roi_pct": t.get("roi_pct"),
            "close_reason": t.get("close_reason"),
            "hold_s": t.get("hold_time_seconds"),

            "migration_low": migration_low,
            "migration_ts": migration_ts,
            "peak_ever": peak_ever,
            "peak_ever_ts": peak_ever_ts,

            "entry_px": entry_px,
            "exit_px": exit_px,

            "post_entry_peak": post_entry_peak,
            "post_entry_peak_ts": post_entry_peak_ts,
            "post_entry_trough": post_entry_trough,
            "post_exit_peak": post_exit_peak,
            "post_exit_peak_ts": post_exit_peak_ts,

            # derived multiples
            "entry_vs_migration_x": entry_px / migration_low if migration_low else None,
            "peak_ever_x_from_migration": peak_ever / migration_low if migration_low else None,
            "post_entry_peak_x": post_entry_peak / entry_px,
            "post_exit_peak_x_from_exit": post_exit_peak / exit_px if exit_px else None,
            "our_mfe_pct": (post_entry_peak / entry_px - 1) * 100,
            "our_mae_pct": (post_entry_trough / entry_px - 1) * 100,
        })
    return out


def pct(n, d):
    return (100 * n / d) if d else 0


def fmt_pct(x):
    return f"{x:+.1f}%" if isinstance(x, (int, float)) else "—"


def fmt_x(x):
    return f"{x:.2f}x" if isinstance(x, (int, float)) else "—"


def main() -> None:
    records = []
    skipped_no_candles = 0
    files = sorted(LIFECYCLE_DIR.glob("*.json"))
    for f in files:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if not d.get("candles"):
            skipped_no_candles += 1
            continue
        records.extend(analyze_mint(d))

    if not records:
        print("no records — run lifecycle_extract.py first")
        return

    n = len(records)
    winners = [r for r in records if (r["our_roi_pct"] or 0) > 5]
    losers = [r for r in records if (r["our_roi_pct"] or 0) < -5]

    # --- edge metrics (from migration) ---
    from_mig_2x = [r for r in records if (r.get("peak_ever_x_from_migration") or 0) >= 2.0]
    from_mig_5x = [r for r in records if (r.get("peak_ever_x_from_migration") or 0) >= 5.0]
    from_mig_10x = [r for r in records if (r.get("peak_ever_x_from_migration") or 0) >= 10.0]

    # --- entry quality (we bought near the top?) ---
    entry_multiples = [r["entry_vs_migration_x"] for r in records if r.get("entry_vs_migration_x")]
    entry_multiples.sort()

    # --- strategy gap (we sold into pain, then it ran) ---
    losers_with_mfe_50 = [r for r in losers if r["our_mfe_pct"] >= 50]
    losers_with_mfe_100 = [r for r in losers if r["our_mfe_pct"] >= 100]
    losers_with_post_exit_2x = [r for r in losers if (r.get("post_exit_peak_x_from_exit") or 0) >= 2.0]

    # --- ideal exit timing ---
    mfes = [r["our_mfe_pct"] for r in records if r.get("our_mfe_pct") is not None]
    mfes.sort()
    gap_to_peak_s = [
        (r["post_entry_peak_ts"] - r["entry_ts"])
        for r in records if r.get("post_entry_peak_ts")
    ]
    gap_to_peak_s.sort()

    def med(xs):
        return statistics.median(xs) if xs else None

    def p(xs, q):
        if not xs:
            return None
        i = int(q * (len(xs) - 1))
        return xs[i]

    our_rois = [r["our_roi_pct"] for r in records if r["our_roi_pct"] is not None]
    avg_our_roi = statistics.mean(our_rois) if our_rois else 0

    # close_reason breakdown × ideal outcome
    reasons: Dict[str, Dict[str, float]] = {}
    for r in records:
        cr = r.get("close_reason") or "UNKNOWN"
        b = reasons.setdefault(cr, {"n": 0, "our_sum": 0.0, "mfe_sum": 0.0, "mfe_ge_50": 0})
        b["n"] += 1
        b["our_sum"] += r.get("our_roi_pct") or 0
        b["mfe_sum"] += r.get("our_mfe_pct") or 0
        if (r.get("our_mfe_pct") or 0) >= 50:
            b["mfe_ge_50"] += 1

    lines = []
    ap = lines.append
    ap("# Lifecycle Study — Edge vs Strategy")
    ap(f"_Sample: {n} trades across {len(files)} unique mints (skipped {skipped_no_candles} with no candles)_")
    ap("")
    ap("## Headline")
    ap(f"- Our average ROI over these trades: **{avg_our_roi:+.2f}%**")
    ap(f"- % of mints that eventually hit ≥2x from migration_low: **{pct(len(from_mig_2x), n):.1f}%**")
    ap(f"- % of mints that eventually hit ≥5x from migration_low: **{pct(len(from_mig_5x), n):.1f}%**")
    ap(f"- % of mints that eventually hit ≥10x from migration_low: **{pct(len(from_mig_10x), n):.1f}%**")
    ap("")

    ap("## Entry Quality — did we buy near the low, or near the top?")
    ap("")
    ap("`entry_vs_migration_x` = our entry price ÷ migration low. 1.0 = we bought the bottom. Higher = we bought the pump.")
    ap("")
    ap(f"| pct | entry_x_from_migration |")
    ap("|---|---|")
    for q, label in [(0.10, "p10"), (0.25, "p25"), (0.50, "median"), (0.75, "p75"), (0.90, "p90")]:
        ap(f"| {label} | {fmt_x(p(entry_multiples, q))} |")
    ap("")

    ap("## Strategy Gap — did our losses eventually become winners?")
    ap("")
    ap(f"- Losers (our ROI < -5%): **{len(losers)}** ({pct(len(losers), n):.1f}% of trades)")
    ap(f"- Losers that reached ≥+50% MFE after our entry: **{len(losers_with_mfe_50)}** ({pct(len(losers_with_mfe_50), len(losers)):.1f}% of losers)")
    ap(f"- Losers that reached ≥+100% MFE after our entry: **{len(losers_with_mfe_100)}** ({pct(len(losers_with_mfe_100), len(losers)):.1f}% of losers)")
    ap(f"- Losers where post-exit peak ≥2x our exit price: **{len(losers_with_post_exit_2x)}** ({pct(len(losers_with_post_exit_2x), len(losers)):.1f}% of losers)")
    ap("")

    ap("## MFE distribution (max % we could have closed at)")
    ap("")
    ap(f"| pct | MFE |")
    ap("|---|---|")
    for q, label in [(0.10, "p10"), (0.25, "p25"), (0.50, "median"), (0.75, "p75"), (0.90, "p90")]:
        ap(f"| {label} | {fmt_pct(p(mfes, q))} |")
    ap("")

    ap("## Ideal exit timing — gap from entry → peak")
    ap("")
    ap(f"| pct | seconds to peak |")
    ap("|---|---|")
    for q, label in [(0.10, "p10"), (0.25, "p25"), (0.50, "median"), (0.75, "p75"), (0.90, "p90")]:
        sec = p(gap_to_peak_s, q)
        ap(f"| {label} | {sec}s ({sec//60}m) |" if sec is not None else f"| {label} | — |")
    ap("")

    ap("## Close reason breakdown")
    ap("")
    ap("| close_reason | n | our avg ROI | avg MFE | % with MFE ≥+50% |")
    ap("|---|---:|---:|---:|---:|")
    for cr, b in sorted(reasons.items(), key=lambda kv: -kv[1]["n"]):
        if b["n"] == 0:
            continue
        ap(f"| {cr} | {b['n']} | {b['our_sum']/b['n']:+.1f}% | {b['mfe_sum']/b['n']:+.1f}% | {pct(b['mfe_ge_50'], b['n']):.1f}% |")
    ap("")

    # verdict
    ap("## Verdict")
    ap("")
    edge_pct = pct(len(from_mig_2x), n)
    strategy_gap_pct = pct(len(losers_with_mfe_50), len(losers)) if losers else 0
    if edge_pct < 20 and strategy_gap_pct < 20:
        verdict = "**NO EDGE.** Most tokens we pick never double from migration, and most losers never recover. The universe of copyable winners is too thin."
    elif edge_pct >= 40 and strategy_gap_pct >= 40:
        verdict = "**STRATEGY PROBLEM.** Tokens we pick routinely double and losers routinely recover — we're cutting winners too early and eating real drawdowns."
    elif edge_pct >= 40 and strategy_gap_pct < 20:
        verdict = "**GOOD ENTRY, DECENT EXIT.** Winners exist and we capture them; losers are genuinely dead."
    elif edge_pct < 20 and strategy_gap_pct >= 40:
        verdict = "**CHASING PUMPS.** Few tokens ever run from migration, but once we buy, many do run and we stop out. We're entering after the top."
    else:
        verdict = "**MIXED.** Some edge, some strategy gap. Needs config-level tuning rather than a full rebuild."
    ap(verdict)
    ap("")
    ap(f"- `edge_ceiling` (% of mints that 2x from migration): **{edge_pct:.1f}%**")
    ap(f"- `strategy_gap` (% of losers with MFE ≥+50% post-entry): **{strategy_gap_pct:.1f}%**")
    ap("")

    REPORT.write_text("\n".join(lines))
    print(f"wrote {REPORT} — {n} trades, {len(files)} mints")


if __name__ == "__main__":
    main()
