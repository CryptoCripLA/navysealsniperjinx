"""
Backtest grid: entry rules × exit rules over the 595 cached OHLCV files.

For each trade we simulate on the real post-detection candle stream:
  1. entry rule picks a candle (or skips)
  2. exit rule walks forward and picks a sell candle (or ladder of them)
  3. we log per-trade ROI (with slippage) and aggregate by strategy

Output: edge_study/lifecycle_backtest.md (ranked table).

Assumptions / caveats printed in the report header.
"""
import json
import statistics
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
LIFECYCLE_DIR = ROOT / "edge_study" / "lifecycle"
OUT = ROOT / "edge_study" / "lifecycle_backtest.md"

SLIPPAGE_PCT = 0.03          # round-trip (1.5% in, 1.5% out)
STAKE_USD = 1.0              # flat stake per trade
MAX_HOLD_S = 4 * 3600        # cap — we only cached 4h of post-exit candles


# ==================== helpers ====================

def idx_at_or_after(candles: List[Dict], ts: int) -> int:
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid]["unixTime"] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def range_pct(candles_slice: List[Dict]) -> float:
    if not candles_slice:
        return 1.0
    hi = max(c["h"] for c in candles_slice)
    lo = min(c["l"] for c in candles_slice)
    if lo <= 0:
        return 1.0
    return (hi - lo) / lo


# ==================== entry rules ====================
# Each returns an index into candles (the entry bar) or None to skip.
# `det_idx` is the candle containing the bot's original detection ts.

def entry_t_plus(n_min: int):
    def f(candles, det_idx):
        i = det_idx + n_min
        return i if 0 <= i < len(candles) else None
    f.__name__ = f"t+{n_min}m"
    return f


def entry_consolidation(min_bars: int, max_range: float, window_after_det: int):
    """Wait until the last `min_bars` have cumulative range < max_range, within window."""
    def f(candles, det_idx):
        start = det_idx + 1
        end = min(start + window_after_det, len(candles))
        for i in range(start + min_bars, end):
            slc = candles[i - min_bars:i]
            if range_pct(slc) < max_range:
                return i
        return None
    f.__name__ = f"consol_{min_bars}b_{int(max_range*100)}%_win{window_after_det}m"
    return f


def entry_pullback(ratio_max: float, lookback: int, window_after_det: int):
    """Enter when price ≤ ratio_max × min-of-last-lookback-candles."""
    def f(candles, det_idx):
        start = det_idx + lookback
        end = min(start + window_after_det, len(candles))
        for i in range(start, end):
            slc = candles[i - lookback:i]
            if not slc:
                continue
            mn = min(c["l"] for c in slc)
            if mn > 0 and candles[i]["c"] <= ratio_max * mn:
                return i
        return None
    f.__name__ = f"pullback_<={ratio_max:.1f}x_lb{lookback}_win{window_after_det}m"
    return f


def entry_volume_cooling(ratio: float, lookback: int, window_after_det: int):
    """Enter when current bar volume ≤ ratio × median of last `lookback` bars."""
    def f(candles, det_idx):
        start = det_idx + lookback
        end = min(start + window_after_det, len(candles))
        for i in range(start, end):
            slc = candles[i - lookback:i]
            vols = [c.get("v", 0) for c in slc if c.get("v")]
            if len(vols) < 2:
                continue
            med = statistics.median(vols)
            if med <= 0:
                continue
            if (candles[i].get("v", 0) or 0) <= ratio * med:
                return i
        return None
    f.__name__ = f"vol_cool_{ratio:.1f}x_lb{lookback}_win{window_after_det}m"
    return f


def entry_skip_if_pumped(max_x: float):
    """Skip if current T+30 price already > max_x × the earliest post-det low."""
    def f(candles, det_idx):
        i = det_idx + 30  # same 30s delay, but with filter
        if i >= len(candles):
            return None
        head = candles[det_idx:det_idx + 10]
        if not head:
            return None
        mn = min(c["l"] for c in head)
        if mn <= 0:
            return None
        if candles[i]["c"] / mn > max_x:
            return None
        return i
    f.__name__ = f"skip_pump>{max_x:.1f}x_t+30"
    return f


def entry_momentum_green(n_green: int):
    """Enter at first bar where last n_green bars are all green (close > open)."""
    def f(candles, det_idx):
        start = det_idx + n_green
        end = min(start + 30, len(candles))
        for i in range(start, end):
            slc = candles[i - n_green:i]
            if all(c["c"] > c["o"] for c in slc):
                return i
        return None
    f.__name__ = f"momentum_{n_green}green"
    return f


