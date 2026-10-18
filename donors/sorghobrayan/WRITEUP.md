# A sniper that bets in notches

### Reverse-engineering `5brv79e`, and why market-cap backtests lie by a factor of 7 to 17

---

## What the bot is

Wallet `5brv79eFZ2rGprXNvqgVJBkBptkkw8GJX1XydJyZLyAr` bought **16,163 pump.fun tokens** between
2026-03-12 and 2026-06-30, entering in the deployment slot itself. It deployed 151,004 SOL, returned
44,915 SOL, and never drew down more than 119 SOL. Three findings shape everything below; two
contradict the obvious hypothesis.

---

## Part 1: Behaviour

### 1. Position size is a discrete confidence score

Entry size is median **2.47 SOL** but mean **9.34** (sd 57.3, max 2,064): a long right tail.
And **94.6% of entries are exact integer multiples of 4/81 SOL** (0.0493827), median deviation
2x10-8, floating-point exact. Fifteen notches cover 90% of trades; notch 40 alone is 27%.

This is not cosmetic. The notch predicts the outcome, monotonically:

| Notch (units of 4/81 SOL) | Trades | Hit rate | Median ROI | Mean P&L |
|---|---|---|---|---|
| <= 35 | 3,235 | 55.6% | +1.8% | +0.12 SOL |
| 36 to 45 | 4,497 | 59.6% | +3.5% | +0.22 SOL |
| 46 to 60 | 3,732 | 68.6% | +10.8% | +0.53 SOL |
| 61 to 80 | 1,470 | 75.0% | +20.3% | +1.07 SOL |
| 81 to 150 | 1,350 | 75.3% | +20.6% | +1.70 SOL |
| > 150 | 1,613 | **79.3%** | +20.9% | +23.36 SOL |

The bot knows which tokens are better and bets more on them. So the target to reverse-engineer is
not only the binary decision: it is a **graded conviction**. Position size is not correlated with
the dev-buy (r = -0.04), so the grading comes from elsewhere.

### 2. It is not in the deployment bundle

79.6% of entries land in the deployment slot; 98.3% within one slot. But **its median position is
+118 transactions after the create transaction, inside the same block**. Only 0.36% land within 5
transactions of the deploy, and not a single entry precedes it.

This rules out bundle co-location. The bot is a fast listener reacting inside the block, paying a
median 0.050 SOL priority fee (P90 0.367), with a Jito tip on only 32% of entries. Any replica that
assumes a bundle slot is backtesting a machine that does not exist.

### 3. It is a 6-second scalper with a very fat tail

Median latency is **0 s** (mean 36.4, a thin tail). Median hold is **6 seconds**.
It exits in a ladder: 4.4 sells per token, 73.5% closed in 3 or more, 100% closed. It **almost
never burns**: 2 burn events in 87,007 wallet actions.

Hit rate is 64.7%, mean win +4.53 SOL against mean loss -0.43 SOL. But a handful of tokens dominates
the aggregate: **one trade is 12.4% of total P&L, the top 1% is 69.3%**. Strip that 1% and global
ROI falls from 29.7% to 14.9%. Any head-to-head on total P&L is mostly a comparison of luck; we
report median per-trade metrics with bootstrap intervals instead.

### 4. Its selection genuinely works

Against all 764,451 June tokens, its picks reach 1.5x within 5 seconds **79.8% of the time versus a
21.9% base rate** (lift x3.65), and double within 10 seconds 53.6% versus 12.1% (lift x4.42).

Part of that lift is reflexive: its own 2.5 SOL buy moves a fresh curve, and followers move it more.
Part 3 bounds that share instead of assuming it away.

---

## The measurement that changes the backtest

The bot's **real** median ROI is **+6.6%**. Exiting at the 10-second close would show +47.6%; at the
10-second high, +109%. It captures only 6.8% of the theoretical move.

The reason is execution, not skill. Market-cap candles give the *marginal* price. Selling ~5% of
supply into a bonding curve realises far less. **A backtest computed from market-cap ratios
overstates ROI by a factor of 7.2 to 16.6.**

We therefore fit an execution model on the bot's **4,195 real June fills**, mapping the mcap
trajectory and position size onto realised ROI. Out-of-sample (5-fold): correlation 0.692,
R2 0.473, mean predicted ROI +14.64% against +14.41% actual, unbiased on the mean, which is what
portfolio P&L depends on. It is slightly optimistic at the median (+8.6% vs +6.6%); we report that.

We also fitted the bonding curve from first principles (recovering 34.0 SOL / 1.024x109 virtual
reserves against the documented 30 / 1.073x109), but only 41% of fills land within 10% of its
prediction, precisely because the bot enters 118 transactions late, after unobserved buyers moved
the curve. The empirical model is the honest choice.

