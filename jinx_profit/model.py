"""Profit prediction adapter for the D1ff1cult11 ten-feature research model.

Research training only; the caller supplies point-in-time labeled transactions.
The returned estimator is a predictor, NOT a signal to submit a transaction.
"""
from __future__ import annotations
from .features import FEATURE_NAMES, extract_deployment_features

def chronological_split(rows, test_fraction: float = 0.2):
    if not 0 < test_fraction < 1:
        raise ValueError("test_fraction must be between 0 and 1")
    ordered = sorted(rows, key=lambda r: r["timestamp"])
    if len(ordered) < 2:
        raise ValueError("at least two labeled observations required")
    cut = max(1, min(len(ordered)-1, int(len(ordered)*(1-test_fraction))))
    # Avoid leaking identical timestamp observations across the boundary.
    while cut > 0 and ordered[cut-1]["timestamp"] == ordered[cut]["timestamp"]:
        cut -= 1
    if cut == 0:
        raise ValueError("timestamp boundary ambiguous")
    return ordered[:cut], ordered[cut:]

def train_profit_model(rows, test_fraction: float = 0.2):
    """Train only on earlier profitable/unprofitable executed trades.

    Returns (model, held_out_rows). Does not tune thresholds using holdout.
    Optional LightGBM is imported only when actual training is requested.
    """
    train, test = chronological_split(rows, test_fraction)
    labels = [int(float(r["profit_usd"]) > 0) for r in train]
    if len(set(labels)) < 2:
        raise ValueError("training needs both profitable and unprofitable trades")
    from lightgbm import LGBMClassifier
    X = [[extract_deployment_features(r)[key] for key in FEATURE_NAMES] for r in train]
    model = LGBMClassifier(
        n_estimators=200, learning_rate=0.05, max_depth=6,
        num_leaves=31, min_child_samples=20, random_state=42,
        class_weight="balanced", verbosity=-1,
    )
    model.fit(X, labels, feature_name=list(FEATURE_NAMES))
    return model, test

def score_opportunity(model, features: dict[str, int]) -> float:
    """Return calibrated-input model probability, not an automatic FIRE command."""
    if model is None:
        raise ValueError("no trained profit model")
    values = [int(features[k]) for k in FEATURE_NAMES]
    score = float(model.predict_proba([values])[0][1])
    if not 0 <= score <= 1:
        raise ValueError("invalid model output")
    return score
