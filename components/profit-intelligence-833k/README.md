# Reverse-engineering a zero-block pump.fun sniper

Analysis of the Solana wallet `5brv79eFZ2rGprXNvqgVJBkBptkkw8GJX1XydJyZLyAr`, which buys pump.fun
tokens **in the deployment slot itself**, before any market activity exists.

Three questions: what does this bot do, what does it decide on, and can we do better?

---

## The three findings that structure the work

**1. Its position size is its confidence score.**
94.6% of its entries are exact integer multiples of **4/81 SOL** (0.0493827 SOL), with a median
deviation from the integer of 2e-8. Fifteen notches cover 90% of trades, and the notch predicts the
outcome monotonically: notch <= 35 gives a 55.6% hit rate, notch > 150 gives 79.3%. The target to
reverse-engineer is therefore a *graded conviction*, not only a binary decision.

**2. It is not in the deployment bundle.**
79.6% of entries land in the zero block, but its median position is **+118 transactions after** the
create transaction, in the same block. Never before it, and only 0.36% within 5 transactions. It is
a fast listener, not a Jito bundle co-tenant, which changes what a replica may assume about
execution.

**3. Computing P&L from market caps overstates returns by a factor of 7 to 17.**
The bot's real median ROI is +6.6%. A theoretical exit at the 10-second close would show +47.6%, at
the high +109%. Candles give the *marginal* price; selling 5% of supply into a bonding curve
realises far less. The backtest therefore uses an execution model calibrated on its **4,195 real
June fills** (out-of-sample correlation 0.69, R-squared 0.47, unbiased on the mean).

---

## Data: a census, not a sample

The negative-class member of the 41.5 GB archive finally downloaded whole (**14,955,899,885 bytes,
byte-exact against the tar header**) and parses to **5,059,880 deployment transactions, 0 parse
failures.**

| | Rows |
|---|---|
| Deployments, 1 Jan - 30 Jun 2026 | 5,075,807 |
| Distinct deployer wallets | 1,005,867 |
| Restricted to the target wallet's active life (12 Mar - 30 Jun) | 3,006,792 |
| of which bought by the bot | 15,924 (**0.5296%**) |

There is no case-control sampling, no reweighting and no time matching anywhere in this repo's
current pipeline. Train is 12 Mar - 31 May; test is the whole of June, unseen.

**Headline result:** PR-AUC **0.2100** at the true base rate (x42 over chance), ROC-AUC 0.9504,
precision 0.380 at 10% recall. Removing the eight features that reference the bot's own past buys
still leaves PR-AUC 0.1017, so about half the predictive power owes nothing to circularity.

**Replica result:** at the top-0.5% operating point, +11.93% median ROI against the bot's +6.6%, on
3,805 June trades, recovering 29.0% of the bot's buys (45.8% at top-1%). Sizing each entry by the
predicted notch instead of a flat 2.469 SOL lifts total P&L **+34% at identical capital** and cuts
max drawdown from 5.5 to 4.0 SOL.

All headline numbers in this README and in the write-up are the **notebook's** output, not the
scripts'. The two differ in the third decimal because feature ordering changes LightGBM's
`colsample_bytree` draw; the notebook is what a judge replays, so it is the one that counts.

---

## The `t_decision` leakage constraint

The rubric scores Parts 2 and 3 zero if a single input feature uses data from after the deployment.
Two independent guards, both verifiable:

- **By construction**: deployer history aggregates are cumulative, ordered by slot, in a
  `RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING` window, bounded to *strictly* earlier slots.
  (`ROWS` instead of `RANGE` let two deployments by the same wallet in the same slot see each other;
  the audit below caught that bug.)
- **By recount**: `audit()` samples deployments, recomputes their history by brute force with a
  `slot < current_slot` join, and compares. Any divergence, or any row at or after the current slot,
  raises `AssertionError` and halts the pipeline. On the census it passes on 159 sampled
  deployments with zero discrepancies.

`pumpfun_trades.parquet` and `mcap_candles.parquet` are used **only** for outcome labels and
backtest P&L, never as decision inputs.

---

## Install

```bash
pip install -r requirements.txt
```

Python 3.11+. The heavy dependencies are `duckdb` (out-of-core as-of joins), `pyarrow`, `lightgbm`,
`shap`, `zstandard`.

## Fetching the data

```bash
bash src/fetch_core.sh
```

