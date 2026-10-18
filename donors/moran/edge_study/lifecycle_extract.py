"""
Fetch Birdeye OHLCV for every mint we closed, covering the window
pre-entry → post-exit so we can grade edge vs strategy offline.

Cache: edge_study/lifecycle/<mint>.json
Resume: existing files are skipped.

Usage:
  python3 edge_study/lifecycle_extract.py              # fetch all uncached
  python3 edge_study/lifecycle_extract.py --limit 20   # smoke test
  python3 edge_study/lifecycle_extract.py --force      # refetch
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "history.json"
OUT_DIR = ROOT / "edge_study" / "lifecycle"

BIRDEYE_KEY = os.environ.get("BIRDEYE_API_KEY", "")
BASE = "https://public-api.birdeye.so/defi/ohlcv"

PRE_ENTRY_BUFFER_S = 30 * 60
POST_EXIT_BUFFER_S = 4 * 60 * 60
RESOLUTION = "1m"
CANDLES_PER_CALL = 1000
RPS_SLEEP = 1.1

_CLIENT = httpx.Client(timeout=20, trust_env=False)


def load_trades() -> Dict[str, Dict]:
    events = json.loads(HISTORY.read_text())
    per_mint: Dict[str, Dict] = {}
    for e in events:
        if e.get("type") != "trade_close":
            continue
        mint = e.get("mint")
        if not mint:
            continue
        entry_ts = int(e.get("entry_ts") or 0)
        exit_ts = int(e.get("ts") or 0)
        if entry_ts <= 0 or exit_ts <= 0:
            continue
        rec = per_mint.setdefault(mint, {
            "mint": mint,
            "trades": [],
            "first_entry_ts": entry_ts,
            "last_exit_ts": exit_ts,
        })
        rec["trades"].append({
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "hold_time_seconds": e.get("hold_time_seconds"),
            "roi_pct": e.get("roi_pct"),
            "pnl_usd": e.get("pnl_usd"),
            "close_reason": e.get("close_reason"),
            "realized_basis_usd": e.get("realized_basis_usd"),
            "proceeds_usd": e.get("proceeds_usd"),
        })
        rec["first_entry_ts"] = min(rec["first_entry_ts"], entry_ts)
        rec["last_exit_ts"] = max(rec["last_exit_ts"], exit_ts)
    return per_mint


def fetch_candles(mint: str, t_from: int, t_to: int) -> List[Dict]:
    """Fetch 1m OHLCV, paginated. Returns list of {unixTime,o,h,l,c,v}."""
    headers = {
        "accept": "application/json",
        "x-chain": "solana",
        "X-API-KEY": BIRDEYE_KEY,
    }
    out: List[Dict] = []
    window = CANDLES_PER_CALL * 60
    cursor = t_from
    while cursor < t_to:
        window_end = min(cursor + window, t_to)
        params = {
            "address": mint,
            "type": RESOLUTION,
            "time_from": cursor,
            "time_to": window_end,
        }
        try:
            r = _CLIENT.get(BASE, params=params, headers=headers)
        except httpx.HTTPError as e:
            print(f"    net error {e}", file=sys.stderr)
            time.sleep(2.0)
            cursor = window_end
            continue
        if r.status_code == 429:
            print("    429 — backing off 10s", file=sys.stderr)
            time.sleep(10.0)
            continue
        if r.status_code != 200:
            print(f"    http {r.status_code}: {r.text[:120]}", file=sys.stderr)
            break
        body = r.json()
        items = (body.get("data") or {}).get("items") or []
        out.extend(items)
        time.sleep(RPS_SLEEP)
        cursor = window_end
        if not items:
            # no data in this window — keep going, token may have later activity
            pass
    # dedupe by unixTime, sort
    seen = {}
    for c in out:
        t = c.get("unixTime")
        if t is None:
            continue
        seen[t] = c
    return [seen[t] for t in sorted(seen.keys())]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only fetch first N uncached mints")
    ap.add_argument("--force", action="store_true", help="refetch even if cached")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    per_mint = load_trades()
    mints = sorted(per_mint.keys())
    print(f"found {len(mints)} unique mints across trade_close events")

    targets = []
    for m in mints:
        cache = OUT_DIR / f"{m}.json"
        if cache.exists() and not args.force:
            continue
        targets.append(m)
    print(f"{len(targets)} mints to fetch (cached: {len(mints) - len(targets)})")
    if args.limit > 0:
        targets = targets[: args.limit]
        print(f"  limiting to {len(targets)}")

    for i, mint in enumerate(targets, 1):
        rec = per_mint[mint]
        t_from = rec["first_entry_ts"] - PRE_ENTRY_BUFFER_S
        t_to = rec["last_exit_ts"] + POST_EXIT_BUFFER_S
        print(f"[{i}/{len(targets)}] {mint[:10]}… span={(t_to-t_from)//60}m trades={len(rec['trades'])}")
        candles = fetch_candles(mint, t_from, t_to)
        rec["candles"] = candles
        rec["fetch_ts"] = int(time.time())
        rec["window"] = {"time_from": t_from, "time_to": t_to, "resolution": RESOLUTION}
        (OUT_DIR / f"{mint}.json").write_text(json.dumps(rec))
        print(f"    → {len(candles)} candles")


if __name__ == "__main__":
    main()
