"""
Pull tick-level (every swap) data around each trade's entry and see what
actually happened — what 1m candles were hiding.

Uses Birdeye /defi/txs/token, which IS available on free tier.

For each selected mint:
  - Fetch txs centered on entry_ts (paginated via before_time)
  - Build a price tape at actual tx resolution
  - Compute: seconds_to_peak, peak_price_vs_entry, max_dump_in_first_Ns,
    tx_count_in_first_minute, volume_in_first_minute, wallet_count

Output: edge_study/tick_report.md + per-mint price tapes in
edge_study/ticks/<mint>.json
"""
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List

import httpx

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "history.json"
OUT_DIR = ROOT / "edge_study" / "ticks"
REPORT = ROOT / "edge_study" / "tick_report.md"

BIRDEYE_KEY = os.environ.get("BIRDEYE_API_KEY", "")
HDR = {"accept": "application/json", "x-chain": "solana", "X-API-KEY": BIRDEYE_KEY}
BASE = "https://public-api.birdeye.so/defi/txs/token"
RPS = 1.2
_CLIENT = httpx.Client(timeout=30, trust_env=False)


def fetch_txs_back_to(mint: str, stop_before_ts: int, stop_after_ts: int, max_offset: int = 4000) -> List[Dict]:
    """Offset-paginate desc. Keep going until oldest tx on page < stop_before_ts."""
    all_txs = []
    seen_hashes = set()
    offset = 0
    while offset <= max_offset:
        params = {"address": mint, "offset": offset, "limit": 50, "sort_type": "desc"}
        try:
            r = _CLIENT.get(BASE, params=params, headers=HDR)
        except Exception as e:
            print(f"    err off={offset}: {e}")
            break
        if r.status_code == 429:
            time.sleep(4)
            continue
        if r.status_code != 200:
            print(f"    http {r.status_code} off={offset}: {r.text[:100]}")
            break
        items = (r.json().get("data") or {}).get("items") or []
        if not items:
            break
        # skip forward until we reach post-exit window start
        newest_ts = max(it.get("blockUnixTime", 0) for it in items)
        oldest_ts = min(it.get("blockUnixTime", 0) for it in items)
        # only keep txs within our analysis window
        for it in items:
            ts = it.get("blockUnixTime", 0)
            if stop_before_ts <= ts <= stop_after_ts:
                h = it.get("txHash")
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    all_txs.append(it)
        # stop if whole page is older than our window
        if newest_ts < stop_before_ts:
            break
        offset += 50
        time.sleep(RPS)
    return all_txs


def price_from_tx(tx: Dict) -> float:
    return float(tx.get("tokenPrice") or (tx.get("base") or {}).get("price") or 0)


def tx_direction(tx: Dict) -> str:
    # "side" is "buy" or "sell" from base-token POV
    return tx.get("side") or "?"


def wallet(tx: Dict) -> str:
    return tx.get("owner") or tx.get("from") or "?"


