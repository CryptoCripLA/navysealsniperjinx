#!/usr/bin/env python3
"""Time-ordered walk-forward evaluation.

Split the dataset by close_ts into K ordered folds. For each fold, treat
the earlier folds as "train" and the current fold as "test". A rule is
considered surviving if its sign of avg_roi_pct agrees between train and
test, and its sample size on test is ≥ 20.

We test the rules that backtest_rules.py flagged as plausible. The goal
is to detect overfitting — if a rule only works in-sample, it fails
here.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
IN = ROOT / "data" / "features.csv"
OUT = ROOT / "data" / "walk_forward_results.csv"

K_FOLDS = 4
MIN_TEST_N = 20


RULES = {
    "delay_32_36s": lambda d: d["delay_bucket"] == "32_36",
    "delay_32_45s": lambda d: d["delay_bucket"].isin(["32_36", "36_45"]),
    "rugs_removed": lambda d: d["rug_label"] == 0,
    "rugs_removed_AND_delay_32_36":
        lambda d: (d["rug_label"] == 0) & (d["delay_bucket"] == "32_36"),
    "had_mfe_gte_15": lambda d: d["mfe_pct"].fillna(0) >= 15,
    "had_mfe_gte_30": lambda d: d["mfe_pct"].fillna(0) >= 30,
    "not_slow_exec": lambda d: d["slow_exec"] == 0,
    "delay_32_36_AND_not_slow":
        lambda d: (d["delay_bucket"] == "32_36") & (d["slow_exec"] == 0),
}


def _metrics(df: pd.DataFrame) -> dict:
    n = len(df)
    if n == 0:
        return {"n": 0, "avg_roi": None, "win_rate": None,
                "rug_rate": None, "pnl": 0.0}
    return {
        "n": n,
        "avg_roi": float(df["roi_pct"].dropna().mean()) if df["roi_pct"].notna().any() else None,
        "win_rate": float(df["win_label"].mean()),
        "rug_rate": float(df["rug_label"].mean()),
        "pnl": float(df["pnl_usd"].fillna(0).sum()),
    }


def main():
    df = pd.read_csv(IN)
    df = df.dropna(subset=["close_ts"]).sort_values("close_ts").reset_index(drop=True)
    if len(df) < K_FOLDS * 10:
        print(f"[walk_forward] only {len(df)} rows — too few for {K_FOLDS} folds")
        return 0

    fold_ids = np.array_split(np.arange(len(df)), K_FOLDS)
    print(f"[walk_forward] {len(df)} trades → {K_FOLDS} folds")
    for i, idx in enumerate(fold_ids):
        t0 = df.iloc[idx[0]]["close_ts"]
        t1 = df.iloc[idx[-1]]["close_ts"]
        print(f"  fold {i}: n={len(idx)} ts=[{t0:.0f}..{t1:.0f}]")

    out_rows = []
    for name, fn in RULES.items():
        for i in range(1, K_FOLDS):
            train_idx = np.concatenate(fold_ids[:i])
            test_idx = fold_ids[i]
            train = df.iloc[train_idx]
            test = df.iloc[test_idx]
            tr_sub = train[fn(train)]
            te_sub = test[fn(test)]
            tr_m = _metrics(tr_sub)
            te_m = _metrics(te_sub)
            survives = (
                te_m["n"] >= MIN_TEST_N
                and tr_m["avg_roi"] is not None
                and te_m["avg_roi"] is not None
                and np.sign(tr_m["avg_roi"]) == np.sign(te_m["avg_roi"])
            )
            out_rows.append({
                "rule": name,
                "fold": i,
                "train_n": tr_m["n"],
                "train_avg_roi": round(tr_m["avg_roi"], 3) if tr_m["avg_roi"] is not None else None,
                "train_win_rate": round(tr_m["win_rate"], 3) if tr_m["win_rate"] is not None else None,
                "test_n": te_m["n"],
                "test_avg_roi": round(te_m["avg_roi"], 3) if te_m["avg_roi"] is not None else None,
                "test_win_rate": round(te_m["win_rate"], 3) if te_m["win_rate"] is not None else None,
                "test_rug_rate": round(te_m["rug_rate"], 3) if te_m["rug_rate"] is not None else None,
                "test_pnl": round(te_m["pnl"], 3),
                "sign_match": survives,
            })

    out = pd.DataFrame(out_rows)
    out.to_csv(OUT, index=False)

    # Rule-level survival summary
    survival = out.groupby("rule")["sign_match"].mean().sort_values(ascending=False)
    print(f"\n[walk_forward] rule-level OOS sign agreement (higher = more stable):")
    for rule, frac in survival.items():
        print(f"  {rule:<34} {frac:.2%}")
    print(f"\n[walk_forward] wrote {len(out)} rows → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