def entry_combo_patient(wait_m: int, min_bars: int, max_range: float):
    """Wait at least wait_m minutes, THEN require consolidation."""
    def f(candles, det_idx):
        start = det_idx + wait_m
        end = min(start + 30, len(candles))
        for i in range(start + min_bars, end):
            slc = candles[i - min_bars:i]
            if range_pct(slc) < max_range:
                return i
        return None
    f.__name__ = f"combo_wait{wait_m}m_then_consol{min_bars}b_{int(max_range*100)}%"
    return f


ENTRY_RULES = [
    entry_t_plus(0),
    entry_t_plus(1),
    entry_t_plus(2),
    entry_t_plus(5),
    entry_t_plus(10),
    entry_skip_if_pumped(2.0),
    entry_skip_if_pumped(3.0),
    entry_skip_if_pumped(5.0),
    entry_consolidation(3, 0.05, 15),
    entry_consolidation(3, 0.08, 20),
    entry_pullback(1.1, 3, 15),
    entry_pullback(0.8, 3, 20),
    entry_volume_cooling(0.5, 5, 15),
    entry_momentum_green(2),
    entry_combo_patient(2, 3, 0.05),
]


# ==================== exit rules ====================
# Each returns (exit_idx, exit_price) — or a weighted-avg price if ladder.
# Signature: (candles, entry_idx, entry_px) -> (exit_idx, avg_exit_px, legs)

def _walk(candles, entry_idx, entry_px, legs, time_cap_s=MAX_HOLD_S):
    """Walk forward. legs = list of (remaining_weight, trigger_fn).
    Each trigger_fn(bar, bars_since_entry, peak_px) returns price-or-None."""
    remaining = 1.0
    weighted_sum = 0.0
    peak = entry_px
    entry_ts = candles[entry_idx]["unixTime"]
    last_idx = entry_idx
    triggered = [False] * len(legs)
    for j in range(entry_idx + 1, len(candles)):
        bar = candles[j]
        peak = max(peak, bar["h"])
        if bar["unixTime"] - entry_ts > time_cap_s:
            break
        for k, (weight, trig) in enumerate(legs):
            if triggered[k]:
                continue
            px = trig(bar, j - entry_idx, peak)
            if px is not None:
                weighted_sum += weight * px
                remaining -= weight
                triggered[k] = True
                last_idx = j
                if remaining <= 1e-9:
                    return last_idx, weighted_sum, triggered
    # any remaining weight settles at final bar close
    if remaining > 0:
        settle_px = candles[min(last_idx, len(candles) - 1)]["c"]
        # find last bar within time cap
        for j in range(len(candles) - 1, entry_idx, -1):
            if candles[j]["unixTime"] - entry_ts <= time_cap_s:
                settle_px = candles[j]["c"]
                last_idx = j
                break
        weighted_sum += remaining * settle_px
    return last_idx, weighted_sum, triggered


def exit_flat(tp: float, sl: float):
    def f(candles, entry_idx, entry_px):
        def trig(bar, _n, _peak):
            if bar["h"] >= entry_px * (1 + tp):
                return entry_px * (1 + tp)
            if bar["l"] <= entry_px * (1 + sl):
                return entry_px * (1 + sl)
            return None
        return _walk(candles, entry_idx, entry_px, [(1.0, trig)])
    f.__name__ = f"tp{int(tp*100)}_sl{int(sl*100)}"
    return f


def exit_trail(sl_init: float, trail_pct: float, arm_at: float):
    """Arm trail at +arm_at%, then sell on peak-drop of trail_pct%."""
    def f(candles, entry_idx, entry_px):
        def trig(bar, _n, peak):
            if bar["l"] <= entry_px * (1 + sl_init) and peak < entry_px * (1 + arm_at):
                return entry_px * (1 + sl_init)
            if peak >= entry_px * (1 + arm_at) and bar["l"] <= peak * (1 - trail_pct):
                return peak * (1 - trail_pct)
            return None
        return _walk(candles, entry_idx, entry_px, [(1.0, trig)])
    f.__name__ = f"trail{int(trail_pct*100)}%_arm+{int(arm_at*100)}%_sl{int(sl_init*100)}"
    return f


def exit_time(max_s: int):
    def f(candles, entry_idx, entry_px):
        entry_ts = candles[entry_idx]["unixTime"]
        for j in range(entry_idx + 1, len(candles)):
            if candles[j]["unixTime"] - entry_ts >= max_s:
                return j, candles[j]["c"], [True]
        last = len(candles) - 1
        return last, candles[last]["c"], [True]
    f.__name__ = f"time_{max_s//60}m"
    return f


