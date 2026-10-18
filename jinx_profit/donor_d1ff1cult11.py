"""D1ff1cult11 donor implementation adapted into callable, reusable functions.

Source: research/d1ff1cult11/solana_sniper_bot_notebook.py
Retains donor's USD P&L grouping, ten-feature vector, LightGBM hyperparameters,
and 0/1/2-slot ROI comparison. Importing does not execute the research notebook.
"""
from __future__ import annotations
import json
from pathlib import Path
from .features import FEATURE_NAMES, extract_deployment_features
from .model import chronological_split

def donor_pnl_records(trades):
    """Donor lines 142-156: invested buys vs recovered sells, grouped per mint."""
    grouped = {}
    for trade in trades:
        mint = trade["token_address"]
        side = trade["event_type"].lower()
        if side not in ("buy", "sell"):
            continue
        row = grouped.setdefault(mint, {"token_address": mint, "total_invested_usd": 0.0,
                                       "total_recovered_usd": 0.0})
        value = float(trade["cost_usd"])
        if value < 0:
            raise ValueError("negative trade cash flow")
        if side == "buy":
            row["total_invested_usd"] += value
        else:
            row["total_recovered_usd"] += value
    for row in grouped.values():
        if row["total_invested_usd"] <= 0:
            continue
        row["profit_usd"] = row["total_recovered_usd"] - row["total_invested_usd"]
        row["profitable"] = int(row["profit_usd"] > 0)
        row["roi_pct"] = row["profit_usd"] / row["total_invested_usd"] * 100
        yield row

def build_labeled_dataset(deployments, trades, *, require_complete=True):
    """Join original deployment transactions to donor grouped economic outcomes.

    A deployment gets its timestamp from blockTime; a close-only dataset is
    requested by passing require_complete=True (requires observed sell).
    """
    source = {d["token_address"]: d for d in deployments}
    closed = {t["token_address"] for t in trades if t["event_type"].lower() == "sell"}
    rows = []
    for pnl in donor_pnl_records(trades):
        mint = pnl["token_address"]
        if mint not in source or (require_complete and mint not in closed):
            continue
        deployment = source[mint]
        feature = extract_deployment_features(deployment)
        rows.append({"timestamp": int(deployment["blockTime"]), **pnl,
                     "transaction": deployment["transaction"], "meta": deployment["meta"],
                     "features": feature})
    return sorted(rows, key=lambda r: r["timestamp"])

def fit_donor_replica(dataset, *, test_fraction=0.2):
    """Same LightGBM classifier and settings as original donor lines 310-334."""
    train, holdout = chronological_split(dataset, test_fraction)
    labels = [int(r["profitable"]) for r in train]
    if len(set(labels)) != 2:
        raise ValueError("need wins and losses in training period")
    import lightgbm as lgb
    X_train = [[r["features"][key] for key in FEATURE_NAMES] for r in train]
    model = lgb.LGBMClassifier(
        n_estimators=200, learning_rate=0.05,
        max_depth=6, num_leaves=31, min_child_samples=20,
        random_state=42, class_weight="balanced", verbosity=-1,
    )
    model.fit(X_train, labels, feature_name=list(FEATURE_NAMES))
    return model, holdout

def donor_slot_comparison(holdout, selected, delays=(0, 1, 2)):
    """Exact donor delay assumption: entry worsens 2% per additional slot.

    This is a research sensitivity calculation, not a live fill prediction.
    """
    if len(holdout) != len(selected):
        raise ValueError("selection and holdout must align")
    output = []
    for delay in delays:
        if delay < 0:
            raise ValueError("negative delay")
        chosen = [r for r, yes in zip(holdout, selected) if yes]
        invested = sum(r["total_invested_usd"] * (1 + delay * 0.02) for r in chosen)
        recovered = sum(r["total_recovered_usd"] for r in chosen)
        output.append({"slots_late": delay, "trades": len(chosen),
                       "roi_pct": (recovered - invested) / invested * 100 if invested else None,
                       "pnl_usd": recovered - invested,
                       "win_rate": (sum(r["total_recovered_usd"] >
                          r["total_invested_usd"]*(1 + delay * 0.02) for r in chosen)/len(chosen))
                          if chosen else None})
    return output

def read_jsonl(path):
    with open(path, encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]
