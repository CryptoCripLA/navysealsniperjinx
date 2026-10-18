#!/usr/bin/env python3
"""Focused analysis on the most recent 25 closed trades.

Question: is the bot salvageable, or is the recent regime meaningfully
different from what the edge studies assumed?
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
IN = ROOT / "data" / "features.csv"

N_LAST = 25


def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def pf(pnl: pd.Series) -> float:
    gains = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def metrics(df: pd.DataFrame, label: str) -> dict:
    from constants import clopper_pearson_ci  # P4.5
    n = len(df)
    if n == 0:
        return {"slice": label, "n": 0}
    pnl = df["pnl_usd"].fillna(0)
    roi = df["roi_pct"].dropna()
    wins = int(df["win_label"].sum())
    wr_lo, wr_hi = clopper_pearson_ci(wins, n)
    return {
        "slice": label,
        "n": n,
        "avg_roi_pct": round(float(roi.mean()), 2),
        "median_roi": round(float(roi.median()), 2),
        "win_rate": round(float(df["win_label"].mean()), 3),
        "wr_ci95":   f"[{wr_lo*100:.1f}%,{wr_hi*100:.1f}%]",
        "rug_rate": round(float(df["rug_label"].mean()), 3),
        "pnl_usd": round(float(pnl.sum()), 3),
        "pf": round(pf(pnl), 3) if pf(pnl) != float("inf") else "inf",
        "best_roi": round(float(roi.max()), 2) if len(roi) else None,
        "worst_roi": round(float(roi.min()), 2) if len(roi) else None,
    }


def main():
    df = pd.read_csv(IN)
    df = df.dropna(subset=["close_ts"]).sort_values("close_ts").reset_index(drop=True)

    print("=" * 78)
    print(f"LAST-{N_LAST} TRADE ANALYSIS")
    print("=" * 78)
    print(f"Total closed trades: {len(df)}")

    if df.empty:
        print("No closed trades in feed — nothing to analyze.")
        return

    last = df.tail(N_LAST).reset_index(drop=True)
    prior = df.iloc[:-N_LAST]

    print(f"Window: {fmt_ts(last['close_ts'].iloc[0])} UTC → {fmt_ts(last['close_ts'].iloc[-1])} UTC")
    span_hrs = (last['close_ts'].iloc[-1] - last['close_ts'].iloc[0]) / 3600
    print(f"Time span: {span_hrs:.1f} hours ({span_hrs/24:.1f} days)")

    # ── Summary comparison ────────────────────────────────────────
    print("\n── Recent vs. earlier ────────────────────────────────")
    rows = [
        metrics(df, "ALL TRADES"),
        metrics(prior, f"PRIOR (first {len(prior)})"),
        metrics(last, f"LAST {N_LAST}"),
    ]
    keys = ["slice", "n", "avg_roi_pct", "median_roi", "win_rate", "wr_ci95",
            "rug_rate", "pnl_usd", "pf", "best_roi", "worst_roi"]
    w = {"slice": 24, "n": 5, "avg_roi_pct": 10, "median_roi": 9,
         "win_rate": 8, "wr_ci95": 17,
         "rug_rate": 8, "pnl_usd": 9, "pf": 7,
         "best_roi": 9, "worst_roi": 10}
    header = "  ".join(k.ljust(w[k]) for k in keys)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r.get(k, "")).ljust(w[k]) for k in keys))

    # ── Close-reason mix ──────────────────────────────────────────
    print(f"\n── Close reasons (LAST {N_LAST}) ────────────────────────")
    cr = last["close_reason"].value_counts()
    for reason, n in cr.items():
        share = n / len(last)
        avg = last.loc[last["close_reason"] == reason, "roi_pct"].mean()
        print(f"  {str(reason)[:32]:<32} n={n:<3} share={share:.0%}  avg_roi={avg:+.2f}%")

    # ── Delay bucket on last 25 ───────────────────────────────────
    print(f"\n── Delay bucket mix (LAST {N_LAST}) ─────────────────────")
    if last["delay_s"].notna().any():
        db = last.groupby("delay_bucket").agg(
            n=("roi_pct", "size"),
            avg_roi=("roi_pct", "mean"),
            rug=("rug_label", "mean"),
        )
        print(db.round(3).to_string())
    else:
        print("  (no delay_s populated in recent trades)")

    # ── Trade-by-trade dump (readable) ────────────────────────────
    print(f"\n── Trade detail (LAST {N_LAST}, oldest → newest) ────────")
    cols = ["close_ts", "mint", "delay_s", "hold_time_s", "roi_pct",
            "pnl_usd", "mfe_pct", "close_reason"]
    disp = last[cols].copy()
    disp["close_ts"] = disp["close_ts"].apply(fmt_ts)
    disp["mint"] = disp["mint"].astype(str).str.slice(0, 8) + "…"
    for col, dec in [("delay_s", 1), ("hold_time_s", 1),
                     ("roi_pct", 2), ("pnl_usd", 3), ("mfe_pct", 2)]:
        disp[col] = disp[col].round(dec)
    print(disp.to_string(index=False, max_colwidth=34))

    # ── Rug & catastrophe breakdown ───────────────────────────────
    print(f"\n── Catastrophic loss analysis (LAST {N_LAST}) ───────────")
    cats = last[last["roi_pct"] <= -50]
    print(f"  trades with ROI ≤ -50%: {len(cats)} / {len(last)} ({len(cats)/len(last):.0%})")
    print(f"  rugs (our label):        {int(last['rug_label'].sum())} / {len(last)}")
    if len(cats) > 0:
        print(f"  total $ lost on cats:    ${cats['pnl_usd'].sum():.2f}")
        print(f"  as share of net PnL:     {abs(cats['pnl_usd'].sum() / last['pnl_usd'].sum()) if last['pnl_usd'].sum() != 0 else 0:.0%}")

    # ── Without-catastrophe view ──────────────────────────────────
    print(f"\n── LAST {N_LAST} with catastrophes removed ────────────")
    clean = last[last["roi_pct"] > -50]
    print(metrics(clean, f"clean (n={len(clean)})"))

    # ── Sweet-spot delay slice in recent window ───────────────────
    print(f"\n── 32-36s delay slice (LAST {N_LAST}) ──────────────────")
    sweet = last[last["delay_bucket"] == "32_36"]
    if len(sweet) >= 3:
        print(metrics(sweet, "delay 32-36s recent"))
    else:
        print(f"  only {len(sweet)} trades in 32-36s — too few for signal")

    # ── MFE presence check ────────────────────────────────────────
    print(f"\n── MFE distribution (LAST {N_LAST}) ─────────────────────")
    mfe = last["mfe_pct"].fillna(0)
    print(f"  saw MFE ≥ 15%: {int((mfe >= 15).sum())} / {len(last)}")
    print(f"  saw MFE ≥ 30%: {int((mfe >= 30).sum())} / {len(last)}")
    print(f"  median MFE:    {mfe.median():.2f}%")
    print(f"  good_entry_bad_exit (MFE≥30 & ROI<5): {int(last['good_entry_bad_exit'].sum())}")


if __name__ == "__main__":
    main()
