"""Sorghobrayan donor #2: learned position-size notches.

Adapted from research/sorghobrayan/src/part3_sizing.py.
One sizing policy for JINX's shared profit brain, not a second FIRE path.
"""
from __future__ import annotations
import math

BASE_NOTCH = 4.0 / 81.0
NOTCH_MIN = 24
NOTCH_MAX = 120

def decode_notch(log_prediction: float) -> float:
    """Original donor's inverse log1p transform and trained-range clip."""
    value = math.expm1(float(log_prediction))
    if not math.isfinite(value):
        raise ValueError("nonfinite predicted notch")
    return max(NOTCH_MIN, min(NOTCH_MAX, value))

def train_notch_model(rows, features):
    """Original donor's LightGBM regressor trained on known historical buys.

    Each row must contain 'notch' and strictly prior deployment features.
    Caller supplies the training-time subset; never add current outcomes.
    """
    if len(rows) < 2:
        raise ValueError("notch training requires history")
    import lightgbm as lgb
    import numpy as np
    X = [[float(r[f]) for f in features] for r in rows]
    y = [math.log1p(float(r["notch"])) for r in rows]
    if any(not math.isfinite(v) or v < 0 for v in y):
        raise ValueError("invalid notch targets")
    model = lgb.LGBMRegressor(
        n_estimators=400, learning_rate=0.05, num_leaves=31,
        min_child_samples=40, subsample=0.85, subsample_freq=1,
        random_state=0, verbose=-1,
    )
    model.fit(np.asarray(X), np.asarray(y))
    return model

def predict_notches(model, rows, features):
    if model is None:
        raise ValueError("model is not trained")
    prediction = model.predict([[float(r[f]) for f in features] for r in rows])
    return [decode_notch(p) for p in prediction]

def equal_capital_allocations(predicted_notches, baseline_usd: float,
                              min_usd: float = 0.0, max_usd: float = float("inf")):
    """Donor capital-neutral allocation: baseline * notch / average notch.

    USD presentation is used; actual purchase sizing must separately account
    for full-position executable liquidation, fees, and wallet capital.
    """
    n = [float(x) for x in predicted_notches]
    if not n or any(not math.isfinite(x) or x <= 0 for x in n):
        raise ValueError("expected positive notch predictions")
    if baseline_usd <= 0 or min_usd < 0 or max_usd < min_usd:
        raise ValueError("invalid allocation bounds")
    avg = sum(n) / len(n)
    return [min(max_usd, max(min_usd, baseline_usd * x / avg)) for x in n]

def donor_absolute_allocations(predicted_notches, usd_per_native_unit: float,
                               min_usd: float = 0.0, max_usd: float = float("inf")):
    """Donor absolute notch policy, converted to owner-facing USD."""
    if usd_per_native_unit <= 0:
        raise ValueError("invalid exchange rate")
    values = [float(x) for x in predicted_notches]
    if any(not math.isfinite(x) or x <= 0 for x in values):
        raise ValueError("invalid predicted notch")
    return [min(max_usd, max(min_usd, x * BASE_NOTCH * usd_per_native_unit)) for x in values]
