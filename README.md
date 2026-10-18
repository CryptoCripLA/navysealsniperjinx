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

## Unified profit brain (current build)

- `jinx_profit/brain.py`: exactly one AUTO/COPY/OFF decision path; one outstanding order and one position; USD entry/exit intent and realized P&L.
- `jinx_profit/execution_port.py`: the sole future trading gateway; requires matching confirmed fill receipts and checks remaining holdings before declaring closure.
- D1ff1cult11 supplies ten deployment features + model probability; Kaizen supplies prior-history context; Sorghobrayan supplies conviction notch predictions; Moran supplies historical exit rules and an observed-only trailing-exit signal. Donors never directly trade.
- `tests/`: 29 tests, including two full mocked receipt lifecycles.

**Before enabling any LIVE trading**, the new-server execution adapter must provide authoritative price/size-specific quotes, wallet/account balance discovery on restart, durable pending-order and partial-fill state, chain-confirmed trade outcomes, token-account closure verification, failure/retry handling and timestamped market evidence. The currently implemented probability model does not yet include Kaizen history in its trained feature vector; Sorghobrayan's notch is computed as context, not yet fed back into a requoted executable dollar amount. Avoid claiming these two capabilities are economically integrated until the model is retrained and price-impact-aware sizing verified. Moran's research MFE counterfactual cannot determine live orders from future highs.

**Remaining donor sequence:** 5. wallet behavior analysis, 6. LYC0603 copier economics, 7. pumpfun-sim exact execution, 8. Pumpfun Market Lab, 9. Money Money Come wallet profiling, 10. PumpCopy execution intelligence. Add each as a source of evidence into this ONE lifecycle. Never introduce alternate order paths.

## Donors 5–10 — cloned and wired (2026-10-18)

The six complete original repositories are cloned as separate read-only workspaces under `/tmp/jinx-donors/` on the existing `jinx-nyc` build host (not deployed). Selected original algorithms are also versioned under `research/` in this repo:

5. `wallet_behavior` — address transaction classification, cumulative proportional copy exits.
6. `copy_economics` — historical copier-performance machine-learning feature pipeline.
7. `pump_sim` — replay engine and market state from on-chain trades.
8. `market_lab` — historical market-data processing.
9. `wallet_profiles` — profitable-wallet profiling rules.
10. `pumpcopy` — original C# copy engine and sizing source.

`jinx_profit/donor_shared_adapters.py` connects the shared mechanics for wallet realized P&L, ranking, proportional copying, post-leader entry net economics, executable full-position quote economics, and timestamp-correct event normalization. `jinx_profit/brain.py` uses the copier and executable economics methods. **No donor owns a second FIRE path.**

This phase imports and tests shared economic adapters; **it does not mean every function of six donor codebases has been ported or invoked, or that the original programs can run independently inside JINX.** Original unported files remain available in their individual clones on the build host. No live order network, complete market feed, model artifacts or realized 35–50% ROI has been demonstrated.
