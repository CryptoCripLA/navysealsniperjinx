"""Backtest stricter entry filters on the post-patch cohort (last 171 real trades).

Joins trade_close events with the preceding signal_accept (same mint, closest earlier ts)
to recover entry_score components, then reports per-threshold and per-combo stats.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "history.json"


def load_joined():
    h = json.loads(HIST.read_text())
    closes = [e for e in h if e.get("type") == "trade_close"]
    closes.sort(key=lambda e: e.get("exit_ts") or e.get("ts") or 0)

    # index signal_accepts by mint → list of (ts, score_dict, delay_s) sorted asc
    accepts = {}
    for e in h:
        if e.get("type") == "signal_accept" and isinstance(e.get("entry_score"), dict):
            accepts.setdefault(e["mint"], []).append(
                (e.get("ts") or 0, e["entry_score"], e.get("delay_s"))
            )
    for m in accepts:
        accepts[m].sort(key=lambda x: x[0])

    joined = []
    for c in closes:
        m = c.get("mint")
        ets = c.get("entry_ts") or 0
        cands = accepts.get(m) or []
        # pick latest signal_accept at or before entry_ts (within 5 min)
        best = None
        # widen to 30 min — delayed_entry path can space signal→entry by several minutes
        for ts, sc, ds in cands:
            if ts <= ets and ets - ts <= 1800:
                best = (ts, sc, ds)
        # if none found before entry_ts, accept closest within ±2 min
        if not best:
            for ts, sc, ds in cands:
                if abs(ts - ets) <= 120:
                    best = (ts, sc, ds)
                    break
        if best:
            joined.append({"close": c, "ts": best[0], "score": best[1], "delay_s": best[2]})
        else:
            joined.append({"close": c, "ts": None, "score": None, "delay_s": None})
    return joined, closes


def evaluate(subset, label):
    real = [
        j for j in subset
        if "GHOST" not in (j["close"].get("close_reason") or "")
    ]
    n = len(real)
    if n == 0:
        return {"label": label, "n": 0}
    wins = [j for j in real if (j["close"].get("pnl_usd") or 0) > 0]
    loss = [j for j in real if (j["close"].get("pnl_usd") or 0) < 0]
    gw = sum(j["close"].get("pnl_usd") or 0 for j in wins)
    gl = -sum(j["close"].get("pnl_usd") or 0 for j in loss)
    net = gw - gl
    staked = sum(j["close"].get("realized_basis_usd") or 0 for j in real)
    fees = sum(j["close"].get("fees_usd") or 0 for j in real)
    return {
        "label": label,
        "n": n,
        "win_rate_pct": 100 * len(wins) / n,
        "gw": gw,
        "gl": gl,
        "net": net,
        "pf": gw / gl if gl > 0 else float("inf"),
        "exp_per_trade": net / n,
        "exp_per_dollar": (net / staked * 100) if staked > 0 else 0,
        "fee_drag_pct": (fees / staked * 100) if staked > 0 else 0,
        "staked": staked,
    }


def main():
    joined, closes = load_joined()
    last200 = joined[-200:]

    # how many in the cohort have score data?
    with_score = [j for j in last200 if j["score"] is not None]
    print(f"cohort: {len(last200)} trades, {len(with_score)} have entry_score linked")

    # distribution
    from collections import Counter
    totals = Counter((j["score"].get("total") or 0) for j in with_score)
    print(f"score_total distribution: {dict(sorted(totals.items()))}")
    hp = Counter((j["score"].get("holder_pts") or 0) for j in with_score)
    sp = Counter((j["score"].get("social_pts") or 0) for j in with_score)
    print(f"holder_pts: {dict(sorted(hp.items()))}")
    print(f"social_pts: {dict(sorted(sp.items()))}")

    # Eligible baseline = post-patch, real (non-ghost), with score
    eligible = [
        j for j in with_score
        if "GHOST" not in (j["close"].get("close_reason") or "")
    ]
    print(f"eligible (non-ghost, with score): {len(eligible)}")

    rows = []

    def record(name, subset):
        st = evaluate(subset, name)
        if st["n"] == 0:
            rows.append({"label": name, "n": 0})
        else:
            rows.append(st)

    # Baseline (no filter beyond having a score)
    record("BASELINE (no filter)", eligible)

    # Score thresholds
    for t in [50, 55, 60, 65, 70, 75, 80]:
        sub = [j for j in eligible if (j["score"].get("total") or 0) >= t]
        record(f"score_total >= {t}", sub)

    # Combos
    combos = [
        (
            "score>=60 AND holder_pts>=10",
            lambda s, d: (s.get("total") or 0) >= 60 and (s.get("holder_pts") or 0) >= 10,
        ),
        (
            "score>=60 AND social_pts==10",
            lambda s, d: (s.get("total") or 0) >= 60 and (s.get("social_pts") or 0) == 10,
        ),
        (
            "score>=65 AND delay_s>=60",
            lambda s, d: (s.get("total") or 0) >= 65 and (d or 0) >= 60,
        ),
        (
            "score>=65 AND (holder_pts==20 OR social_pts==10)",
            lambda s, d: (s.get("total") or 0) >= 65 and (
                (s.get("holder_pts") or 0) == 20 or (s.get("social_pts") or 0) == 10
            ),
        ),
    ]
    for name, pred in combos:
        sub = [j for j in eligible if pred(j["score"], j["delay_s"])]
        record(name, sub)

    # Print table
    print()
    hdr = f"{'filter':45} {'n':>4} {'win%':>6} {'pf':>6} {'exp$':>8} {'exp%':>7} {'net$':>8} {'feedrag%':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        if r["n"] == 0:
            print(f"{r['label']:45} {r['n']:>4}  (no trades)")
            continue
        print(
            f"{r['label']:45} {r['n']:>4} {r['win_rate_pct']:>5.1f}% {r['pf']:>5.2f}x "
            f"{r['exp_per_trade']:>+7.3f} {r['exp_per_dollar']:>+6.2f}% "
            f"{r['net']:>+7.2f} {r['fee_drag_pct']:>7.2f}%"
        )

    # Best by expectancy/trade with n>=30
    valid = [r for r in rows if r.get("n", 0) >= 30]
    best = max(valid, key=lambda r: r["exp_per_trade"]) if valid else None
    if best:
        print()
        print(f"BEST (n>=30): {best['label']}")
        print(f"  n={best['n']}, exp/trade=${best['exp_per_trade']:+.3f}, "
              f"pf={best['pf']:.2f}x, net=${best['net']:+.2f}, win={best['win_rate_pct']:.1f}%")


if __name__ == "__main__":
    main()
