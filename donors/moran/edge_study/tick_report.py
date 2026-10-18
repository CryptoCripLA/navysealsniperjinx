"""Generate tick_report.md from cached edge_study/ticks/*.json (no refetch)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TICKS = ROOT / "edge_study" / "ticks"
LIFECYCLE = ROOT / "edge_study" / "lifecycle"
REPORT = ROOT / "edge_study" / "tick_report.md"


def price_from_tx(tx):
    return float(tx.get("tokenPrice") or (tx.get("base") or {}).get("price") or 0)


def side(tx):
    return tx.get("side") or "?"


def wallet(tx):
    return tx.get("owner") or tx.get("from") or "?"


def analyze(tape):
    txs = sorted(tape["txs"], key=lambda t: t.get("blockUnixTime", 0))
    entry_ts = tape["entry_ts"]
    entry_px = tape.get("entry_px") or 0
    if entry_px <= 0 and txs:
        pre = [t for t in txs if t["blockUnixTime"] <= entry_ts]
        entry_px = price_from_tx(pre[-1]) if pre else price_from_tx(txs[0])
    if entry_px <= 0:
        return None

    post = [t for t in txs if t["blockUnixTime"] > entry_ts]
    peak_px, peak_ts = entry_px, entry_ts
    trough_px, trough_ts = entry_px, entry_ts
    for t in post:
        p = price_from_tx(t)
        if p > peak_px:
            peak_px, peak_ts = p, t["blockUnixTime"]
        if 0 < p < trough_px:
            trough_px, trough_ts = p, t["blockUnixTime"]

    first_60s = [t for t in post if t["blockUnixTime"] - entry_ts <= 60]
    first_10s = [t for t in post if t["blockUnixTime"] - entry_ts <= 10]
    first_300s = [t for t in post if t["blockUnixTime"] - entry_ts <= 300]

    buys_60 = sum(1 for t in first_60s if side(t) == "buy")
    sells_60 = sum(1 for t in first_60s if side(t) == "sell")
    vol_60 = sum(abs(float((t.get("quote") or {}).get("uiChangeAmount") or 0)) for t in first_60s)
    wallets_60 = len(set(wallet(t) for t in first_60s))

    return {
        "mint": tape["mint"],
        "label": tape["label"],
        "our_roi": tape["our_roi_pct"],
        "entry_px": entry_px,
        "peak_px": peak_px,
        "trough_px": trough_px,
        "s_to_peak": peak_ts - entry_ts,
        "s_to_trough": trough_ts - entry_ts,
        "mfe_pct": (peak_px / entry_px - 1) * 100,
        "mae_pct": (trough_px / entry_px - 1) * 100,
        "n_total": len(txs),
        "n_10s": len(first_10s),
        "n_60s": len(first_60s),
        "n_300s": len(first_300s),
        "buys_60s": buys_60,
        "sells_60s": sells_60,
        "wallets_60s": wallets_60,
        "vol_sol_60s": vol_60,
    }


def main():
    records = []
    for f in sorted(TICKS.glob("*.json")):
        tape = json.loads(f.read_text())
        r = analyze(tape)
        if r:
            records.append(r)

    # label ordering
    records.sort(key=lambda r: (0 if r["label"] == "winner" else 1, -r["our_roi"]))

    lines = []
    ap = lines.append
    ap("# Tick-Level Reality Check")
    ap("_Sub-second data from Birdeye `/defi/txs/token` — every swap, not 1m aggregates._")
    ap("")
    ap(f"_N={len(records)} trades (5 biggest wins + 5 biggest losses, dedup'd by mint)._")
    ap("")
    ap("## Per-trade tick tape")
    ap("")
    ap("| label | mint | our ROI | real MFE | s→peak | real MAE | s→trough | txs 10s/60s/300s | buys/sells 60s | wallets 60s | SOL vol 60s |")
    ap("|---|---|---:|---:|---:|---:|---:|---|---|---:|---:|")
    for r in records:
        ap(
            f"| {r['label']} | {r['mint'][:10]} | {r['our_roi']:+.0f}% | "
            f"{r['mfe_pct']:+.0f}% | {r['s_to_peak']}s | "
            f"{r['mae_pct']:+.0f}% | {r['s_to_trough']}s | "
            f"{r['n_10s']}/{r['n_60s']}/{r['n_300s']} | "
            f"{r['buys_60s']}/{r['sells_60s']} | "
            f"{r['wallets_60s']} | {r['vol_sol_60s']:.3f} |"
        )
    ap("")

    # 1m-candle MFE comparison
    ap("## Tick MFE vs 1m-candle MFE — the gap")
    ap("")
    ap("_Quantifies what our 1m-resolution backtest could not see._")
    ap("")
    from_1m = {}
    for f in LIFECYCLE.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            from_1m[d.get("mint") or ""] = d
        except Exception:
            pass

    ap("| label | mint | tick MFE | 1m MFE | gap | tick MAE | 1m MAE |")
    ap("|---|---|---:|---:|---:|---:|---:|")
    for r in records:
        lc = from_1m.get(r["mint"])
        if not lc:
            ap(f"| {r['label']} | {r['mint'][:10]} | {r['mfe_pct']:+.0f}% | — | — | {r['mae_pct']:+.0f}% | — |")
            continue
        entry_ts = (lc.get("trades") or [{}])[0].get("entry_ts", 0)
        candles = sorted(lc.get("candles") or [], key=lambda c: c["unixTime"])
        post_c = [c for c in candles if c["unixTime"] >= entry_ts]
        if not post_c:
            ap(f"| {r['label']} | {r['mint'][:10]} | {r['mfe_pct']:+.0f}% | (no post candles) | — | {r['mae_pct']:+.0f}% | — |")
            continue
        entry_c = post_c[0]["c"]
        peak_c = max(c["h"] for c in post_c)
        trough_c = min(c["l"] for c in post_c if c["l"] > 0)
        mfe_1m = (peak_c / entry_c - 1) * 100 if entry_c > 0 else 0
        mae_1m = (trough_c / entry_c - 1) * 100 if entry_c > 0 else 0
        gap = r["mfe_pct"] - mfe_1m
        ap(f"| {r['label']} | {r['mint'][:10]} | {r['mfe_pct']:+.0f}% | {mfe_1m:+.0f}% | {gap:+.0f}pp | {r['mae_pct']:+.0f}% | {mae_1m:+.0f}% |")
    ap("")

    # Winners vs losers aggregate stats
    wins = [r for r in records if r["label"] == "winner"]
    losses = [r for r in records if r["label"] == "loser"]

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else 0

    ap("## Aggregate: first-60s tick signature")
    ap("")
    ap("_Is there a separation in early-window activity between winners and losers?_")
    ap("")
    ap("| metric | winners median | losers median |")
    ap("|---|---:|---:|")
    ap(f"| txs in first 10s | {med([r['n_10s'] for r in wins])} | {med([r['n_10s'] for r in losses])} |")
    ap(f"| txs in first 60s | {med([r['n_60s'] for r in wins])} | {med([r['n_60s'] for r in losses])} |")
    ap(f"| txs in first 300s | {med([r['n_300s'] for r in wins])} | {med([r['n_300s'] for r in losses])} |")
    ap(f"| buys/sells ratio 60s | {med([r['buys_60s'] / max(r['sells_60s'], 1) for r in wins]):.2f} | {med([r['buys_60s'] / max(r['sells_60s'], 1) for r in losses]):.2f} |")
    ap(f"| unique wallets 60s | {med([r['wallets_60s'] for r in wins])} | {med([r['wallets_60s'] for r in losses])} |")
    ap(f"| SOL volume 60s | {med([r['vol_sol_60s'] for r in wins]):.3f} | {med([r['vol_sol_60s'] for r in losses]):.3f} |")
    ap(f"| seconds to peak | {med([r['s_to_peak'] for r in wins])} | {med([r['s_to_peak'] for r in losses])} |")
    ap("")

    # What this means
    ap("## Interpretation")
    ap("")
    ap("- If tick MFE >> 1m MFE: the 1m-candle backtest was missing real sub-minute moves we might have been able to exit on.")
    ap("- If winners show higher early tx count / wallet count / volume than losers: we have a candidate pre-exit signal.")
    ap("- If medians look similar: tick data doesn't rescue us; the edge lives in external data (dev wallet, security flags) as FINDINGS.md concluded.")
    ap("")
    ap("Raw tapes: `edge_study/ticks/<mint>.json`.")

    REPORT.write_text("\n".join(lines))
    print(f"wrote {REPORT}")
    print(f"n records: {len(records)} ({len(wins)} winners, {len(losses)} losers)")


if __name__ == "__main__":
    main()
