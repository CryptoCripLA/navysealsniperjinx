"""Deployment-time feature adapter extracted from D1ff1cult11 research.

Only information present in the supplied confirmed deployment transaction is read.
No chart, future token trade, or post-deployment wallet activity is accepted.
"""
from __future__ import annotations

FEATURE_NAMES = (
    "compute_units", "fee", "dev_buy_lamports", "n_instructions",
    "n_inner_groups", "n_inner_instructions", "n_signers",
    "n_account_keys", "n_log_messages", "n_pre_token_balances",
)

def extract_deployment_features(tx: dict) -> dict[str, int]:
    """Extract the notebook's exact ten input features; reject unusable events."""
    if not isinstance(tx, dict):
        raise ValueError("deployment transaction must be a mapping")
    transaction = tx.get("transaction") or {}
    message = transaction.get("message") or {}
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        raise ValueError("failed deployment transaction")
    inner = meta.get("innerInstructions") or []
    has_mint = any(
        isinstance(i.get("parsed"), dict)
        and i["parsed"].get("type") in ("initializeMint2", "initializeMint")
        for g in inner for i in (g.get("instructions") or [])
    )
    if not has_mint:
        raise ValueError("no parsed mint initialization in deployment")
    instructions = message.get("instructions") or []
    largest_transfer = max(
        (int(i["parsed"].get("info", {}).get("lamports", 0))
         for i in instructions
         if isinstance(i.get("parsed"), dict)
         and i["parsed"].get("type") == "transfer"),
        default=0,
    )
    result = dict(zip(FEATURE_NAMES, (
        int(meta.get("computeUnitsConsumed") or 0),
        int(meta.get("fee") or 0),
        largest_transfer,
        len(instructions),
        len(inner),
        sum(len(g.get("instructions") or []) for g in inner),
        len(transaction.get("signatures") or []),
        len(message.get("accountKeys") or []),
        len(meta.get("logMessages") or []),
        len(meta.get("preTokenBalances") or []),
    )))
    if any(v < 0 for v in result.values()):
        raise ValueError("negative deployment feature")
    return result
