"""
Characterize the 31 catastrophic trades (ROI ≤ -90%) using OHLCV we already
cached. Compare against 55 winners. Decide which rug-defense is viable.

Signals computed per trade from candles:
  - minutes_to_50pct_drop      time until price first hit ≤0.5× entry
  - minutes_to_90pct_drop      time until ≤0.1× entry (terminal)
  - max_drop_in_one_bar        sharpest single-candle drop after entry
  - volume_drop_ratio          post-entry volume vs pre-entry volume
  - pre_entry_ramp_x           entry price ÷ earliest-captured low
  - bars_to_first_down_bar     bars of grace period before first red bar
  - close_reason from history.json

We then look for statistical separation between rugs and winners on these
pre-entry / early-entry features that could plausibly gate a filter.
"""
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "history.json"
LIFECYCLE_DIR = ROOT / "edge_study" / "lifecycle"
REPORT = ROOT / "edge_study" / "catastrophic_report.md"


def idx_at_or_after(candles: List[Dict], ts: int) -> int:
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid]["unixTime"] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def features_for_trade(trade: Dict, candles: List[Dict]) -> Optional[Dict]:
    candles = sorted(candles, key=lambda c: c["unixTime"])
    if not candles:
        return None
    entry_ts = trade["entry_ts"]
    ei = idx_at_or_after(candles, entry_ts)
    if ei >= len(candles):
        return None
    entry_bar = candles[ei]
    entry_px = entry_bar["c"]
    if entry_px <= 0:
        return None

    pre = candles[:ei]
    post = candles[ei:]

    # earliest low in captured window (proxy for migration low — imperfect)
    earliest_low = min(c["l"] for c in candles[:10]) if len(candles) >= 1 else entry_px
    pre_entry_ramp_x = entry_px / earliest_low if earliest_low > 0 else None

    pre_vol = statistics.mean([c.get("v", 0) or 0 for c in pre[-5:]]) if pre else 0
    post_vol = statistics.mean([c.get("v", 0) or 0 for c in post[:5]]) if post else 0
    vol_drop_ratio = (post_vol / pre_vol) if pre_vol > 0 else None

    # time to -50%, -90%
    minutes_to_50 = None
    minutes_to_90 = None
    for j, c in enumerate(post[1:], 1):
        gap_s = c["unixTime"] - entry_bar["unixTime"]
        if minutes_to_50 is None and c["l"] <= entry_px * 0.5:
            minutes_to_50 = gap_s / 60
        if minutes_to_90 is None and c["l"] <= entry_px * 0.1:
            minutes_to_90 = gap_s / 60
            break

    # sharpest 1-bar drop (% from bar open to bar low, any post-entry bar)
    max_drop_one_bar = 0.0
    for c in post[1:30]:
        if c["o"] > 0:
            drop = (c["l"] / c["o"] - 1) * 100
            max_drop_one_bar = min(max_drop_one_bar, drop)

    # bars of grace before first down-bar (close < open)
    bars_grace = 0
    for c in post[1:30]:
        if c["c"] < c["o"]:
            break
        bars_grace += 1

    # how big is first-post-entry bar range (volatility proxy)
    first_bar_range_pct = None
    if len(post) >= 2:
        b = post[1]
        if b["o"] > 0:
            first_bar_range_pct = (b["h"] - b["l"]) / b["o"] * 100

    return {
        "mint": trade["mint"],
        "our_roi_pct": trade.get("roi_pct"),
        "close_reason": trade.get("close_reason"),
        "pre_entry_ramp_x": pre_entry_ramp_x,
        "pre_vol_usd": pre_vol,
        "post_vol_usd": post_vol,
        "vol_drop_ratio": vol_drop_ratio,
        "minutes_to_50_drop": minutes_to_50,
        "minutes_to_90_drop": minutes_to_90,
        "max_drop_one_bar_pct": max_drop_one_bar,
        "bars_grace_before_red": bars_grace,
        "first_bar_range_pct": first_bar_range_pct,
    }