---

## Part 2: What it decides on

### The `t_decision` constraint, enforced twice

Every input feature is extracted from the deployment transaction itself or from strictly prior
deployments. Two independent guards:

- **By construction.** Deployer aggregates are cumulative, ordered by slot, in a
  `RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING` window: bounded to strictly earlier slots.
  We first wrote `ROWS`, which let two deployments by the same wallet **in the same slot** see each
  other; the audit below caught it.
- **By recount.** `audit()` samples deployments, recomputes their history by brute force with a
  `slot < current_slot` join, and compares. Any divergence, or any row at or after `t_decision`,
  raises and halts the pipeline. On the census it passes on 159 sampled deployments, zero
  discrepancies, zero rows at or after the current slot.

`pumpfun_trades.parquet` and `mcap_candles.parquet` are used **only** for outcome labels and
backtest P&L, never as decision inputs.

### Data reality, stated plainly

The archive server ignores `Range`, so a drop restarts from byte 0; across 34 hours it never
delivered more than 3.3 of the 15 GiB negative member. Extracting **in stream** (`curl | tar -xf -`)
puts each completed member on disk before the next drop. It then landed whole (**14,955,899,885
bytes, byte-exact against the tar header**), parsing to **5,059,880 transactions, 0 failures.**

The dataset is therefore not a sample. It is the census:

| | Rows |
|---|---|
| Deployments, 1 Jan - 30 Jun 2026 | 5,075,807 |
| Distinct deployer wallets | 1,005,867 |
| Restricted to the target wallet's active life (12 Mar - 30 Jun) | 3,006,792 |
| of which bought by the bot | 15,924 (**0.5296%**) |

Three earlier caveats disappear with it: no case-control sampling and so no reweighting; no
time-matching, so `hour` and `dow` are legitimate features rather than a sampling fingerprint; and
the observed base rate *is* the true one. Train is 12 Mar - 31 May (2,158,906 rows, 11,729
positives); test is **the whole of June, never seen** (847,886 rows, 4,195 positives).

One member is still missing and we exclude it rather than use it one-sided: the deployer
**activity** history. Its negative half never downloaded, and using the positives' alone would be
gross leakage: the model would learn that merely having a history implies a buy.

### The deployer is the signal, not the token

Because the two classes together are *every* pump.fun deployment of the period, each deployer's
history rebuilds from the dataset itself, symmetrically, with no external source. For any deployment
we ask, strictly before `t_decision`: how many tokens has this wallet launched, how fast, how much
of its own money did it stake, and how many did the bot already take, at what notch. The family
splits in two, and we keep the halves apart on purpose.

**What the bot already thought of this deployer.** These reference the target wallet's own past
buys: public on-chain before `t_decision`, so legal inputs, but open to a charge of circularity.

> **74.3% of buys come from a deployer the bot had already bought before, against 12.0% of the
> deployments it ignored**, a x6.2 lift from a single binary. Requiring five prior buys sharpens
> it to 50.7% against 4.9%, x10.4.

**What the deployer is, independent of the bot.** These reference nobody's decisions, only the
wallet's own behaviour, and they are nearly as strong:

> **The bot avoids spammers.** A bought deployer launches a median **1.85 tokens per day**; an
> ignored one launches **9.89**. Time since that wallet's previous launch separates the classes at
> AUC 0.804: median **6,513 s** for bought against **88 s** for ignored.

Adding both families moved PR-AUC from 0.056 to **0.210**. Removing the eight bot-referencing
variables entirely still leaves **0.102**, so roughly half the predictive power owes nothing to
the bot's own record, and the model is not merely echoing it back.

### Ablation: what each family is worth

