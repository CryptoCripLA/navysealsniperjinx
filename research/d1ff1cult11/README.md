# Solana Sniper Bot Reverse-Engineering

> Reverse-engineering a live on-chain sniper bot on Solana, then building a replica strategy that beats it.

## Overview

This project analyzes wallet `5brv79eFZ2rGprXNvqgVJBkBptkkw8GJX1XydJyZLyAr`, a high-frequency sniper bot on Solana's Pump.fun. The bot buys tokens in the **zero block** — the same slot the token is deployed — before any trading activity exists on the bonding curve.

## Structure

| File | Description |
|------|-------------|
| `solana_sniper_bot_notebook.py` | Full end-to-end pipeline: behavioral analysis, feature engineering, model training, and backtest |

## Methodology

### Part 1: Behavioral Analysis
- Entry sizing (mean $262.78, median $183.42)
- Latency in slots and seconds (78.33% zero-block share)
- Hold time (median 4 seconds), exit structure (single-tx dumps)
- P&L distribution, hit rate (78.74%), equity curve, max drawdown

### Part 2: Feature Reverse-Engineering
All 10 features are extracted strictly from the deployment transaction (`t_decision = 0`):
1. `compute_units` — Compute budget of deployment
2. `fee` — Transaction fee (lamports)
3. `dev_buy_lamports` — Largest SOL transfer in deploy tx
4. `n_instructions` — Number of top-level instructions
5. `n_inner_groups` — Number of inner instruction groups
6. `n_inner_instructions` — Total inner instructions (flattened)
7. `n_signers` — Number of unique signers
8. `n_account_keys` — Number of account keys
9. `n_log_messages` — Log message count (contract complexity proxy)
10. `n_pre_token_balances` — Pre-existing token balance count

### Part 3: Replica Strategy & Backtest
- LightGBM binary classifier (Profitable vs. Unprofitable)
- Chronological 80/20 time-based split
- Slot-delay sensitivity analysis (0, 1, 2 slots with 2% slippage per slot)
- Head-to-head: token overlap, P&L, hit rate, max drawdown, equity curves

## Results
- **PR-AUC:** 0.8518
- **F1-Score:** 0.8907
- **Replica ROI (0-slot):** 35.58% vs. Bot ROI: 35.53%

## Setup & Reproduction

```bash
# Requirements
pip install pandas numpy matplotlib seaborn scikit-learn lightgbm shap

# Run
python solana_sniper_bot_notebook.py
```

On Kaggle, attach both:
- Competition dataset: `solana-sniper-bot-reverse-engineering`
- Custom dataset: `d1ff1cult11/solana-sniper-bot-reverse-engineering`

The notebook auto-discovers file paths via `os.walk`.

## t_decision Enforcement
All model features are computed exclusively from the deployment transaction JSONL. Post-deployment data (bonding-curve trades, price action) is used ONLY for computing P&L labels and backtesting — never as model input.

## License
MIT
