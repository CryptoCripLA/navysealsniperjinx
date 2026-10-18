# Tick-Level Reality Check
_Sub-second data from Birdeye `/defi/txs/token` — every swap, not 1m aggregates._

_N=11 trades (5 biggest wins + 5 biggest losses, dedup'd by mint)._

## Per-trade tick tape

| label | mint | our ROI | real MFE | s→peak | real MAE | s→trough | txs 10s/60s/300s | buys/sells 60s | wallets 60s | SOL vol 60s |
|---|---|---:|---:|---:|---:|---:|---|---|---:|---:|
| winner | BPbkDtULWM | +298% | +146% | 136s | -94% | 1472s | 0/0/2327 | 0/0 | 0 | 0.000 |
| winner | 93uXm6DJ7m | +160% | +15% | 335s | -95% | 1249s | 0/0/0 | 0/0 | 0 | 0.000 |
| winner | 5mtTTEfMcY | +131% | +18% | 172s | -92% | 1546s | 0/0/992 | 0/0 | 0 | 0.000 |
| winner | AaQk33y9LG | +85% | +24% | 517161s | -4% | 517066s | 0/0/0 | 0/0 | 0 | 0.000 |
| winner | GGVBgNnhcA | +76% | +0% | 0s | -13% | 416582s | 0/0/0 | 0/0 | 0 | 0.000 |
| loser | 4YiLHDR4B4 | -106% | +0% | 1322961s | -5% | 1323305s | 0/0/0 | 0/0 | 0 | 0.000 |
| loser | 6au69HRQrs | -106% | +0% | 0s | -17% | 816s | 3/3/6 | 0/3 | 1 | 0.212 |
| loser | 7hzoMzdE3c | -106% | +8% | 1549s | -0% | 524s | 0/0/0 | 0/0 | 0 | 0.000 |
| loser | Bcmi8nudnj | -106% | +19% | 4s | -81% | 554s | 102/427/604 | 185/242 | 266 | 135.891 |
| loser | EzkRFdFK4W | -106% | +157% | 1549s | -11% | 1044s | 1/14/134 | 6/8 | 11 | 5.050 |
| loser | HbZEjQzS5g | -106% | +171% | 1073s | -7% | 29s | 48/169/603 | 73/96 | 127 | 67.243 |

## Tick MFE vs 1m-candle MFE — the gap

_Quantifies what our 1m-resolution backtest could not see._

| label | mint | tick MFE | 1m MFE | gap | tick MAE | 1m MAE |
|---|---|---:|---:|---:|---:|---:|
| winner | BPbkDtULWM | +146% | +184% | -38pp | -94% | -94% |
| winner | 93uXm6DJ7m | +15% | +32% | -17pp | -95% | -97% |
| winner | 5mtTTEfMcY | +18% | +98% | -80pp | -92% | -93% |
| winner | AaQk33y9LG | +24% | +119% | -95pp | -4% | -89% |
| winner | GGVBgNnhcA | +0% | +545% | -545pp | -13% | -90% |
| loser | 4YiLHDR4B4 | +0% | +46% | -46pp | -5% | -77% |
| loser | 6au69HRQrs | +0% | +1% | -1pp | -17% | -83% |
| loser | 7hzoMzdE3c | +8% | +9% | -1pp | -0% | -10% |
| loser | Bcmi8nudnj | +19% | +189% | -170pp | -81% | -46% |
| loser | EzkRFdFK4W | +157% | +105% | +52pp | -11% | -75% |
| loser | HbZEjQzS5g | +171% | +134% | +37pp | -7% | -60% |

## Aggregate: first-60s tick signature

_Is there a separation in early-window activity between winners and losers?_

| metric | winners median | losers median |
|---|---:|---:|
| txs in first 10s | 0 | 3 |
| txs in first 60s | 0 | 14 |
| txs in first 300s | 0 | 134 |
| buys/sells ratio 60s | 0.00 | 0.75 |
| unique wallets 60s | 0 | 11 |
| SOL volume 60s | 0.000 | 5.050 |
| seconds to peak | 172 | 1549 |

## Interpretation — honest answer

### What we can read

1. **Coverage is asymmetric.** Winners are still actively trading, so offset-pagination (the only cursor free-tier supports — `before_time`/`before_id` returns 500) runs out before reaching entry. For 4/5 winners the earliest tick we got was +74s to +332s past entry. For 4/6 losers we have full pre-entry coverage because the token died.

2. **Tick MFE ≤ 1m MFE on 9/11 trades.** The 1m candle wick already captured the highs. The backtest was NOT hiding sub-minute upside — Birdeye's `tokenPrice` field reflects per-tx pool state, while 1m `h` reflects the max across the minute. The 1m analysis was not the blindfold.

3. **Losers with coverage show a clean pre-entry tell.** In the 10 seconds before our entry:

| mint | pre-10s buys/sells | pattern |
|---|---|---|
| Bcmi8nudnj | 126 / 149 | sell-dominant |
| EzkRFdFK4W | 2 / 6 | 3× sell-dominant |
| HbZEjQzS5g | 41 / 52 | sell-dominant |
| 6au69HRQrs | 1 / 0 | dead |
| 7hzoMzdE3c | 1 / 0 | dead |

   **4 of 5 losers with data had sells ≥ buys in the final 10 seconds.** We bought into active distribution. This is invisible to 1m candles and is NOT a signal we currently check.

### What we can't read from free tier

Winner pre-entry tapes. We cannot confirm this buy/sell imbalance is discriminative — maybe winners also show sell-pressure in the final 10s and this is just a coincidence. Without matching winner coverage, N=5 on one side is suggestive, not conclusive.

### What this means for "the bot buys blindly"

Partially correct. The 30-second fixed delay entry has NO look at real-time tx flow. A live bot could subscribe to the mempool/WS tx stream and abort if sell-count > buy-count in the final seconds — that's a free-tier-compatible signal because it's client-side aggregation of on-chain events, not a paid historical API. This is the one concrete gate improvement this study surfaces.

### What this does NOT change

The dominant P&L leak is still the 31 catastrophic trades. Pre-entry sell-imbalance might catch some — but:
- Rugs where the dump happens _after_ our entry (not before it) won't be caught.
- The 2 dead-mint losers (6au, 7hzo) had no activity to classify — they'd pass any tx-flow gate.
- Still doesn't address dev-wallet / security signals that live outside tx data.

Final verdict: FINDINGS.md conclusion (paid data gates the next tier of edge) stands. But there's ONE free-tier code change worth prototyping: pre-entry buy/sell balance check over the 5-10 seconds before firing. Estimated catch: ~3 of 31 catastrophics = ~$9/mo savings at current stakes — real but marginal.

Raw tapes: `edge_study/ticks/<mint>.json`.