def exit_ladder(rungs: List[Tuple[float, float]]):
    """rungs = [(weight, tp_pct), ...]. Remainder held to time cap."""
    def f(candles, entry_idx, entry_px):
        legs = []
        for w, tp in rungs:
            target = entry_px * (1 + tp)
            def make(t):
                return lambda bar, _n, _peak: t if bar["h"] >= t else None
            legs.append((w, make(target)))
        return _walk(candles, entry_idx, entry_px, legs)
    label = "lad_" + "_".join(f"{int(w*100)}@+{int(tp*100)}" for w, tp in rungs)
    f.__name__ = label
    return f


def exit_ladder_with_trail(rungs: List[Tuple[float, float]], trail_pct: float, arm_at: float):
    """Take ladder profits; remainder trails after arm_at."""
    def f(candles, entry_idx, entry_px):
        legs = []
        for w, tp in rungs:
            target = entry_px * (1 + tp)
            def make(t):
                return lambda bar, _n, _peak: t if bar["h"] >= t else None
            legs.append((w, make(target)))
        remaining = 1.0 - sum(w for w, _ in rungs)
        if remaining > 1e-6:
            def trail_trig(bar, _n, peak):
                if peak >= entry_px * (1 + arm_at) and bar["l"] <= peak * (1 - trail_pct):
                    return peak * (1 - trail_pct)
                return None
            legs.append((remaining, trail_trig))
        return _walk(candles, entry_idx, entry_px, legs)
    label = "lad+trail_" + "_".join(f"{int(w*100)}@+{int(tp*100)}" for w, tp in rungs) + f"_trail{int(trail_pct*100)}%"
    f.__name__ = label
    return f


EXIT_RULES = [
    exit_flat(0.30, -0.15),
    exit_flat(0.50, -0.15),
    exit_flat(1.00, -0.15),
    exit_flat(0.50, -0.10),
    exit_flat(0.30, -0.08),
    exit_trail(-0.15, 0.20, 0.30),
    exit_trail(-0.10, 0.20, 0.30),
    exit_trail(-0.15, 0.30, 0.50),
    exit_trail(-0.10, 0.15, 0.20),
    exit_time(5 * 60),
    exit_time(15 * 60),
    exit_time(30 * 60),
    exit_time(60 * 60),
    exit_ladder([(0.5, 0.50), (0.5, 1.00)]),
    exit_ladder([(0.33, 0.30), (0.33, 0.80), (0.34, 2.00)]),
    exit_ladder_with_trail([(0.5, 0.50)], 0.20, 0.60),
    exit_ladder_with_trail([(0.33, 0.30), (0.33, 0.80)], 0.30, 1.00),
    exit_ladder_with_trail([(0.25, 0.25), (0.25, 0.50), (0.25, 1.00)], 0.30, 1.50),
]


# ==================== driver ====================

