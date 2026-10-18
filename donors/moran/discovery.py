#!/usr/bin/env python3
"""
Phase 7 — Watchlist Quality Filters.

Seven hard filters applied BEFORE the next 50-trade collection batch to
eliminate non-copyable wallets (farmers, micro traders, whales, spam,
stale, inconsistent quoting, unreliable win rate).

Usage:
    python discovery.py                       # self-demo with synthetic seed
    python discovery.py watchlist_seed.json   # real data

Output:
    stdout report + watchlist.json (accepted, capped at WATCHLIST_SIZE)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------
WATCHLIST_SIZE           = 20

MIN_SELL_FRACTION        = 0.20    # F1: >= 20% of trades must be sells
MIN_AVG_TRADE_SIZE_USD   = 50.0    # F2: $50 floor
MAX_AVG_TRADE_SIZE_USD   = 5000.0  # F2: $5k ceiling
MIN_CLOSED_TRADES_7D     = 5       # F4: >= 5 round-trips in 7d
MAX_AVG_HOLD_HOURS       = 24.0    # F5: avg hold <= 24h
MIN_WIN_RATE             = 0.55    # F6: >= 55% win rate (not None)
REPEATED_BUY_MIN_COUNT   = 5       # F7: evaluate when >= 5 recent buys
REPEATED_BUY_MAX_UNIQUE  = 2       # F7: <=2 unique sizes across those buys → spam


# --------------------------------------------------------------------------
# Filters  — each returns None on pass, or a string reason on reject
# --------------------------------------------------------------------------
def _reject_exit_activity(w):
    trades = int(w.get("trades_7d") or 0)
    sells  = int(w.get("sell_count") or 0)
    if trades <= 0:
        return "no trades in last 7d"
    frac = sells / trades
    if frac < MIN_SELL_FRACTION:
        return f"sell fraction {frac:.0%} < {MIN_SELL_FRACTION:.0%} (farmer pattern)"
    return None


def _reject_trade_size(w):
    trades = int(w.get("trades_7d") or 0)
    vol    = float(w.get("volume_usd_7d") or 0.0)
    if trades <= 0:
        return "cannot compute avg trade size (trades_7d=0)"
    avg = vol / trades
    if avg < MIN_AVG_TRADE_SIZE_USD:
        return f"avg trade ${avg:,.2f} < ${MIN_AVG_TRADE_SIZE_USD:.0f} (micro trader)"
    if avg > MAX_AVG_TRADE_SIZE_USD:
        return f"avg trade ${avg:,.2f} > ${MAX_AVG_TRADE_SIZE_USD:,.0f} (whale)"
    return None


def _reject_mixed_quote(w):
    q_map = w.get("quote_currencies_per_mint") or {}
    for mint, quotes in q_map.items():
        uniq = {str(q).upper() for q in (quotes or [])}
        if len(uniq) > 1:
            return f"mint {str(mint)[:8]}.. mixed quotes {sorted(uniq)}"
    return None


def _reject_min_closed(w):
    closed = int(w.get("closed_trades_7d") or 0)
    if closed < MIN_CLOSED_TRADES_7D:
        return f"closed round-trips {closed} < {MIN_CLOSED_TRADES_7D}"
    return None


def _reject_hold_time(w):
    sells = int(w.get("sell_count") or 0)
    if sells == 0:
        return "zero exits observed"
    hold = w.get("avg_hold_time_hours")
    if hold is None:
        return "avg_hold_time_hours missing"
    if float(hold) > MAX_AVG_HOLD_HOURS:
        return f"avg hold {float(hold):.1f}h > {MAX_AVG_HOLD_HOURS:.0f}h (too slow)"
    return None


def _reject_win_rate(w):
    wr = w.get("win_rate")
    if wr is None:
        return "win_rate is None"
    if float(wr) < MIN_WIN_RATE:
        return f"win_rate {float(wr):.0%} < {MIN_WIN_RATE:.0%}"
    return None


def _reject_repeated_buys(w):
    sizes = w.get("recent_buy_sizes_usd") or []
    if len(sizes) < REPEATED_BUY_MIN_COUNT:
        return None
    uniq = {round(float(s), 2) for s in sizes}
    if len(uniq) <= REPEATED_BUY_MAX_UNIQUE:
        return (f"repeated buy sizes {sorted(uniq)} across {len(sizes)} buys "
                f"(spam/farmer pattern)")
    return None


FILTERS = [
    ("exit_activity", _reject_exit_activity),
    ("trade_size",    _reject_trade_size),
    ("mixed_quote",   _reject_mixed_quote),
    ("min_closed",    _reject_min_closed),
    ("hold_time",     _reject_hold_time),
    ("win_rate",      _reject_win_rate),
    ("repeated_buys", _reject_repeated_buys),
]


def apply_filters(w):
    for name, fn in FILTERS:
        reason = fn(w)
        if reason:
            return (False, name, reason)
    return (True, None, None)


def filter_watchlist(wallets):
    kept, rejected = [], []
    for w in wallets:
        ok, filt, reason = apply_filters(w)
        if ok:
            kept.append(w)
        else:
            rejected.append({
                "address": w.get("address"),
                "filter":  filt,
                "reason":  reason,
            })
    kept.sort(
        key=lambda x: (
            -(float(x.get("win_rate") or 0.0)),
            (float(x.get("volume_usd_7d") or 0.0) /
             max(1, int(x.get("trades_7d") or 1))),
        )
    )
    return kept[:WATCHLIST_SIZE], kept[WATCHLIST_SIZE:], rejected


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def _print_report(before, kept, capped_out, rejected):
    print("\n=== PHASE 7 — WATCHLIST FILTER PASS ===\n")
    print(f"Input wallets (before):   {before}")
    print(f"Passed all filters:       {len(kept) + len(capped_out)}")
    print(f"Rejected:                 {len(rejected)}")
    print(f"Capped out (> size={WATCHLIST_SIZE}):   {len(capped_out)}")
    print(f"Final watchlist (after):  {len(kept)}\n")

    by_filter = {}
    for r in rejected:
        by_filter.setdefault(r["filter"], []).append(r)
    print("-- Rejections by filter --")
    for fname, _ in FILTERS:
        items = by_filter.get(fname, [])
        print(f"  {fname:<16} {len(items):>3}")
        for r in items:
            addr = str(r["address"])[:10] + "…"
            print(f"      - {addr:<13} {r['reason']}")
    print()

    print("-- Final watchlist (sorted by win_rate desc, then avg size asc) --")
    for i, w in enumerate(kept, 1):
        addr = str(w.get("address"))[:10] + "…"
        wr = w.get("win_rate")
        wr_s = f"{wr:.0%}" if wr is not None else "—"
        trades = int(w.get("trades_7d") or 0)
        vol = float(w.get("volume_usd_7d") or 0.0)
        avg = (vol / trades) if trades else 0.0
        closed = int(w.get("closed_trades_7d") or 0)
        hold = w.get("avg_hold_time_hours")
        hold_s = f"{float(hold):>4.1f}h" if hold is not None else "  — "
        print(f"  {i:>2}. {addr:<13} wr={wr_s:>4}  "
              f"trades7d={trades:>3}  closed={closed:>2}  "
              f"avg=${avg:>8,.2f}  hold={hold_s}")
    if capped_out:
        print(f"\n  (+ {len(capped_out)} additional passing wallets capped out)")


# --------------------------------------------------------------------------
# Synthetic demo seed (used when no seed file is provided)
# --------------------------------------------------------------------------
def _demo_seed():
    seed = []

    # 22 valid wallets — will be capped at 20 in output
    for i in range(22):
        seed.append({
            "address": f"Pass{i:02d}Wallet111111111111111111111111111111",
            "trades_7d": 30 + i,
            "buy_count": 18 + i // 2,
            "sell_count": 12 + i // 2,
            "closed_trades_7d": 8 + i // 3,
            "volume_usd_7d": 3000.0 + i * 120,
            "win_rate": 0.56 + (i % 7) * 0.02,
            "avg_hold_time_hours": 2.0 + (i % 5),
            "quote_currencies_per_mint": {f"M{i}a": ["SOL"], f"M{i}b": ["SOL"]},
            "recent_buy_sizes_usd": [80 + i, 120 - i, 95 + i, 110 - i, 70 + i, 130 - i],
        })

    # 4 farmers (mostly buys, fails F1)
    for i in range(4):
        seed.append({
            "address": f"Farmer{i}Wallet1111111111111111111111111111111",
            "trades_7d": 40, "buy_count": 38, "sell_count": 2, "closed_trades_7d": 2,
            "volume_usd_7d": 2000.0, "win_rate": 0.70, "avg_hold_time_hours": 4.0,
            "quote_currencies_per_mint": {"Mf": ["SOL"]},
            "recent_buy_sizes_usd": [50, 60, 55, 52, 58, 54],
        })

    # 2 micro traders (fails F2 low)
    for i in range(2):
        seed.append({
            "address": f"Micro{i}Wallet111111111111111111111111111111",
            "trades_7d": 200, "buy_count": 120, "sell_count": 80, "closed_trades_7d": 60,
            "volume_usd_7d": 800.0, "win_rate": 0.60, "avg_hold_time_hours": 1.0,
            "quote_currencies_per_mint": {"Mm": ["SOL"]},
            "recent_buy_sizes_usd": [4, 5, 4, 3, 5, 4],
        })

    # 1 whale (fails F2 high)
    seed.append({
        "address": "WhaleWallet11111111111111111111111111111111",
        "trades_7d": 10, "buy_count": 6, "sell_count": 4, "closed_trades_7d": 4,
        "volume_usd_7d": 120000.0, "win_rate": 0.65, "avg_hold_time_hours": 3.0,
        "quote_currencies_per_mint": {"Mw": ["SOL"]},
        "recent_buy_sizes_usd": [12000, 15000, 11000, 14000, 13000],
    })

    # 2 mixed-quote (fails F3)
    for i in range(2):
        seed.append({
            "address": f"Mixed{i}Wallet111111111111111111111111111111",
            "trades_7d": 25, "buy_count": 15, "sell_count": 10, "closed_trades_7d": 8,
            "volume_usd_7d": 2500.0, "win_rate": 0.60, "avg_hold_time_hours": 5.0,
            "quote_currencies_per_mint": {"Mx": ["SOL", "USDC"]},
            "recent_buy_sizes_usd": [90, 110, 80, 120, 95, 100],
        })

    # 3 too few closed (fails F4)
    for i in range(3):
        seed.append({
            "address": f"Few{i}Wallet1111111111111111111111111111111",
            "trades_7d": 12, "buy_count": 8, "sell_count": 4, "closed_trades_7d": 3,
            "volume_usd_7d": 1200.0, "win_rate": 0.58, "avg_hold_time_hours": 4.0,
            "quote_currencies_per_mint": {"Mc": ["SOL"]},
            "recent_buy_sizes_usd": [100, 90, 110, 95, 105],
        })

    # 1 slow hold (fails F5)
    seed.append({
        "address": "SlowHoldWallet111111111111111111111111111",
        "trades_7d": 20, "buy_count": 12, "sell_count": 8, "closed_trades_7d": 7,
        "volume_usd_7d": 2000.0, "win_rate": 0.58, "avg_hold_time_hours": 72.0,
        "quote_currencies_per_mint": {"Ms": ["SOL"]},
        "recent_buy_sizes_usd": [100, 105, 95, 98, 102],
    })

    # 1 missing hold_time (fails F5)
    seed.append({
        "address": "NullHoldWallet11111111111111111111111111",
        "trades_7d": 20, "buy_count": 12, "sell_count": 8, "closed_trades_7d": 7,
        "volume_usd_7d": 2000.0, "win_rate": 0.60, "avg_hold_time_hours": None,
        "quote_currencies_per_mint": {"Mh": ["SOL"]},
        "recent_buy_sizes_usd": [100, 105, 95, 98, 102],
    })

    # 1 null win_rate (fails F6)
    seed.append({
        "address": "NullWrWallet1111111111111111111111111111",
        "trades_7d": 15, "buy_count": 9, "sell_count": 6, "closed_trades_7d": 5,
        "volume_usd_7d": 1500.0, "win_rate": None, "avg_hold_time_hours": 4.0,
        "quote_currencies_per_mint": {"Mn": ["SOL"]},
        "recent_buy_sizes_usd": [100, 110, 90, 105, 95],
    })

    # 1 low win_rate (fails F6)
    seed.append({
        "address": "LowWrWallet11111111111111111111111111111",
        "trades_7d": 18, "buy_count": 10, "sell_count": 8, "closed_trades_7d": 7,
        "volume_usd_7d": 1800.0, "win_rate": 0.40, "avg_hold_time_hours": 3.0,
        "quote_currencies_per_mint": {"Mlw": ["SOL"]},
        "recent_buy_sizes_usd": [100, 120, 80, 110, 95],
    })

    # 2 spam/repeat (fails F7)
    for i in range(2):
        seed.append({
            "address": f"Spam{i}Wallet11111111111111111111111111111",
            "trades_7d": 40, "buy_count": 30, "sell_count": 10, "closed_trades_7d": 10,
            "volume_usd_7d": 2000.0, "win_rate": 0.60, "avg_hold_time_hours": 2.0,
            "quote_currencies_per_mint": {"Mp": ["SOL"]},
            "recent_buy_sizes_usd": [50.0] * 15,
        })

    return seed


def main(argv):
    seed_path = Path(argv[1]) if len(argv) > 1 else None
    if seed_path and seed_path.exists():
        wallets = json.loads(seed_path.read_text())
        print(f"[discovery] loaded {len(wallets)} wallets from {seed_path}")
    else:
        wallets = _demo_seed()
        print(f"[discovery] no seed file — using {len(wallets)} synthetic demo wallets "
              f"(pass a seed JSON path as argv[1] to use real data)")

    before = len(wallets)
    kept, capped_out, rejected = filter_watchlist(wallets)
    _print_report(before, kept, capped_out, rejected)

    out_path = Path(__file__).resolve().parent / "watchlist.json"
    out_path.write_text(json.dumps(kept, indent=2))
    print(f"\n[discovery] wrote final watchlist → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