> **Warning about `65.21.203.147:48102`**: it is a Python `SimpleHTTP` that **ignores `Range`
> headers**. No resume is possible; a drop means restarting from byte 0. `fetch_core.sh` therefore
> extracts **in stream** (`curl | tar -xf -`) so that each completed member survives the next drop.
> Writing into a monolithic `.tar` first cost us 19.9 GB lost at 48%.
>
> Two further traps that cost us real time. The loop **re-truncates already-complete members** on
> its next attempt, so copy each finished member somewhere safe as soon as `check` reports it
> complete. And `os.remove` on the temp file fails on Windows while the zstd reader still holds the
> handle. Exit the read loop before deleting.

The two supplementary fetchers are only needed for Part 1 and the outcome labels:

```bash
bash src/fetch_wallet.sh      # target wallet activity (~110 MB)
bash src/fetch_candles.sh     # June market-cap candles (~2.84 GB, range-resumable)
```

## Running the pipeline

```bash
python src/run_parse.py data/raw/bought_deploy_txs.jsonl.gz data/interim/bought_features.parquet
python src/run_parse.py data/raw/not_bought_deploy_txs.jsonl.gz data/interim/neg_features_full.parquet --workers 10
python src/june_outcomes.py          # outcome labels from per-second candles
python src/run_full_pipeline.py      # labelling -> deployer history + leak audit -> Part 2 model
python src/part2_rules.py            # univariate AUC table + depth-3 readable tree
python src/part2_interpret.py        # ablation, SHAP, monotonicity, calibration, ordinal model
python src/part3_backtest.py         # replica, delay sensitivity, head-to-head
python src/part3_sizing.py           # notch-based position sizing vs flat sizing
```

`run_parse.py` on the 14.96 GB negative member takes ~70 minutes on 10 workers and needs ~4 GB free
RAM; 18 workers on 50k-line blocks exhausted a 15.7 GB machine and stalled at 227 lines/s.

---

## Layout

| File | Role |
|---|---|
| `src/deploy_parser.py` | Feature extraction from a raw deployment transaction |
| `src/run_parse.py` | Parallel `.jsonl.gz` parsing with bounded memory feeding |
| `src/part1_behavior.py` | Latency, in-block position, hold time, exits, P&L |
| `src/asof_from_deploys.py` | Deployer history rebuilt from the deployment universe (+ leak audit) |
| `src/asof_deployer.py` | Activity history truncated at `t_decision` (unused: member never landed) |
| `src/june_outcomes.py` | Per-token outcomes from per-second candles |
| `src/run_full_pipeline.py` | Labelling, then deployer history, then the census model |
| `src/part2_full.py` | Part 2 model on the full census, with the no-bot-reference ablation |
| `src/part2_rules.py` | Univariate separation table and readable depth-3 tree |
| `src/part2_interpret.py` | Ablation by family, SHAP, monotonicity, calibration, ordinal notch model |
| `src/part3_backtest.py` | Backtest with execution model, fees, delay sensitivity |
| `src/part3_sizing.py` | Notch-based position sizing measured against flat sizing |
| `src/clean_prose.py`, `src/fix_prose.py` | Strip non-ASCII from prose, then repair the sentences it breaks |
| `src/make_cover.py`, `src/make_notebook.py` | Submission artefacts |

## Traps we hit, documented for whoever picks this up

- **Four pump.fun buy discriminants, not one.** `66063d1201daebea` and `b817ee6167c5d33d` encode
  (token_amount, max_sol_cost); `38fc74089edfcd5f` and `c2ab1c46684d5b2f` encode (sol_amount,
  min_token_out). **The u64 order is reversed between the two families.** Missing two makes 19% of
  deployments look like a zero dev-buy.
- `e445a52e51cb9a1d` is the Anchor **event CPI** discriminant, never a buy.
- The mint is `create.accounts[0]`. Guessing it from the "pump" suffix misses 3% of tokens
  (non-vanity mints) and blows joins up into a cartesian product on the `None`s.
- A `u64` can exceed `int64` and break the parquet write: clamp it.
- `pool.imap()` over a whole file queues all lines into the workers before results are consumed,
  giving `MemoryError` on 5M transactions. Feed it in bounded blocks, sized to the machine.
- `gzip.open(..., 'rt')` without `encoding='utf-8'` breaks on Windows (cp1252 default).
- A parquet file being written has no footer yet; reading it mid-write raises "Parquet magic bytes
  not found", which is expected rather than corruption.