def load_mint_data():
    out = []
    for f in sorted(LIFECYCLE_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        candles = sorted(d.get("candles") or [], key=lambda c: c["unixTime"])
        if not candles:
            continue
        for t in d.get("trades", []):
            out.append({"mint": d["mint"], "candles": candles, "entry_ts": t["entry_ts"]})
    return out


def run() -> None:
    trades = load_mint_data()
    print(f"loaded {len(trades)} trades with candles")

    results: Dict[Tuple[str, str], Dict] = {}

    for entry_rule in ENTRY_RULES:
        for exit_rule in EXIT_RULES:
            key = (entry_rule.__name__, exit_rule.__name__)
            rois = []
            skipped = 0
            for t in trades:
                candles = t["candles"]
                det_idx = idx_at_or_after(candles, t["entry_ts"])
                if det_idx >= len(candles):
                    skipped += 1
                    continue
                ei = entry_rule(candles, det_idx)
                if ei is None or ei >= len(candles):
                    skipped += 1
                    continue
                entry_px = candles[ei]["c"]
                if entry_px <= 0:
                    skipped += 1
                    continue
                _xi, avg_exit_px, _legs = exit_rule(candles, ei, entry_px)
                if avg_exit_px <= 0:
                    skipped += 1
                    continue
                roi = (avg_exit_px / entry_px - 1) - SLIPPAGE_PCT
                rois.append(roi)
            results[key] = {
                "n_taken": len(rois),
                "n_skipped": skipped,
                "sum_pnl_usd": STAKE_USD * sum(rois),
                "avg_roi_pct": 100 * (sum(rois) / len(rois)) if rois else 0.0,
                "win_rate_pct": 100 * sum(1 for r in rois if r > 0) / len(rois) if rois else 0.0,
            }

    # write report
    total_trades = len(trades)
    lines = []
    ap = lines.append
    ap("# Lifecycle Backtest Grid")
    ap(f"_Sample: {total_trades} trades across cached mints. Stake: $1. Round-trip slippage: {SLIPPAGE_PCT*100:.1f}%. Hold cap: {MAX_HOLD_S//3600}h._")
    ap("")
    ap("## Caveats")
    ap("- Candle close = assumed fillable price; reality is worse in thin pools.")
    ap("- No_pool/rug exits treated at last captured price — may overstate recovery value.")
    ap("- Strategies that skip heavily (low `taken`) may be cherry-picking; evaluate P&L against volume.")
    ap(f"- Post-exit window cached = 4h; hold-beyond-4h strategies truncate at last candle.")
    ap("")

    # Top-20 by total P&L, min taken ≥ 30% of trades
    min_taken = int(total_trades * 0.3)
    ranked = sorted(
        [(k, v) for k, v in results.items() if v["n_taken"] >= min_taken],
        key=lambda kv: -kv[1]["sum_pnl_usd"],
    )

    ap(f"## Top 20 strategies (min taken ≥ {min_taken} / {total_trades})")
    ap("")
    ap("| rank | entry | exit | taken | skip% | total P&L | avg ROI | win% |")
    ap("|---:|---|---|---:|---:|---:|---:|---:|")
    for i, (key, v) in enumerate(ranked[:20], 1):
        skip_pct = 100 * v["n_skipped"] / total_trades
        ap(f"| {i} | {key[0]} | {key[1]} | {v['n_taken']} | {skip_pct:.0f}% | ${v['sum_pnl_usd']:+.2f} | {v['avg_roi_pct']:+.1f}% | {v['win_rate_pct']:.0f}% |")
    ap("")

    # Best exit per entry (diagnostic)
    ap("## Best exit for each entry rule")
    ap("")
    ap("| entry | best exit | taken | total P&L | avg ROI | win% |")
    ap("|---|---|---:|---:|---:|---:|")
    by_entry: Dict[str, List[Tuple[str, Dict]]] = {}
    for (e, x), v in results.items():
        by_entry.setdefault(e, []).append((x, v))
    for e in sorted(by_entry.keys()):
        best = max(by_entry[e], key=lambda kv: kv[1]["sum_pnl_usd"])
        x, v = best
        ap(f"| {e} | {x} | {v['n_taken']} | ${v['sum_pnl_usd']:+.2f} | {v['avg_roi_pct']:+.1f}% | {v['win_rate_pct']:.0f}% |")
    ap("")

    # Best entry per exit
    ap("## Best entry for each exit rule")
    ap("")
    ap("| exit | best entry | taken | total P&L | avg ROI | win% |")
    ap("|---|---|---:|---:|---:|---:|")
    by_exit: Dict[str, List[Tuple[str, Dict]]] = {}
    for (e, x), v in results.items():
        by_exit.setdefault(x, []).append((e, v))
    for x in sorted(by_exit.keys()):
        best = max(by_exit[x], key=lambda kv: kv[1]["sum_pnl_usd"])
        e, v = best
        ap(f"| {x} | {e} | {v['n_taken']} | ${v['sum_pnl_usd']:+.2f} | {v['avg_roi_pct']:+.1f}% | {v['win_rate_pct']:.0f}% |")
    ap("")

    # Current-bot baseline row
    ap("## Baseline (current bot behavior approximation)")
    ap("")
    baseline_key = ("t+0m", "trail20%_arm+30%_sl-15")
    # fallback to any entry close to t+0 + a trail
    for k in list(results.keys()):
        if k[0].startswith("t+0") and "trail" in k[1]:
            baseline_key = k
            break
    v = results.get(baseline_key)
    if v:
        ap(f"- `{baseline_key[0]} × {baseline_key[1]}`: taken {v['n_taken']}, P&L **${v['sum_pnl_usd']:+.2f}**, avg ROI {v['avg_roi_pct']:+.1f}%, win {v['win_rate_pct']:.0f}%")

    OUT.write_text("\n".join(lines))
    print(f"wrote {OUT}  — {len(results)} strategy combos")


if __name__ == "__main__":
    run()