def analyze_one(mint: str, entry_ts: int, our_roi: float, label: str) -> Dict:
    # analysis window: 2 min pre-entry → 30 min post-entry
    txs = fetch_txs_back_to(mint, entry_ts - 120, entry_ts + 1800)
    if not txs:
        return {"mint": mint, "label": label, "n_txs": 0}
    txs = sorted(txs, key=lambda t: t.get("blockUnixTime", 0))
    pre = [t for t in txs if t["blockUnixTime"] <= entry_ts]
    post = [t for t in txs if t["blockUnixTime"] > entry_ts]

    # entry price: closest tx to entry_ts
    entry_px = None
    if pre:
        entry_px = price_from_tx(pre[-1])
    if not entry_px and post:
        entry_px = price_from_tx(post[0])
    if not entry_px or entry_px <= 0:
        return {"mint": mint, "label": label, "n_txs": len(txs), "error": "no entry px"}

    # peak after entry
    peak_px = entry_px
    peak_ts = entry_ts
    for t in post:
        p = price_from_tx(t)
        if p > peak_px:
            peak_px = p
            peak_ts = t["blockUnixTime"]
    # trough after entry
    trough_px = entry_px
    trough_ts = entry_ts
    for t in post:
        p = price_from_tx(t)
        if p > 0 and p < trough_px:
            trough_px = p
            trough_ts = t["blockUnixTime"]

    # Within first 60s / 10s post-entry
    first_60s = [t for t in post if t["blockUnixTime"] - entry_ts <= 60]
    first_10s = [t for t in post if t["blockUnixTime"] - entry_ts <= 10]
    buys_60s = sum(1 for t in first_60s if tx_direction(t) == "buy")
    sells_60s = sum(1 for t in first_60s if tx_direction(t) == "sell")
    vol_60s = sum(abs(float((t.get("quote") or {}).get("uiChangeAmount") or 0)) for t in first_60s)
    wallets_60s = len(set(wallet(t) for t in first_60s))

    # sub-minute MFE/MAE
    mfe_pct = (peak_px / entry_px - 1) * 100 if entry_px else 0
    mae_pct = (trough_px / entry_px - 1) * 100 if entry_px else 0

    # Save full tape
    (OUT_DIR / f"{mint}.json").write_text(json.dumps({
        "mint": mint,
        "entry_ts": entry_ts,
        "entry_px": entry_px,
        "our_roi_pct": our_roi,
        "label": label,
        "txs": txs,
    }))

    return {
        "mint": mint,
        "label": label,
        "our_roi_pct": our_roi,
        "entry_px": entry_px,
        "peak_px": peak_px,
        "trough_px": trough_px,
        "seconds_to_peak": peak_ts - entry_ts,
        "seconds_to_trough": trough_ts - entry_ts,
        "real_mfe_pct": mfe_pct,
        "real_mae_pct": mae_pct,
        "n_txs_total": len(txs),
        "n_txs_first_10s": len(first_10s),
        "n_txs_first_60s": len(first_60s),
        "buys_first_60s": buys_60s,
        "sells_first_60s": sells_60s,
        "sol_volume_first_60s": vol_60s,
        "unique_wallets_first_60s": wallets_60s,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    h = json.loads(HISTORY.read_text())
    closes = [e for e in h if e.get("type") == "trade_close"]
    # dedup by mint, keep best ROI per mint
    by_mint = {}
    for e in closes:
        m = e.get("mint")
        if m and (m not in by_mint or (e.get("roi_pct") or 0) > (by_mint[m].get("roi_pct") or 0)):
            by_mint[m] = e
    uniq = list(by_mint.values())
    top_wins = sorted(uniq, key=lambda e: -(e.get("roi_pct") or 0))[:5]
    top_losses = sorted(uniq, key=lambda e: (e.get("roi_pct") or 0))[:5]

    records = []
    for t in top_wins:
        print(f"[winner {t['roi_pct']:+.1f}%] {t['mint'][:12]}")
        records.append(analyze_one(t["mint"], t["entry_ts"], t["roi_pct"], "winner"))
    for t in top_losses:
        print(f"[loser  {t['roi_pct']:+.1f}%] {t['mint'][:12]}")
        records.append(analyze_one(t["mint"], t["entry_ts"], t["roi_pct"], "loser"))

    # Report
    lines = []
    ap = lines.append
    ap("# Tick-Level Reality Check")
    ap(f"_Sub-second data from Birdeye `/defi/txs/token` for 5 biggest wins + 5 biggest losses. Uncovers what 1m candles hid._")
    ap("")
    ap("## Per-trade tick tape")
    ap("")
    ap("| label | mint | our ROI | real MFE | sec→peak | real MAE | sec→trough | n_txs 60s | buys/sells 60s | wallets 60s | vol SOL 60s |")
    ap("|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|")
    for r in records:
        if "error" in r or r.get("n_txs_total", 0) == 0:
            roi = r.get('our_roi_pct') or 0
            ap(f"| {r.get('label')} | {r['mint'][:10]} | {roi:+.0f}% | — | — | — | — | — | — | — | — |")
            continue
        ap(
            f"| {r['label']} | {r['mint'][:10]} | {r['our_roi_pct']:+.0f}% | "
            f"{r['real_mfe_pct']:+.0f}% | {r['seconds_to_peak']}s | "
            f"{r['real_mae_pct']:+.0f}% | {r['seconds_to_trough']}s | "
            f"{r['n_txs_first_60s']} | {r['buys_first_60s']}/{r['sells_first_60s']} | "
            f"{r['unique_wallets_first_60s']} | {r['sol_volume_first_60s']:.2f} |"
        )
    ap("")

    ap("## Comparison with 1m-candle analysis")
    ap("")
    ap("(Shows how much signal the 1m-candle backtest was missing.)")
    ap("")
    from_1m = {}
    for f in (ROOT / "edge_study" / "lifecycle").glob("*.json"):
        try:
            d = json.loads(f.read_text())
            from_1m[d["mint"]] = d
        except Exception:
            pass
    ap("| mint | tick MFE | 1m-candle MFE | gap |")
    ap("|---|---:|---:|---|")
    for r in records:
        if "error" in r or r.get("n_txs_total", 0) == 0:
            continue
        lc = from_1m.get(r["mint"])
        if not lc:
            continue
        candles = sorted(lc.get("candles") or [], key=lambda c: c["unixTime"])
        # rebuild 1m MFE from entry
        entry_ts = lc["trades"][0]["entry_ts"]
        post_c = [c for c in candles if c["unixTime"] >= entry_ts]
        if not post_c:
            continue
        entry_bar = post_c[0]
        entry_c_px = entry_bar["c"]
        peak_c = max(c["h"] for c in post_c) if post_c else entry_c_px
        mfe_1m = (peak_c / entry_c_px - 1) * 100 if entry_c_px > 0 else 0
        gap = r["real_mfe_pct"] - mfe_1m
        ap(f"| {r['mint'][:10]} | {r['real_mfe_pct']:+.0f}% | {mfe_1m:+.0f}% | {gap:+.0f}pp |")
    ap("")

    ap("## What the tick data enables")
    ap("")
    ap("- Actual seconds-to-peak per trade (not coarse-minute estimates)")
    ap("- Real peak price, not candle-high (which can be a 1-sec spike we'd never fill)")
    ap("- Buyer/seller ratio in first 10s-60s post-entry — a candidate pre-exit gating signal")
    ap("- Unique wallet count in first 60s — proxy for organic interest vs bot-dominated")
    ap("- SOL volume in first 60s — thinness detector")
    ap("")
    ap("Full tx tapes saved to `edge_study/ticks/<mint>.json` for further analysis.")

    REPORT.write_text("\n".join(lines))
    print(f"wrote {REPORT}")


if __name__ == "__main__":
    main()