def group_stats(records: List[Dict]) -> Dict:
    def pct(key, q):
        vals = sorted([r[key] for r in records if r.get(key) is not None])
        if not vals:
            return None
        i = int(q * (len(vals) - 1))
        return vals[i]

    def med(key):
        return pct(key, 0.5)

    return {
        "n": len(records),
        "pre_entry_ramp_median": med("pre_entry_ramp_x"),
        "pre_entry_ramp_p25": pct("pre_entry_ramp_x", 0.25),
        "pre_entry_ramp_p75": pct("pre_entry_ramp_x", 0.75),
        "pre_vol_median_usd": med("pre_vol_usd"),
        "post_vol_median_usd": med("post_vol_usd"),
        "vol_drop_ratio_median": med("vol_drop_ratio"),
        "minutes_to_50_drop_median": med("minutes_to_50_drop"),
        "minutes_to_90_drop_median": med("minutes_to_90_drop"),
        "max_drop_one_bar_median": med("max_drop_one_bar_pct"),
        "bars_grace_median": med("bars_grace_before_red"),
        "first_bar_range_median_pct": med("first_bar_range_pct"),
        "close_reasons": Counter(r.get("close_reason") for r in records).most_common(),
    }


def hit_rate(records: List[Dict], key: str, threshold: float, direction: str) -> float:
    """Pct of records where feature satisfies threshold."""
    if not records:
        return 0.0
    hits = 0
    for r in records:
        v = r.get(key)
        if v is None:
            continue
        if direction == "ge" and v >= threshold:
            hits += 1
        elif direction == "le" and v <= threshold:
            hits += 1
    return 100 * hits / len(records)


