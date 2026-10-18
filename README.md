# Navy SEAL Sniper JINX

Profit-first clean rebuild. One decision brain, with execution and exits to be integrated in subsequent build stages.

## Phase 1 — D1ff1cult11 research extracted

Original upstream materials (including license): [research/d1ff1cult11](research/d1ff1cult11).

The importable [jinx_profit](jinx_profit) package extracts the ten deployment-transaction features described in the donor's reverse-engineering notebook, trains a LightGBM classifier on **actual labeled profitable/unprofitable executions**, and scores a new deployment without reading future trades. The chronological split sorts by event time, avoiding dependence on input file order. The adapter exposes a probability, **not a transaction/FIRE**.

Run unit tests without dependencies:

```bash
python3 -m unittest discover -s tests -v
```

Train and score when an execution-labeled dataset exists:

```bash
python3 -m pip install -r requirements-research.txt
python3 -m jinx_profit.cli train executed_trades.jsonl profit_model.joblib
python3 -m jinx_profit.cli score profit_model.joblib deployment.json
```

Each JSONL training row supplies `timestamp`, `profit_usd` (realized outcome after execution costs), and the original deployment transaction keys `transaction` and `meta`. The source notebook and code use integer chain amounts internally for math; JINX's owner-facing accounting and reports will use **USD**.

The donor's reported 35.58% ROI is a **research benchmark**, not an achieved or verified return of the new JINX. Real model training and walk-forward economic validation require historical deployments paired with actual realized trade outcomes. This phase does not claim live execution, COPY, or complete profit/exit circuitry. Do not connect the model directly to an order submission without the single profit/entry/exit lifecycle.

## Next integration boundary

Connect the trained launch model to one decision pipeline with expected USD net proceeds, current actionable executable price, exitability, and observed wallet capital, then run a real-data full-cycle outcome test. No legacy sniper FIRE paths are imported.
