#!/usr/bin/env python3
"""Rule-based filter experiments on data/features.csv.

Each rule is a boolean mask over closed trades; we report:
  n, avg_roi_pct, win_rate, rug_rate, total_pnl_usd, profit_factor

Rules are conservative slices over entry/exit metadata already in the CSV.
No model fitting, no parameter search — these are sanity checks, not
optimizations. Output: data/rule_results.csv + stdout table.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
IN = ROOT / "data" / "features.csv"
OUT = ROOT / "data" / "rule_results.csv"


def _stats(df: pd.DataFrame, label: str) -> dict:
    n = len(df)
    if n == 0:
        return {"rule": label, "n": 0, "avg_roi_pct": None,
                "win_rate": None, "rug_rate": None,
                "total_pnl_usd": 0.0, "profit_factor": None}
    roi = df["roi_pct"].dropna()
    pnl = df["pnl_usd"].fillna(0)
    gains = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    pf = (gains / losses) if losses > 0 else float("inf") if gains > 0 else 0.0
    return {
        "rule": label,
        "n": n,
        "avg_roi_pct": round(float(roi.mean()), 3) if len(roi) else None,
        "win_rate": round(float(df["win_label"].mean()), 3),
        "rug_rate": round(float(df["rug_label"].mean()), 3),
        "total_pnl_usd": round(float(pnl.sum()), 3),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
    }


def main():
    df = pd.read_csv(IN)
    rows = []

    # Baseline
    rows.append(_stats(df, "ALL_TRADES"))

    # Delay-based slices (edge-study hypothesis)
    rows.append(_stats(df[df["delay_bucket"] == "32_36"], "delay_32_36s"))
    rows.append(_stats(df[df["delay_bucket"] == "30_32"], "delay_30_32s"))
    rows.append(_stats(df[df["delay_bucket"].isin(["32_36", "36_45"])],
                       "delay_32_45s"))
    rows.append(_stats(df[df["delay_bucket"] == "gte_45"], "delay_gte_45s"))

    # Execution latency
    rows.append(_stats(df[df["fast_exec"] == 1], "fast_exec_lt_800ms"))
    rows.append(_stats(df[df["slow_exec"] == 0], "exec_lt_1500ms"))
    rows.append(_stats(df[df["slow_exec"] == 1], "slow_exec_gte_1500ms"))

    # Trade-size sanity
    rows.append(_stats(df[df["liq_bucket"] == "rung1"], "rung1_size"))

    # Exit-regime profile
    rows.append(_stats(df[df["exit_bucket"] == "trail_sl"], "exit_trail_sl"))
    rows.append(_stats(df[df["exit_bucket"] == "take_profit"], "exit_take_profit"))
    rows.append(_stats(df[df["exit_bucket"] == "hard_cap"], "exit_hard_cap"))
    rows.append(_stats(df[df["exit_bucket"] == "rapid_dump"], "exit_rapid_dump"))
    rows.append(_stats(df[df["exit_bucket"] == "liquidity"], "exit_liquidity_guard"))

    # Rug removal counterfactual
    rows.append(_stats(df[df["rug_label"] == 0], "rugs_removed"))
    rows.append(_stats(df[(df["rug_label"] == 0) & (df["delay_bucket"] == "32_36")],
                       "rugs_removed_AND_delay_32_36"))

    # Signal-stack slices (when delay known AND fast exec)
    mask = (df["has_delay"] == 1) & (df["fast_exec"] == 1)
    rows.append(_stats(df[mask], "has_delay_AND_fast_exec"))

    # Very conservative: known delay in sweet spot, not stale exec, non-rug regime a priori
    rows.append(_stats(
        df[(df["delay_bucket"] == "32_36") & (df["slow_exec"] == 0)],
        "delay_32_36_AND_not_slow",
    ))

    # MFE existed (trade had a peak) – sanity that trail rule is viable
    rows.append(_stats(df[df["mfe_pct"].fillna(0) >= 15], "had_mfe_gte_15"))
    rows.append(_stats(df[df["mfe_pct"].fillna(0) >= 30], "had_mfe_gte_30"))

    out = pd.DataFrame(rows)
    out.to_csv(OUT, index=False)

    # Pretty print
    print(f"[backtest_rules] {len(rows)} rules → {OUT}\n")
    w = {"rule": 34, "n": 5, "avg_roi_pct": 9, "win_rate": 7,
         "rug_rate": 7, "total_pnl_usd": 11, "profit_factor": 8}
    header = "  ".join(k.ljust(w[k]) for k in w)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(
            str(r[k]).ljust(w[k]) if r[k] is not None else "—".ljust(w[k])
            for k in w
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
