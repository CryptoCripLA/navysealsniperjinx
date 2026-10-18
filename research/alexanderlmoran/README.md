# Solana Micro-Sniper

A research-grade **automated trading system** for newly-launched Solana tokens (Pump.fun / Raydium), built around a hard rule: *no parameter ships to live trading until it survives statistical scrutiny on replayed historical data.* This repo is the engineering + quantitative-research codebase behind that system.

> **Portfolio note:** Shared publicly as a sample of my work in real-time systems and quantitative research. It is **not** investment advice, not a turnkey money-maker, and not maintained for third-party use. Live trading of low-cap tokens is extremely high-risk. See *Usage & disclaimer* below.

---

## What it does

The system watches the Solana mempool/WebSocket firehose for brand-new token launches, scores each candidate in real time, simulates or executes an entry, and manages the position with a multi-stage exit ladder — while logging every decision variable so the strategy can be replayed and re-evaluated offline.

It runs in three modes:

- **Paper** — full simulation against the live feed, no funds at risk (default).
- **Replay** — deterministic re-execution of recorded launches to test strategy changes against identical conditions.
- **Live canary** — real execution with hard risk caps (fixed micro trade size, max concurrent positions, daily-loss kill switch, failsafe stop), gated behind explicit acknowledgment flags.

## Engineering highlights

- **Async real-time ingestion** — `asyncio` + `websockets` consuming Solana RPC/WS with ping/timeout watchdogs and reconnection.
- **Deterministic replay engine** (`replay_v2.py`, `replay_analyzer.py`) — re-runs recorded token lifecycles so a strategy change can be A/B'd against the exact same market data. Live and post-hoc logic import shared constants (`constants.py`) so behavior cannot drift.
- **Single-source-of-truth config** — every tunable parameter is env-driven with documented defaults and a canonical registry (`VARIABLES_REGISTRY.md`).
- **Exit-ladder position management** — trailing stops, breakeven triggers, and a tiered take-profit ladder, all unit-shared between live and analysis.
- **Live HUD** — FastAPI backend (`sniper_hud_api.py`) + vanilla-JS frontend (`app.js` / `index.html`) for real-time position and telemetry monitoring.
- **Operational safety** — kill switches, failsafe stops, wallet/PnL reconciliation, and scaling gates that must pass before size increases.

## Quantitative research pipeline

The bulk of this repo is the research that decides *what* the bot does:

- **Feature engineering** (`feature_builder.py`, `extract_trades.py`) from recorded launch telemetry.
- **Edge studies** (`edge_study/`) — lifecycle backtests, tick-level analysis, catastrophic-loss analysis, and a trained classifier, each with a written findings report.
- **Statistical rigor** — exact **Clopper–Pearson** binomial confidence intervals and **walk-forward evaluation** (`walk_forward_eval.py`) to avoid overfitting and to show that short-sample win-rate comparisons are mostly noise.
- **Attribution & scorecards** (`trade_attribution.py`, `batch_scorecard.py`, `model_ranker.py`) to rank exit strategies and rule sets.

## Tech stack

| Area | Tools |
|---|---|
| **Language** | Python 3.13 (async) |
| **Solana / chain** | `solders`, `base58`, Pump.fun & Raydium program interfaces, Jito bundles |
| **Networking** | `aiohttp`, `websockets`, `httpx` |
| **API / HUD** | FastAPI, Uvicorn + vanilla JS/CSS frontend |
| **Research / ML** | pandas, NumPy, SciPy, scikit-learn |
| **Config** | `python-dotenv`, `pydantic` |

## Layout

```
.
├── simulation_mode.py      # main engine (paper/live), real-time scoring & execution
├── live_canary.py          # live-canary execution path with hard risk caps
├── sniper_hud_api.py        # FastAPI HUD backend
├── app.js / index.html / style.css   # live monitoring HUD frontend
├── constants.py            # single source of truth for strategy constants
├── replay_v2.py / replay_analyzer.py # deterministic replay + analysis
├── feature_builder.py / extract_trades.py  # research feature pipeline
├── edge_study/             # backtests, tick studies, classifier + findings reports
├── docs/                   # implementation plan, schemas, archived reports
└── *.md                    # research reports (exit lab, replay, variables registry)
```

## Running (paper mode)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # defaults to EXECUTION_MODE=paper (safe)
python simulation_mode.py
```

Live mode additionally requires a funded wallet key and explicit acknowledgment flags (see `.env_archive/.env.canary.template`) — intentionally cumbersome to enable.

## Usage & disclaimer

© Alexander Moran. All rights reserved. Published for portfolio/code-review purposes only — **not** licensed for reuse or redistribution. This software is for research and educational demonstration. It is **not financial advice**. Trading newly-launched low-cap tokens carries extreme risk of total loss. Use at your own risk; the author accepts no liability.
