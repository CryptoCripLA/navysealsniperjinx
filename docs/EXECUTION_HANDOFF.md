# JINX execution handoff — 2026-10-18

## Current runnable application seam

- `jinx_profit.runner.JinxRunner` is the ONLY event-to-order controller.
- `jinx_profit.brain.ProfitBrain` owns AUTO/COPY/OFF decisions, pending orders, position state, and realized USD.
- `jinx_profit.execution_port.SingleOrderPort` checks matching fill evidence before crediting a trade.
- `jinx_profit.donor_shared_adapters` transforms external wallet, copy, and market observations. All donor research is accessible under `research/`; no donor may send transactions.
- `tests/test_runner.py` exercises a full entry/exit and partial liquidation in a fake venue. These are contract tests, not real market returns.

## Required production transport implementation

Implement a single `TradingVenue` against the chosen Solana RPC/stream/signing stack:

1. Read ordered launch and trade events and attach timestamped deployment evidence.
2. Supply executable, size-specific USD buy cost and full-position exit proceeds, plus all priority/network/venue fees.
3. Supply authenticated wallet balances and the native token's USD reference conversion; owner-facing fields must remain USD.
4. Implement `submit(intent)`, `get_confirmed_fill(tx_id)`, and `get_remaining_quantity(mint)` with chain-confirmed balances and transaction receipts.
5. Persist the submitted transaction ID and lifecycle before network submission; recover unresolved pending orders, inventory and wallet equity on restart.
6. Make submission idempotent per trade intent, resolve stale transaction statuses, reconcile partial fills, and keep selling remaining inventory until verified zero.
7. Run testnet or controlled tiny real-wallet tests only after accurate quote, fee, balance and restart checks pass.

The current controller is **not yet safe for unsupervised live trading**: the pending transaction ID is in memory, live feeds and signing aren't wired, the training model is not loaded, and the sizing model is not yet allocating a requoted executable USD order amount. No live ROI has been measured.

## Run fresh tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 -m compileall -q jinx_profit
```

## Constraints

Modes are exactly `AUTO`, `COPY`, `OFF`; no MANUAL. Only the single runner owns trade submission. Prefer full-exit verification over counting FIRE events. USD-only owner-facing reporting. Preserve research from all ten donor sources; avoid importing their competing executors or dashboards into the production path.