PR-AUC lost when a family is removed, and PR-AUC achieved by each family alone (baseline 0.2085 at
the interpretability model's smaller hyperparameters):

| Family | Loss if removed | Alone |
|---|---|---|
| **Deployer reputation** (bot's past buys: count, notch, recency) | **-32.6%** | **0.1012** |
| **Deployer career** (cadence, span, past stake, bot-independent) | **-19.6%** | **0.0822** |
| Deployment economics | -4.9% | 0.0281 |
| Token metadata | -0.9% | 0.0131 |
| Bot's past verdict (count, share) | **+0.8%** | 0.0504 |
| Priority / fees | **+1.7%** | 0.0192 |

Two readings a single-family table would get wrong. First, **the two bot-referencing families
overlap**. Dropping "bot's past verdict" alone costs nothing because "deployer reputation" already
carries it, so the table *understates* the bot's record. The honest test removes both at once: 0.2100 falls to 0.1017,
a **51% loss**. Second, **career beats economics four to one**. Who the deployer is
(0.0822 alone) outweighs what the transaction spends (0.0281), and career features reference
nobody's decisions, so no charge of circularity touches them.

Priority/fee and transaction-shape features make the model *worse*; backward selection drops them.

### Four interpretability checks

- **SHAP** (top 5 by mean |value|, signed effect bracketed): `cu_consumed` 0.883 [-0.355],
  `h_since_prev_deploy_s` 0.584 [-0.131], `h_n_prev_deploys` 0.467 [-0.077], `h_deploys_per_day`
  0.366 [+0.009], `h_share_prev_bought` 0.236 [-0.008]. Three of the top four describe launch
  behaviour.
- **Monotonic constraints, and a result of ours that reversed.** On the sampled panel, forcing
  "more dev-buy implies more likely bought" cost **13% of PR-AUC**, and we published that the
  boundary is *not* monotone. On the census the same constraint **gains 1%** (0.2085 to 0.2099): the earlier
  finding was an artefact of 10,638 rows. The constrained model is the one to prefer, equal
  accuracy, auditable rule by rule.
- **Calibration.** Score and realised rate rise monotonically across all ten deciles, and the mean
  score is **0.0050 against a true rate of 0.00495**. The score is already a probability; the logit
  prior shift our previous version needed existed only to undo case-control sampling.
- **Ordinal model on the notch.** Predicting *conviction* rather than the binary decision gives
  Spearman rho = **0.460** out of sample on 3,771 held-out positives; the top predicted quartile
  has a true median notch of **97.5** against **55.2** for the bottom. It is driven by the same career
  signals as the binary decision: evidence the ladder is one underlying score, not a separate
  mechanism.

### The separating signals

Univariate separating power on the full training census, ranked by AUC, no hyperparameter
choices involved:

| Feature | AUC | Median (bought) | Median (not bought) |
|---|---|---|---|
| `h_share_prev_bought` | **0.867** | 5.26% | 0% |
| `h_prev_notch_max` | **0.853** | notch 60 | never bought |
| `h_prev_notch_mean` | 0.852 | notch 45 | never bought |
| `h_n_prev_bought_by_bot` | 0.845 | 5 | 0 |
| `h_since_prev_deploy_s` | **0.812** | 6,120 s | 104 s |
| `h_since_last_bought_s` | 0.794 | 4,980 s | never bought |
| `h_prev_devbuy_mean_sol` | 0.762 | 3.269 SOL | 0.437 SOL |
| `h_n_bought_24h` | 0.759 | 1 | 0 |
| `dev_buy_tokens` | 0.742 | 9.16e13 | 1.07e13 |
| `h_prev_devbuy_max_sol` | 0.736 | 9.319 SOL | 1.650 SOL |
| `dev_buy_lamports` | 0.733 | 3.038 SOL | 0.550 SOL |

**This overturns our own earlier reading.** On the sampled panel `dev_buy_lamports` topped the table
at AUC 0.712 and we called the bot a reader of "money and tooling". On the census it ranks eleventh.
The dev-buy still matters, 3.04 SOL against 0.55, but what the *deployer's history* says dominates
what the transaction says about itself.

### The rules, in plain language

A depth-3 tree on the census gives a human-readable boundary. Its root split is not a property of
the token at all: it is **"has the bot bought this deployer before, at a rate above 0.2%"**. The
dominant positive leaf (class-balanced weights 19,995 negative / 675,889 positive) reads:

> **The bot buys when the deployer has a track record with it, has waited more than ~90 seconds
> since its own previous launch, and was last bought within ~3 days.**

And the dominant negative leaf (877,359 / 102,617) is its mirror:

> **It ignores deployments from a wallet it has no record with, that fired its previous launch less
> than ~92 minutes ago, and that it has bought nothing from in the last 24 hours.**

For a deployer it has no record with, one branch still opens: waiting more than ~92 minutes since
the last launch *and* staking more than **0.555 SOL** (58,833 / 123,048). That is how a new wallet
earns a first buy: patience plus stake.

In words: **the bot underwrites reputations, not tokens, and treats launch cadence as a
disqualifier.** A wallet firing a token every few minutes is spam; one that waits and puts several
SOL behind each launch is a business. Notably **absent**: name and ticker shape, socials, vanity
mint address, time of day.

### Classification quality

Trained on 12 Mar - 31 May (2,158,906 deployments, 11,729 bought). Tested on **the whole of June**
(847,886 deployments, 4,195 bought). Every number below is measured at the true base rate of
**0.4948%**: no reweighting, no matching, no resampling of any kind.

| Metric | Full model | Without the 8 bot-referencing features |
|---|---|---|
| PR-AUC | **0.2100** (x42 over chance) | 0.1017 (**-51.5%**) |
| ROC-AUC | 0.9504 | |
| Best F1 | **0.3294** (precision 0.274, recall 0.413) | |
| Precision at 10% recall | **0.3801** (**x77 lift**) | |
| Precision at 25% recall | 0.3356 (x68) | |
| Precision at 50% recall | 0.2253 (x46) | |

At 10% recall the model is right **more than one time in three**, on a population where 1
deployment in 202 is bought.

**A number we published earlier was wrong, and we say which way.** An early version reported PR-AUC
0.276 with `hour` and `dow` as its top two features: a harvested batch covers ~15 minutes of one
moment while positives spanned all of June, so the model had learned *which slots we sampled*.
Time-matching removed that confound and dropped the honest figure to 0.056. The census removes it by
construction: negatives now cover every slot. The progression **0.056 to 0.148 to 0.210** is a
data-volume story, not a modelling one.

---

## Part 3: Replica

Entry rule: score the token with the Part 2 model at `t_decision`, take the top *N*. Position size
fixed at 2.469 SOL, the bot's own median, so the comparison is like-for-like.

Out-of-sample universe: **760,992 June tokens with measurable outcomes, of which the bot took
4,195.** Intervals are bootstrap over trades.

| Selectivity | Entries | Median ROI | Hit rate | Total P&L | Max DD | Recall of the bot | Precision |
|---|---|---|---|---|---|---|---|
| Top 0.1% | 761 | **+12.60%** [+10.7, +15.3] | 64.7% | +201 SOL | -2.9 SOL | 6.8% | **37.5%** |
| Top 0.5% | 3,805 | **+11.93%** [+11.2, +13.1] | 64.2% | +1,041 SOL | -3.0 SOL | 29.0% | 32.0% |
| Top 1% | 7,610 | +9.73% [+9.2, +10.2] | 59.5% | +1,731 SOL | -8.1 SOL | **45.8%** | 25.3% |
| *Target bot, June* | *4,226* | *+6.49%* | *64.6%* | *+31,706 SOL* | *-16.8 SOL* | | |

The replica beats the bot's median ROI **at every selectivity**, at a comparable hit rate, and now
at scale: at top-1% it finds **45.8% of everything the bot bought in June**, against 2.5% before.
That earlier 100%-precision result was not a better model; it was a model with 27 trades.

**Where the bot still wins.** Per SOL deployed it returns **+34.0%** in June against our 14.8%: it
concentrates into its own tail far harder than we allow ourselves. We beat it per trade, not per
SOL; the notch sizing below closes part of that gap.

**Reflexivity, measured.** At top-0.5% the picks split into 1,217 the bot also took (+13.32%,
68.2% hit) and 2,588 it ignored (+11.30%, 62.4%): its flow is worth ~**2 ROI points**.

### Sizing the ladder, not just picking the token

Our replica bet a flat 2.469 SOL, throwing the ladder away. Regressing the notch on the same
`t_decision` features (train positives only, Spearman 0.460) and **redistributing identical
capital** across identical trades gives:

| Top 0.5%, 3,805 trades | P&L | P&L per SOL | Max DD | Median ROI |
|---|---|---|---|---|
| Flat 2.469 SOL | +1,041 SOL | 11.08% | -3.0 SOL | +11.93% |
| **Sized by predicted notch** | **+1,390 SOL** | **14.80%** | -2.9 SOL | +11.82% |

**+34% P&L at identical capital.** Median ROI does *not* improve (-0.11%) and hit rate
falls to 61.0%: an allocation gain, not a selection gain, and the execution model charges for the
larger positions. At top-1% the move is +40% P&L, drawdown 8.1 to 6.2 SOL.

### The delay row, stated precisely

Our finest price granularity is the **1-second** candle, while a slot is ~400 ms. The "+1 slot"
rows therefore re-price entry a full second late, roughly **2.5 slots**: a conservative upper
bound, not the literal 1-slot penalty. On that basis +11.93% becomes **-18.43%**, hit rate
collapsing to **1.5%**. The true 1-slot cost sits between the zero-block result and this figure;
we cannot resolve below one second. Any replica reporting healthy returns a slot late has not
modelled execution.

---

## What we would do next

- **Sub-second execution data.** The 1-second candle floor is what stops us pricing a literal 1-slot
  delay. Per-slot bonding-curve state would turn our conservative upper bound into a real number,
  and it is the single measurement that would most sharpen Part 3.
- **The deployer activity member**, the one dataset we never obtained. "Has this wallet ever claimed
  creator fees" should be predictive, since 34.8% of the bot's picks come from deployers who had,
  but we measured that on positives only and will not model on it until the negative half exists.
- **Deployer-level cross-validation.** Our split is temporal, the right primary guard, but a wallet
  can appear in both folds, so part of the career signal is memorised identity. Grouping folds by
  deployer would separate the two, and would lower the headline.
