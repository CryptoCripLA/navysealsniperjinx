"""
train_classifier.py — Train HistGradientBoostingClassifier on features.csv.

Label: is_winner (ROI > 5%). Baseline 18.3% positive class.

Three model tiers:
  Model A: broad features (time, source, delay) — all 601 trades
  Model B: Model A + fill/pool features — ~280 trades
  Model C: Model B + entry_score — ~61 trades (likely too small)

Evaluation:
  - Stratified 5-fold CV
  - Metrics: AUC, precision@top-decile, baseline comparison
  - Permutation feature importance on full-fit model
  - Tiered P&L projection (what if we sized by predicted percentile?)
"""
import csv
import math
import numpy as np
from pathlib import Path
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import roc_auc_score, average_precision_score
from sklearn.inspection import permutation_importance

ROOT = Path(__file__).resolve().parent.parent
FEAT = ROOT / "edge_study" / "features.csv"


def load():
    rows = list(csv.DictReader(open(FEAT)))
    return rows


def to_float(v):
    if v == "" or v is None:
        return math.nan
    try:
        return float(v)
    except Exception:
        return math.nan


FEATURE_TIERS = {
    "A_broad": [
        "hour_of_day", "day_of_week", "delay_s",
        "source_migration_retry", "source_unset",
        "has_reference_price",
    ],
    "B_fill": [
        "hour_of_day", "day_of_week", "delay_s",
        "source_migration_retry", "source_unset",
        "has_reference_price",
        "entry_px", "size_usd", "slippage_pct", "pre_entry_ratio",
        "lp_locked_pct", "lp_locked_usd", "holder_top5_pct",
        "liq_known", "route_out_amount",
    ],
    "C_score": [
        "hour_of_day", "day_of_week", "delay_s",
        "source_migration_retry", "source_unset",
        "has_reference_price",
        "entry_px", "size_usd", "slippage_pct", "pre_entry_ratio",
        "lp_locked_pct", "lp_locked_usd", "holder_top5_pct",
        "liq_known", "route_out_amount",
        "score_total", "score_graduation", "score_dex", "score_holder",
        "score_social", "score_age_s", "score_fetch_err",
    ],
}


def build_matrix(rows, features, require_any=None):
    """Build X, y, roi, mint. If require_any given, drop rows where ALL of
    require_any are NaN (keeps rows that have at least some of the tier's key features)."""
    X, y, roi, mints = [], [], [], []
    for r in rows:
        if require_any:
            has_any = any(not math.isnan(to_float(r.get(f))) for f in require_any)
            if not has_any:
                continue
        X.append([to_float(r.get(f)) for f in features])
        y.append(int(r["is_winner"]))
        roi.append(float(r["roi_pct"]))
        mints.append(r["mint"])
    return np.array(X, dtype=float), np.array(y, dtype=int), np.array(roi), mints


