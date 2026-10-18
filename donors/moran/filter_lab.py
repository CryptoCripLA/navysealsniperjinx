#!/usr/bin/env python3
"""Pre-entry filter-layer analysis on data/features.csv.

Only uses fields known at entry time:
  - delay_s (502/635 non-null)
  - detect_to_exec_ms (472/635 non-null)
  - execution_latency_ms (634/635 non-null)

Every other "allowed" feature in the spec (liquidity_usd, holder_pct_top5,
market_cap, volatility, candidate_age_ms) has 0–69 non-nulls and is
excluded. Using them would be noise.

Steps: evaluate each single filter, combine into stacks, walk-forward test.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
IN = ROOT / "data" / "features.csv"


def metrics(df: pd.DataFrame, label: str) -> dict:
    n = len(df)
    if n == 0:
        return {"rule": label, "n": 0}
    pnl = df["pnl_usd"].fillna(0)
    gains = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    pf = (gains / losses) if losses > 0 else (float("inf") if gains > 0 else 0.0)
    return {
        "rule": label,
        "n": n,
        "kept_pct": round(100 * n / 635, 1),
        "avg_roi": round(float(df["roi_pct"].dropna().mean()), 3),
        "win_rate": round(float(df["win_label"].mean()), 3),
        "rug_rate": round(float(df["rug_label"].mean()), 3),
        "pnl_usd": round(float(pnl.sum()), 3),
        "pf": round(pf, 3) if pf != float("inf") else "inf",
    }


def show(rows):
    w = {"rule": 44, "n": 5, "kept_pct": 9, "avg_roi": 9,
         "win_rate": 8, "rug_rate": 8, "pnl_usd": 9, "pf": 7}
    header = "  ".join(k.ljust(w[k]) for k in w)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(
            str(r.get(k, "")).ljust(w[k]) for k in w
        ))


def walk_forward(df: pd.DataFrame, mask_fn, label: str, k=4, min_n=15):
    d = df.dropna(subset=["close_ts"]).sort_values("close_ts").reset_index(drop=True)
    folds = np.array_split(np.arange(len(d)), k)
    out = []
    agree = 0
    checked = 0
    for i in range(1, k):
        tr = d.iloc[np.concatenate(folds[:i])]
        te = d.iloc[folds[i]]
        tr_sub = tr[mask_fn(tr)]
        te_sub = te[mask_fn(te)]
        tr_roi = tr_sub["roi_pct"].dropna().mean() if len(tr_sub) else None
        te_roi = te_sub["roi_pct"].dropna().mean() if len(te_sub) else None
        te_n = len(te_sub)
        tr_n = len(tr_sub)
        sign = None
        if te_n >= min_n and tr_roi is not None and te_roi is not None:
            sign = np.sign(tr_roi) == np.sign(te_roi)
            agree += int(sign)
            checked += 1
        out.append({
            "stack": label, "fold": i,
            "train_n": tr_n, "train_roi": round(tr_roi, 3) if tr_roi is not None else None,
            "test_n": te_n, "test_roi": round(te_roi, 3) if te_roi is not None else None,
            "sign_match": sign,
        })
    agreement = (agree / checked) if checked else 0.0
    return out, agreement


def main():
    df = pd.read_csv(IN)
    baseline = metrics(df, "BASELINE_ALL")

    # ---- Step 1: single-filter candidates (keep-rule form) ----
    candidates = []

    # F1: delay in 32-36s sweet spot (edge-study validated)
    candidates.append(("F1_delay_32_36",
                       lambda d: d["delay_s"].between(32.0, 36.0, inclusive="left")))
    # F2: delay at least 32s (exclude the 30-32 bucket that loses −13.5%)
    candidates.append(("F2_delay_gte_32",
                       lambda d: d["delay_s"] >= 32.0))
    # F3: detect-to-exec not slow (≥1500ms means stale quote at fill)
    candidates.append(("F3_detect_lt_1500",
                       lambda d: d["detect_to_exec_ms"] < 1500))
    # F4: exec latency under 3s (tail of execution_latency runs to 13s)
    candidates.append(("F4_exec_lat_lt_3000",
                       lambda d: d["execution_latency_ms"] < 3000))
    # F5: require delay known (drops 'unknown' bucket which was -heavy)
    candidates.append(("F5_delay_known",
                       lambda d: d["delay_s"].notna()))

    rows = [baseline]
    for name, fn in candidates:
        sub = df[fn(df).fillna(False)]
        rows.append(metrics(sub, name))
    print("\n== STEP 1 — single filters ==\n")
    show(rows)

    # Rejection gate per spec: keep only filters that
    #   (a) do not remove >70% of trades
    #   (b) improve rug rate OR avg ROI vs baseline
    print("\n== STEP 1 — rejection gate ==\n")
    b_roi = baseline["avg_roi"]
    b_rug = baseline["rug_rate"]
    kept = []
    for r in rows[1:]:
        if r["kept_pct"] < 30:
            verdict = "REJECT (removes >70% of trades)"
        elif r["avg_roi"] > b_roi or r["rug_rate"] < b_rug:
            verdict = "KEEP"
            kept.append(r["rule"])
        else:
            verdict = "REJECT (no improvement on ROI or rug rate)"
        print(f"  {r['rule']:<24} {verdict}")
    print(f"\n  Kept filters: {kept}")

    # ---- Step 2: stack combinations ----
    stacks = [
        ("STACK_A_delay_32_36_only",
         lambda d: d["delay_s"].between(32.0, 36.0, inclusive="left").fillna(False)),
        ("STACK_B_delay_32_36_AND_not_slow",
         lambda d: (d["delay_s"].between(32.0, 36.0, inclusive="left").fillna(False))
                    & (d["detect_to_exec_ms"].fillna(99999) < 1500)),
        ("STACK_C_delay_gte_32_AND_exec_lat_lt_3000",
         lambda d: (d["delay_s"].fillna(0) >= 32.0)
                    & (d["execution_latency_ms"].fillna(99999) < 3000)),
        ("STACK_D_delay_32_36_AND_exec_lat_lt_3000",
         lambda d: (d["delay_s"].between(32.0, 36.0, inclusive="left").fillna(False))
                    & (d["execution_latency_ms"].fillna(99999) < 3000)),
    ]
    rows = [baseline]
    for name, fn in stacks:
        sub = df[fn(df)]
        rows.append(metrics(sub, name))
    print("\n== STEP 2 — candidate stacks ==\n")
    show(rows)

    # ---- Step 3: walk-forward on each stack ----
    print("\n== STEP 3 — walk-forward (4 folds, train=earlier, test=later) ==\n")
    wf_summary = []
    for name, fn in stacks:
        fold_rows, agreement = walk_forward(df, fn, name)
        wf_summary.append((name, agreement, fold_rows))
        print(f"\n  {name}   sign_agreement={agreement:.0%}")
        for r in fold_rows:
            print(f"    fold {r['fold']}  train_n={r['train_n']:<4} train_roi={r['train_roi']}"
                  f"  test_n={r['test_n']:<4} test_roi={r['test_roi']}  sign_match={r['sign_match']}")

    # ---- Step 3 gate ----
    print("\n== STEP 3 — gate (require ≥60% sign agreement) ==\n")
    survivors = []
    for name, agree, _ in wf_summary:
        ok = agree >= 0.60
        print(f"  {name:<48} {agree:.0%}  {'KEEP' if ok else 'REJECT'}")
        if ok:
            survivors.append(name)

    print(f"\n  Survivors: {survivors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
