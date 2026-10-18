"""Kaizen256 donor #3: strict-prior deployer relationship intelligence.

Derived from research/kaizen256/src/solana_sniper_reverse_engineering/
target_relationship.py. Produces its named historical relationship features
without importing research-only DuckDB pipelines into JINX's live path.
"""
from __future__ import annotations
from datetime import datetime, timezone
from math import log1p

WINDOWS = {"1h": 3600, "6h": 21600, "1d": 86400, "7d": 604800, "30d": 2592000}

def deployer_relationship_features(candidate_time: int, prior_deployments: list[dict],
                                   prior_target_buys: list[dict]) -> dict[str, float]:
    """Historical events have signer, timestamp; decision uses strictly earlier events.

    prior_target_buys represents research target-wallet buys and is deliberately
    not a signal to copy its future actions. This function only computes features.
    """
    cutoff = int(candidate_time)
    deploys = sorted(int(x["timestamp"]) for x in prior_deployments if int(x["timestamp"]) < cutoff)
    buys = sorted(int(x["timestamp"]) for x in prior_target_buys if int(x["timestamp"]) < cutoff)
    n = len(buys)
    last = buys[-1] if buys else None
    after_last = sum(t > last for t in deploys) if last is not None else len(deploys)
    result = {
        "target_signer_known": int(n > 0),
        "prior_target_buy_count": n,
        "seconds_since_prior_target_buy": cutoff-last if last is not None else -1,
        "deployments_since_prior_target_buy": after_last,
        "seconds_since_first_target_buy": cutoff-buys[0] if buys else -1,
        "prior_target_buy_count_log1p": log1p(n),
        "prior_target_buy_recency_log1p": log1p(cutoff-last) if last is not None else -1,
        "prior_target_buy_fraction": n / len(deploys) if deploys else 0.0,
        "prior_target_buy_rate_shrunk_5": n/(len(deploys)+5),
        "prior_target_buy_rate_shrunk_20": n/(len(deploys)+20),
        "prior_target_deployments_per_buy": len(deploys)/n if n else 0.0,
    }
    for name, duration in WINDOWS.items():
        recent_buys = sum(t >= cutoff-duration for t in buys)
        recent_deploys = sum(t >= cutoff-duration for t in deploys)
        result[f"prior_target_buy_count_{name}"] = recent_buys
        if name in ("1h", "1d", "7d", "30d"):
            result[f"prior_target_buy_rate_{name}"] = recent_buys/recent_deploys if recent_deploys else 0.0
    days = max((cutoff-buys[0])/86400, 1) if buys else 1
    result["prior_target_buys_per_known_day"] = n/days
    return result