def evaluate(rows, features, require_any, tier_name):
    X, y, roi, mints = build_matrix(rows, features, require_any)
    n = len(y)
    if n < 30:
        return {"tier": tier_name, "n": n, "skipped": True}

    print(f"\n=== Model {tier_name}: n={n}, positives={y.sum()} ({y.mean():.1%}) ===")

    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    aucs, aps, top_decile_wr, top_decile_roi = [], [], [], []
    all_pred = np.zeros(n)
    for tr, te in skf.split(X, y):
        clf = HistGradientBoostingClassifier(
            max_iter=200, max_depth=4, learning_rate=0.05, random_state=42
        )
        clf.fit(X[tr], y[tr])
        p = clf.predict_proba(X[te])[:, 1]
        all_pred[te] = p
        if y[te].sum() > 0 and y[te].sum() < len(y[te]):
            aucs.append(roc_auc_score(y[te], p))
            aps.append(average_precision_score(y[te], p))

    # Top-decile analysis on out-of-fold predictions
    k = max(1, n // 10)
    top_idx = np.argsort(all_pred)[-k:]
    td_wr = y[top_idx].mean()
    td_roi = roi[top_idx].mean()
    baseline_wr = y.mean()
    baseline_roi = roi.mean()

    print(f"  AUC (CV mean ± std): {np.mean(aucs):.3f} ± {np.std(aucs):.3f}")
    print(f"  AP  (CV mean ± std): {np.mean(aps):.3f} ± {np.std(aps):.3f}")
    print(f"  Baseline win rate:   {baseline_wr:.1%}   baseline avg ROI: {baseline_roi:+.2f}%")
    print(f"  Top-decile win rate: {td_wr:.1%}   top-decile avg ROI: {td_roi:+.2f}%")
    print(f"  Lift (win rate):     {td_wr - baseline_wr:+.1%} ({td_wr/baseline_wr:.2f}x)")

    # Tiered sizing projection
    # p50, p80, p95 splits
    p_sorted_idx = np.argsort(all_pred)
    n_p50 = int(n * 0.50)
    n_p80 = int(n * 0.30)  # between p50 and p80
    n_p95 = int(n * 0.15)  # between p80 and p95
    n_p100 = n - n_p50 - n_p80 - n_p95
    buckets = {
        "below p50 ($1)":  (p_sorted_idx[:n_p50], 1.0),
        "p50-p80 ($25)":   (p_sorted_idx[n_p50:n_p50+n_p80], 25.0),
        "p80-p95 ($100)":  (p_sorted_idx[n_p50+n_p80:n_p50+n_p80+n_p95], 100.0),
        "p95+ ($500)":     (p_sorted_idx[-n_p100:], 500.0),
    }
    total_pnl = 0.0
    total_size = 0.0
    print(f"  Tiered sizing projection:")
    for name, (idx, size) in buckets.items():
        if len(idx) == 0:
            continue
        bucket_roi = roi[idx].mean()
        bucket_pnl = sum((roi[i] / 100.0) * size for i in idx)
        total_pnl += bucket_pnl
        total_size += size * len(idx)
        print(f"    {name}: n={len(idx)} avg_roi={bucket_roi:+.2f}% total_pnl=${bucket_pnl:+.2f}")
    print(f"    → Net P&L over {n} trades: ${total_pnl:+.2f}")
    baseline_pnl = sum((r / 100.0) * 1.0 for r in roi)
    print(f"    → Flat $1 sizing baseline: ${baseline_pnl:+.2f}")

    # Permutation importance on full-fit model
    clf_full = HistGradientBoostingClassifier(
        max_iter=200, max_depth=4, learning_rate=0.05, random_state=42
    )
    clf_full.fit(X, y)
    try:
        pi = permutation_importance(clf_full, X, y, n_repeats=10, random_state=42, scoring="roc_auc")
        imp = sorted(zip(features, pi.importances_mean, pi.importances_std), key=lambda t: -t[1])
        print(f"  Top feature importances (permutation, AUC delta):")
        for feat, m, s in imp[:8]:
            print(f"    {feat:28s} {m:+.4f} ± {s:.4f}")
    except Exception as e:
        print(f"  Importance failed: {e}")

    return {
        "tier": tier_name, "n": n, "pos": int(y.sum()),
        "auc_mean": float(np.mean(aucs)), "auc_std": float(np.std(aucs)),
        "ap_mean": float(np.mean(aps)),
        "baseline_wr": float(baseline_wr), "baseline_roi": float(baseline_roi),
        "top_decile_wr": float(td_wr), "top_decile_roi": float(td_roi),
        "tiered_pnl": float(total_pnl), "baseline_pnl": float(baseline_pnl),
    }


def main():
    rows = load()
    results = []
    # Model A: no filter — all trades
    results.append(evaluate(rows, FEATURE_TIERS["A_broad"], None, "A_broad"))
    # Model B: require at least one of the fill features
    results.append(evaluate(
        rows, FEATURE_TIERS["B_fill"],
        require_any=["entry_px", "slippage_pct", "pre_entry_ratio"],
        tier_name="B_fill",
    ))
    # Model C: require score
    results.append(evaluate(
        rows, FEATURE_TIERS["C_score"],
        require_any=["score_total"],
        tier_name="C_score",
    ))

    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    for r in results:
        if r.get("skipped"):
            print(f"  {r['tier']}: SKIPPED (n={r['n']} too small)")
        else:
            print(f"  {r['tier']:10s} n={r['n']:3d} AUC={r['auc_mean']:.3f}±{r['auc_std']:.3f}  "
                  f"top-decile ROI {r['top_decile_roi']:+6.2f}%  vs baseline {r['baseline_roi']:+6.2f}%  "
                  f"tiered P&L=${r['tiered_pnl']:+.2f} vs flat=${r['baseline_pnl']:+.2f}")

    return results


if __name__ == "__main__":
    main()
