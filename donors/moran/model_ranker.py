#!/usr/bin/env python3
"""Train a small logistic-regression classifier on features → win_label.

Secondary: optional random forest for non-linear feature importance.
Metrics: AUC, accuracy, precision/recall on time-ordered holdout.
Output: data/feature_importance.csv + stdout summary.

Kept intentionally small — overfitting is the main risk with ~635 rows.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, precision_score, recall_score,
                              roc_auc_score)
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent
IN = ROOT / "data" / "features.csv"
OUT_FI = ROOT / "data" / "feature_importance.csv"
OUT_METRICS = ROOT / "data" / "model_metrics.csv"

# P4.3: split features by what is knowable *at entry decision time* vs
# what is only observable *after* the trade closes. Predictive models must
# use AT_ENTRY only — the POST_HOC list is kept for explanatory diagnostics
# (which observed behaviors correlate with outcome) but will trivially leak
# the label if used for prediction.
NUMERIC_AT_ENTRY = [
    "detect_to_exec_ms", "execution_latency_ms",
    "size_usd", "candidate_age_ms", "opportunity_age_ms",
    "entry_price",
    "slow_exec", "fast_exec", "has_delay", "has_entry_score",
]
NUMERIC_POST_HOC = [
    "hold_time_s", "mfe_pct", "mae_pct",
]
NUMERIC = NUMERIC_AT_ENTRY + NUMERIC_POST_HOC  # kept for back-compat in _prep

CATEGORICAL_AT_ENTRY = ["delay_bucket", "liq_bucket"]
CATEGORICAL_POST_HOC = ["exit_bucket"]   # close_reason — leaks the outcome
CATEGORICAL = CATEGORICAL_AT_ENTRY + CATEGORICAL_POST_HOC

TARGET = "win_label"  # P4.2 canonical: pnl_usd > 0


def _prep(df: pd.DataFrame, feature_mode: str = "all"):
    """feature_mode: 'at_entry' (predictive only) | 'all' (incl. post-hoc, leaky)."""
    if feature_mode == "at_entry":
        num_cols = NUMERIC_AT_ENTRY
        cat_cols = CATEGORICAL_AT_ENTRY
    else:
        num_cols = NUMERIC
        cat_cols = CATEGORICAL

    X_num = df[num_cols].copy()
    for c in num_cols:
        X_num[c] = pd.to_numeric(X_num[c], errors="coerce")
    X_num = X_num.fillna(X_num.median(numeric_only=True)).fillna(0)

    dummies = pd.get_dummies(df[cat_cols].fillna("unknown"), drop_first=False) if cat_cols else pd.DataFrame(index=df.index)
    X = pd.concat([X_num, dummies], axis=1)
    X = X.astype(float)
    y = df[TARGET].astype(int).values
    return X, y


def _time_split(df: pd.DataFrame, frac=0.7):
    df = df.dropna(subset=["close_ts"]).sort_values("close_ts").reset_index(drop=True)
    cut = int(len(df) * frac)
    return df.iloc[:cut], df.iloc[cut:]


def _run_model_suite(train_df, test_df, feature_mode: str, metrics_rows: list):
    """Train logreg + RF for one feature set. Returns (importance_df, auc_flags)."""
    X_tr, y_tr = _prep(train_df, feature_mode)
    X_te, y_te = _prep(test_df,  feature_mode)
    X_te = X_te.reindex(columns=X_tr.columns, fill_value=0)

    scaler = StandardScaler(with_mean=False)
    X_tr_s = scaler.fit_transform(X_tr)
    X_te_s = scaler.transform(X_te)
    try:
        lr = LogisticRegression(max_iter=2000, C=0.5, class_weight="balanced")
        lr.fit(X_tr_s, y_tr)
        proba = lr.predict_proba(X_te_s)[:, 1]
        pred = (proba >= 0.5).astype(int)
        auc = roc_auc_score(y_te, proba) if len(set(y_te)) > 1 else float("nan")
        metrics_rows.append({
            "model": f"logreg_{feature_mode}",
            "feature_mode": feature_mode,
            "train_n": len(X_tr), "test_n": len(X_te),
            "auc": round(float(auc), 3) if not np.isnan(auc) else None,
            "accuracy": round(float(accuracy_score(y_te, pred)), 3),
            "precision": round(float(precision_score(y_te, pred, zero_division=0)), 3),
            "recall": round(float(recall_score(y_te, pred, zero_division=0)), 3),
        })
        coef = pd.DataFrame({
            "feature": X_tr.columns,
            f"logreg_coef_{feature_mode}": lr.coef_[0],
        })
    except Exception as exc:
        print(f"[model_ranker] logreg({feature_mode}) failed: {exc}")
        coef = pd.DataFrame()

    rf = RandomForestClassifier(n_estimators=200, max_depth=4,
                                min_samples_leaf=10, random_state=0,
                                class_weight="balanced")
    rf.fit(X_tr, y_tr)
    rf_proba = rf.predict_proba(X_te)[:, 1]
    rf_pred  = (rf_proba >= 0.5).astype(int)
    rf_auc = roc_auc_score(y_te, rf_proba) if len(set(y_te)) > 1 else float("nan")
    metrics_rows.append({
        "model": f"rf_{feature_mode}",
        "feature_mode": feature_mode,
        "train_n": len(X_tr), "test_n": len(X_te),
        "auc": round(float(rf_auc), 3) if not np.isnan(rf_auc) else None,
        "accuracy": round(float(accuracy_score(y_te, rf_pred)), 3),
        "precision": round(float(precision_score(y_te, rf_pred, zero_division=0)), 3),
        "recall": round(float(recall_score(y_te, rf_pred, zero_division=0)), 3),
    })
    rf_imp = pd.DataFrame({
        "feature": X_tr.columns,
        f"rf_importance_{feature_mode}": rf.feature_importances_,
    })
    fi = rf_imp.merge(coef, on="feature", how="outer") if not coef.empty else rf_imp
    fi["feature_mode"] = feature_mode
    return fi


def main():
    df = pd.read_csv(IN)
    train_df, test_df = _time_split(df)

    metrics_rows = []
    # Predictive run (at-entry features only) — AUC here is the honest edge signal.
    fi_ae = _run_model_suite(train_df, test_df, "at_entry", metrics_rows)
    # Explanatory run (all features, incl. post-hoc) — AUC here is inflated by leakage.
    fi_all = _run_model_suite(train_df, test_df, "all",      metrics_rows)

    # Merge for a single importance CSV keyed on feature name.
    fi = fi_ae.merge(fi_all.drop(columns=["feature_mode"]),
                     on="feature", how="outer", suffixes=("", "_all"))
    fi = fi.sort_values(by=[c for c in fi.columns if c.startswith("rf_importance")][0], ascending=False)
    fi.to_csv(OUT_FI, index=False)
    pd.DataFrame(metrics_rows).to_csv(OUT_METRICS, index=False)

    print(f"[model_ranker] wrote {OUT_FI}")
    print(f"[model_ranker] wrote {OUT_METRICS}")
    print("\nModel metrics (at_entry = honest predictive AUC; all = leaky/explanatory):")
    for r in metrics_rows:
        print(f"  {r['model']:<22} AUC={r['auc']} acc={r['accuracy']} "
              f"prec={r['precision']} rec={r['recall']} "
              f"(train={r['train_n']}, test={r['test_n']})")
    print("\nTop 15 features (at_entry RF importance):")
    imp_col = [c for c in fi.columns if c.startswith("rf_importance_at_entry")]
    if imp_col:
        for _, row in fi.sort_values(imp_col[0], ascending=False).head(15).iterrows():
            print(f"  {row['feature']:<32} rf_at_entry={row[imp_col[0]]:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
