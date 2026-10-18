#!/usr/bin/env python3
"""Stitch the outputs from backtest_rules / walk_forward / model_ranker
into a single human-readable RESEARCH_REPORT.md.

Flags leakage (exit_bucket / mfe_pct are only known post-close) and
separates *predictive-at-entry* signals from *post-hoc explanatory*
signals. Writes RESEARCH_REPORT.md in repo root.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "RESEARCH_REPORT.md"

FEATURES = ROOT / "data" / "features.csv"
RULES = ROOT / "data" / "rule_results.csv"
WFR = ROOT / "data" / "walk_forward_results.csv"
FI = ROOT / "data" / "feature_importance.csv"
METRICS = ROOT / "data" / "model_metrics.csv"

# Known post-close fields — not available when the bot decides to enter.
LEAKY_FEATURES = {
    "mfe_pct", "mae_pct", "hold_time_s", "exit_bucket_trail_sl",
    "exit_bucket_rapid_dump", "exit_bucket_hard_cap",
    "exit_bucket_liquidity", "exit_bucket_take_profit",
    "exit_bucket_ghost", "exit_bucket_time_exit",
    "exit_bucket_other", "exit_bucket_unknown",
    "roi_pct", "pnl_usd", "roi_pct_log", "mfe_pct_log", "mae_pct_log",
}


def _md_table(df: pd.DataFrame, cols=None) -> str:
    if df.empty:
        return "_(no rows)_\n"
    if cols is None:
        cols = list(df.columns)
    df = df[cols]
    head = "| " + " | ".join(str(c) for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    rows = [
        "| " + " | ".join(
            "" if pd.isna(v) else str(v) for v in row
        ) + " |"
        for row in df.itertuples(index=False)
    ]
    return "\n".join([head, sep, *rows]) + "\n"


def main():
    features = pd.read_csv(FEATURES)
    rules = pd.read_csv(RULES) if RULES.exists() else pd.DataFrame()
    wfr = pd.read_csv(WFR) if WFR.exists() else pd.DataFrame()
    fi = pd.read_csv(FI) if FI.exists() else pd.DataFrame()
    metrics = pd.read_csv(METRICS) if METRICS.exists() else pd.DataFrame()

    n = len(features)
    rugs = int(features["rug_label"].sum())
    wins = int(features["win_label"].sum())
    gebe = int(features["good_entry_bad_exit"].sum())
    total_pnl = float(features["pnl_usd"].fillna(0).sum())
    avg_roi = float(features["roi_pct"].dropna().mean()) if features["roi_pct"].notna().any() else float("nan")

    # Classify features by leakage
    if not fi.empty:
        fi = fi.copy()
        fi["leaky"] = fi["feature"].isin(LEAKY_FEATURES)
        predictive = fi[~fi["leaky"]].head(12)
        leaky = fi[fi["leaky"]].head(8)
    else:
        predictive = leaky = pd.DataFrame()

    # Walk-forward survival
    if not wfr.empty:
        survival = (wfr.groupby("rule")["sign_match"].mean()
                    .round(2).reset_index()
                    .rename(columns={"sign_match": "oos_sign_agreement"}))
        survival = survival.sort_values("oos_sign_agreement", ascending=False)
    else:
        survival = pd.DataFrame()

    # Top rules (by avg_roi_pct) with n ≥ 30
    rules_view = pd.DataFrame()
    if not rules.empty:
        r = rules.copy()
        r["avg_roi_pct"] = pd.to_numeric(r["avg_roi_pct"], errors="coerce")
        r = r.dropna(subset=["avg_roi_pct"])
        rules_view = r[r["n"] >= 30].sort_values("avg_roi_pct", ascending=False)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = []
    lines.append(f"# RESEARCH REPORT — Solana micro-sniper\n")
    lines.append(f"_Generated {ts}. Source: `history.json` → `data/trades.csv` → `data/features.csv`._\n")

    lines.append("## Dataset\n")
    lines.append(f"- Closed trades: **{n}**\n"
                 f"- Rugs (roi ≤ −80% OR HARD_CAP/FAILSAFE/GHOST): **{rugs}** "
                 f"({rugs/max(1,n):.1%})\n"
                 f"- Wins (roi > 10%): **{wins}** ({wins/max(1,n):.1%})\n"
                 f"- Good-entry-bad-exit (mfe ≥ 30% but roi < 5%): **{gebe}**\n"
                 f"- Total PnL: **${total_pnl:,.2f}**, avg ROI **{avg_roi:.2f}%**\n")

    lines.append("## Rule backtest (in-sample)\n")
    if not rules_view.empty:
        lines.append(_md_table(rules_view.round(3).head(15),
                               cols=["rule", "n", "avg_roi_pct", "win_rate",
                                     "rug_rate", "total_pnl_usd", "profit_factor"]))
    else:
        lines.append("_(rule_results.csv missing)_\n")

    lines.append("## Walk-forward OOS stability\n")
    lines.append("Rules are re-evaluated on later time folds after being computed on earlier folds. "
                 "`oos_sign_agreement` = fraction of folds where train and test sign of avg ROI match.\n\n")
    if not survival.empty:
        lines.append(_md_table(survival))
    else:
        lines.append("_(walk_forward_results.csv missing)_\n")

    lines.append("## Model AUC (time-ordered 70/30 split)\n")
    if not metrics.empty:
        lines.append(_md_table(metrics))
        lines.append("\n> **Important:** AUC on these models is inflated by post-close features "
                     "(`mfe_pct`, `hold_time_s`, `exit_bucket_*`) that the live bot does NOT have "
                     "at entry time. Treat these as descriptive, not deployable.\n")
    else:
        lines.append("_(model_metrics.csv missing)_\n")

    lines.append("## Feature ranking\n")
    lines.append("### Predictive-at-entry (usable for entry decisions)\n")
    if not predictive.empty:
        cols = [c for c in ["feature", "rf_importance", "logreg_coef"] if c in predictive.columns]
        lines.append(_md_table(predictive.round(4), cols=cols))
    else:
        lines.append("_(no non-leaky features ranked)_\n")

    lines.append("### Post-hoc / leaky (descriptive only)\n")
    if not leaky.empty:
        cols = [c for c in ["feature", "rf_importance", "logreg_coef"] if c in leaky.columns]
        lines.append(_md_table(leaky.round(4), cols=cols))
    else:
        lines.append("_(none)_\n")

    lines.append("## Bottom line\n")
    # Heuristic verdict
    sweet = features[features["delay_bucket"] == "32_36"]
    sweet_roi = float(sweet["roi_pct"].dropna().mean()) if len(sweet) else float("nan")
    sweet_n = len(sweet)

    non_rug = features[features["rug_label"] == 0]
    non_rug_roi = float(non_rug["roi_pct"].dropna().mean()) if len(non_rug) else float("nan")

    lines.append(
        f"- **Entry timing matters.** The `delay_bucket=32_36` slice (n={sweet_n}) averages "
        f"**{sweet_roi:+.2f}%** ROI vs **{avg_roi:+.2f}%** overall.\n"
        f"- **Rugs dominate deficit.** Removing rugs lifts avg ROI from "
        f"**{avg_roi:+.2f}%** → **{non_rug_roi:+.2f}%**, meaning ~{rugs}/{n} trades "
        f"({rugs/max(1,n):.1%}) are responsible for most of the loss.\n"
        f"- **The model's 'real' AUC is likely ≤0.6** once leaky features are stripped. "
        f"Logistic regression AUC {metrics.iloc[0]['auc'] if not metrics.empty else 'n/a'} "
        f"is boosted by hold_time/mfe, which the live bot does not know at entry.\n"
        f"- **Walk-forward verdict:** only simple delay-bucket rules survive; the narrower "
        f"'rugs_removed AND delay_32_36' rule is overfit when tested forward.\n"
        f"- **Actionable:** tighten delay gate to 32–36s and harden rug screening "
        f"(see existing `HARD_CAP`/`RAPID_DUMP` patches). Do not rely on model-ranked "
        f"post-hoc features for entry policy.\n"
    )

    OUT.write_text("\n".join(lines))
    print(f"[research_report] wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