def main() -> None:
    history = json.loads(HISTORY.read_text())
    closes = [e for e in history if e.get("type") == "trade_close"]
    catastrophic = [e for e in closes if (e.get("roi_pct") or 0) <= -90]
    winners = [e for e in closes if (e.get("roi_pct") or 0) > 20]

    def collect(trades):
        out = []
        for t in trades:
            f = LIFECYCLE_DIR / f"{t['mint']}.json"
            if not f.exists():
                continue
            d = json.loads(f.read_text())
            feats = features_for_trade(t, d.get("candles") or [])
            if feats:
                out.append(feats)
        return out

    cat_records = collect(catastrophic)
    win_records = collect(winners)

    cat = group_stats(cat_records)
    win = group_stats(win_records)

    # Filter rule hit rates
    rules = [
        ("pre_entry_ramp_x ≥ 3",      "pre_entry_ramp_x", 3.0, "ge"),
        ("pre_entry_ramp_x ≥ 5",      "pre_entry_ramp_x", 5.0, "ge"),
        ("pre_entry_ramp_x ≥ 10",     "pre_entry_ramp_x", 10.0, "ge"),
        ("first_bar_range ≥ 50%",     "first_bar_range_pct", 50.0, "ge"),
        ("first_bar_range ≥ 100%",    "first_bar_range_pct", 100.0, "ge"),
        ("vol_drop_ratio ≤ 0.1",      "vol_drop_ratio", 0.1, "le"),
        ("vol_drop_ratio ≤ 0.05",     "vol_drop_ratio", 0.05, "le"),
        ("post_vol ≤ $50k",           "post_vol_usd", 50_000, "le"),
    ]

    # close_reason tally for catastrophic
    cat_reasons = cat["close_reasons"]

    lines = []
    ap = lines.append
    ap("# Catastrophic-Trade Characterization (OHLCV-only)")
    ap(f"_Comparing {len(cat_records)} catastrophic trades (ROI ≤-90%) vs {len(win_records)} winners (ROI >+20%). All features derived from cached 1m OHLCV._")
    ap("")
    ap("## Close reason breakdown — catastrophic group")
    ap("")
    ap("| close_reason | count |\n|---|---:|")
    for r, n in cat_reasons:
        ap(f"| {r} | {n} |")
    ap("")

    ap("## Feature medians (catastrophic vs winners)")
    ap("")
    ap("| feature | catastrophic | winners | separation? |\n|---|---:|---:|---|")
    def row(name, a, b, good_gap=None):
        if a is None or b is None:
            return f"| {name} | {a} | {b} | — |"
        if good_gap is not None and abs(a - b) >= good_gap:
            verdict = "**yes**"
        elif a == b:
            verdict = "no"
        else:
            verdict = "weak"
        fmt = lambda v: f"{v:,.2f}" if isinstance(v, float) else str(v)
        return f"| {name} | {fmt(a)} | {fmt(b)} | {verdict} |"

    ap(row("pre_entry_ramp_x (entry÷earliest low)", cat["pre_entry_ramp_median"], win["pre_entry_ramp_median"], good_gap=2.0))
    ap(row("pre_vol_usd (last 5 pre-entry bars, avg)", cat["pre_vol_median_usd"], win["pre_vol_median_usd"], good_gap=10000))
    ap(row("post_vol_usd (first 5 post-entry bars, avg)", cat["post_vol_median_usd"], win["post_vol_median_usd"], good_gap=10000))
    ap(row("vol_drop_ratio (post÷pre)", cat["vol_drop_ratio_median"], win["vol_drop_ratio_median"], good_gap=0.3))
    ap(row("minutes_to_50%_drop", cat["minutes_to_50_drop_median"], win["minutes_to_50_drop_median"]))
    ap(row("minutes_to_90%_drop", cat["minutes_to_90_drop_median"], win["minutes_to_90_drop_median"]))
    ap(row("max_drop_one_bar_pct", cat["max_drop_one_bar_median"], win["max_drop_one_bar_median"], good_gap=20))
    ap(row("bars_grace_before_red", cat["bars_grace_median"], win["bars_grace_median"]))
    ap(row("first_bar_range_pct", cat["first_bar_range_median_pct"], win["first_bar_range_median_pct"], good_gap=25))
    ap("")

    ap("## Candidate filter hit rates")
    ap("")
    ap("`catches` = % of catastrophic trades the rule would reject.  `false positives` = % of winners it would also reject. A useful filter has **catches >> false positives**.")
    ap("")
    ap("| rule | catches catastrophic | rejects winners | net value |\n|---|---:|---:|---|")
    for name, key, th, direction in rules:
        ccat = hit_rate(cat_records, key, th, direction)
        cwin = hit_rate(win_records, key, th, direction)
        net = ccat - cwin
        verdict = "**useful**" if net >= 20 else ("worth trying" if net >= 10 else "no edge")
        ap(f"| {name} | {ccat:.0f}% | {cwin:.0f}% | {net:+.0f}pp — {verdict} |")
    ap("")

    # Post-entry rug detector feasibility: how fast do rugs drop?
    drop_5min = sum(1 for r in cat_records if r.get("minutes_to_50_drop") is not None and r["minutes_to_50_drop"] <= 5)
    drop_1min = sum(1 for r in cat_records if r.get("minutes_to_50_drop") is not None and r["minutes_to_50_drop"] <= 1)
    win_drop_5 = sum(1 for r in win_records if r.get("minutes_to_50_drop") is not None and r["minutes_to_50_drop"] <= 5)
    ap("## Post-entry rug detector — feasibility")
    ap("")
    ap(f"- Catastrophic trades hitting -50% within **1 minute**: {drop_1min}/{len(cat_records)} ({100*drop_1min/max(1,len(cat_records)):.0f}%)")
    ap(f"- Catastrophic trades hitting -50% within **5 minutes**: {drop_5min}/{len(cat_records)} ({100*drop_5min/max(1,len(cat_records)):.0f}%)")
    ap(f"- Winners that briefly hit -50% (false positives): {win_drop_5}/{len(win_records)}")
    ap("")

    # Verdict
    ap("## Verdict")
    ap("")
    best_rule = None
    best_net = -999
    for name, key, th, direction in rules:
        ccat = hit_rate(cat_records, key, th, direction)
        cwin = hit_rate(win_records, key, th, direction)
        if ccat - cwin > best_net:
            best_net = ccat - cwin
            best_rule = (name, ccat, cwin)

    if best_rule and best_net >= 30:
        ap(f"**Pre-trade filter viable**: `{best_rule[0]}` catches {best_rule[1]:.0f}% of rugs while only rejecting {best_rule[2]:.0f}% of winners (+{best_net:.0f}pp edge).")
    elif drop_5min / max(1, len(cat_records)) >= 0.6 and win_drop_5 / max(1, len(win_records)) < 0.1:
        ap(f"**Post-entry rug detector viable**: {100*drop_5min/max(1,len(cat_records)):.0f}% of rugs drop ≥50% within 5 min; only {100*win_drop_5/max(1,len(win_records)):.0f}% of winners briefly dip that far. A -50% SL within 5 min catches rugs cleanly.")
    else:
        ap("**No clean OHLCV-only signature.** Rugs and winners look similar in the pre-entry and early-post-entry candle features we can measure. Either the signal lives in data Birdeye free tier won't give us (dev wallet / security flags / mempool), or rugs are too fast to filter from candle data alone.")
    ap("")

    REPORT.write_text("\n".join(lines))
    print(f"wrote {REPORT}")


if __name__ == "__main__":
    main()
