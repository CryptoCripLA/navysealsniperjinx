"""Builds the public Kaggle notebook that reproduces all three parts."""
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cells = []


def _lines(t):
    """nbformat expects a list of lines each ending with a newline, except the
    last one. Without this Jupyter concatenates the whole cell onto one line."""
    ls = t.strip("\n").split("\n")
    return [l + "\n" for l in ls[:-1]] + [ls[-1]]


def md(t):
    cells.append({"cell_type": "markdown", "metadata": {}, "source": _lines(t)})


def code(t):
    cells.append({"cell_type": "code", "execution_count": None, "metadata": {},
                  "outputs": [], "source": _lines(t)})


md("""
# A sniper that bets in notches

Reverse-engineering `5brv79eFZ2rGprXNvqgVJBkBptkkw8GJX1XydJyZLyAr`, a Solana wallet that buys
pump.fun tokens **in the deployment slot itself**.

This notebook reproduces every number in the write-up end to end: feature computation, the
`t_decision` leakage audit, model training, and the backtest.

**Three findings drive the analysis:**

1. **Position size is a discrete confidence score.** 94.6% of entries are exact integer multiples
   of 4/81 SOL, and the notch predicts the outcome monotonically (55.6% to 79.3% hit rate).
2. **It is not in the deployment bundle.** Median position is +118 transactions after the create
   transaction, inside the same block. Never before it.
3. **Market-cap backtests overstate ROI by 7x to 17x.** The bot's real median ROI is +6.6%; exiting
   at the 10-second high would show +109%. Execution, not selection, explains the gap.
""")

code("""
import os, numpy as np, pandas as pd, joblib
import matplotlib.pyplot as plt

# The derived tables are published as a Kaggle Dataset. The full pipeline that
# produces them (download, raw transaction parsing, as-of joins) lives in the
# repository linked from the write-up.
BASE = "/kaggle/input/solana-sniper-derived"
if not os.path.isdir(BASE):
    BASE = os.path.join("..", "kaggle_dataset")       # local run from notebooks/
if not os.path.isdir(BASE):
    BASE = "kaggle_dataset"                           # local run from the repo root

P = lambda f: os.path.join(BASE, f)
trades = pd.read_parquet(P("bot_trades.parquet"))
pos    = pd.read_parquet(P("bought_features.parquet"))
out    = pd.read_parquet(P("june_outcomes.parquet"))
print(f"bot trades        : {len(trades):,}")
print(f"bought deployments: {len(pos):,}")
print(f"June tokens with outcomes: {len(out):,}")
""")

md("""
---
## Part 1: Behaviour

Entry size, latency, in-block position, hold time, exit structure, hit rate, P&L distribution.
""")

code("""
BASE_UNIT = 4/81      # 0.0493827 SOL

e = trades.entry_sol.dropna(); e = e[e > 0]
u = e / BASE_UNIT
exact = (np.abs(u - np.round(u)) < 1e-6).mean()
print(f"entry size: mean {e.mean():.2f} SOL | median {e.median():.2f} | sd {e.std():.1f} | max {e.max():,.0f}")
print(f"entries that are exact multiples of 4/81 SOL: {exact:.1%}")
print(f"median deviation from the integer: {np.median(np.abs(u - np.round(u))):.2e}")

# The rubric asks for latency in SLOTS and in SECONDS.
ls = trades.latency_slots.dropna(); lsec = trades.latency_s.dropna()
print(f"latency: median {lsec.median():.0f} s / {ls.median():.0f} slots | mean {lsec.mean():.1f} s")
print(f"\\nzero-block share: {(ls==0).mean():.2%} | within 1 slot: {(ls<=1).mean():.2%}")
ib = trades.inblock_delta.dropna()
print(f"median in-block position: +{ib.median():.0f} tx | within 5 tx: {(ib<=5).mean():.2%}"
      f" | before the deploy: {(ib<0).mean():.2%}")
print(f"\\nmedian hold: {trades.hold_s.median():.0f} s | sells per token: {trades.n_sells.median():.0f}"
      f" | positions closed: {trades.closed.mean():.0%}")
print(f"hit rate: {trades.is_win.mean():.2%} | overall ROI: {trades.pnl_sol.sum()/trades.sol_in.sum():.2%}")

# Use of burn, explicitly required by the rubric under exit structure.
act = pd.read_parquet(P("wallet_activity.parquet"), columns=["event_type"])
nb_burn = int((act.event_type == "burn").sum())
print(f"wallet actions: {len(act):,} | burn events: {nb_burn} ({nb_burn/len(act):.4%}) -> it essentially never burns")
""")

code("""
# The conviction ladder: the notch predicts the outcome
t = trades.assign(lv=np.round(trades.entry_sol / BASE_UNIT)).dropna(subset=["lv"])
bins = pd.cut(t.lv, [0, 35, 45, 60, 80, 150, 1e9])
print(t.groupby(bins, observed=True).agg(
    trades=("roi", "size"), hit_rate=("is_win", "mean"),
    roi_median=("roi", "median"), pnl_moyen=("pnl_sol", "mean")).round(3).to_string())
""")

code("""
# P&L concentration: a single trade is 12% of the total.
# Sort the ROWS, not just the P&L column: sorting one series and then indexing
# another column in its original order mixes two different trades into the
# numerator and denominator (that bug produced 9.33%).
o = trades.sort_values("pnl_sol", ascending=False)
tot = o.pnl_sol.sum()
for k in [1, 10, 50, int(len(o)*0.01)]:
    print(f"  top {k:>4} trades = {o.pnl_sol.head(k).sum()/tot:6.1%} of total P&L")
rest = o.iloc[int(len(o)*0.01):]
print(f"\\noverall ROI without the top 1%: {rest.pnl_sol.sum()/rest.sol_in.sum():.2%}"
      f"  (with: {tot/trades.sol_in.sum():.2%})")
""")

md("""
### Selection lift against the June base rate

The bot's picks against all 764,451 June tokens.
""")

code("""
o = out[out.mcap_entry > 0].copy()
june = (trades.first_buy_ts >= 1780272000) & (trades.first_buy_ts < 1782864000)
o["achete"] = o.mint.isin(set(trades.loc[june, "token_address"])).astype(int)
for lab, f in [("x1.5 en 5s",  lambda d: d.mcap_max_5s  > d.mcap_entry*1.5),
               ("x2 en 10s",   lambda d: d.mcap_max_10s > d.mcap_entry*2),
               ("x3 en 30s",   lambda d: d.mcap_max_30s > d.mcap_entry*3)]:
    b, n = f(o[o.achete==1]).mean(), f(o[o.achete==0]).mean()
    print(f"  {lab:<12} bot {b:6.2%}  vs base {n:6.2%}   lift x{b/n:.2f}")
""")

md("""
---
## The `t_decision` constraint

> **Disqualifying condition:** any feature computed from data after the deployment slot scores zero
> on Parts 2 and 3.

Two independent guards. First, **by construction**: deployer aggregates use a
`RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING` window ordered by slot, bounded to *strictly*
earlier slots. We first wrote `ROWS`, which let two deployments by the same wallet **in the same
slot** see each other; the audit below caught it.

Second, **by recount**: we sample deployments, recompute their history by brute force with a
`slot < current_slot` join, and compare. Any divergence halts the pipeline.
""")

code("""
# THE LEAKAGE AUDIT, replayed on the FULL CENSUS (3M deployments).
import duckdb
con = duckdb.connect(); con.execute("SET threads=4; SET memory_limit='4GB';")
src = P("census_dataset.parquet").replace("\\\\", "/")

q = f'''
WITH allm AS (SELECT mint, creator AS dep, slot, block_time FROM read_parquet('{src}')
              WHERE creator IS NOT NULL),
cum AS (
  SELECT mint, dep, slot, block_time,
         COUNT(*) OVER w AS h_prev_deploys
  FROM allm
  WINDOW w AS (PARTITION BY dep ORDER BY slot
               RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)),
s AS (SELECT * FROM cum WHERE h_prev_deploys > 0 USING SAMPLE 200 ROWS),
rc AS (
  SELECT s.mint, s.dep, s.slot, s.h_prev_deploys,
         COUNT(*) AS vrai_n,
         COUNT(*) FILTER (WHERE a.slot >= s.slot) AS future_rows
  FROM s JOIN allm a ON a.dep = s.dep AND a.slot < s.slot
  GROUP BY 1,2,3,4)
SELECT COUNT(*) AS verifies,
       SUM(CASE WHEN h_prev_deploys <> vrai_n THEN 1 ELSE 0 END) AS incoherents,
       SUM(future_rows) AS future_rows
FROM rc;
'''
n, bad, fut = con.execute(q).fetchone()
print(f"[AUDIT] {n} deployments recounted by brute force")
print(f"  inconsistent histories       : {bad}")
print(f"  rows at or after t_decision  : {fut}")
assert bad == 0 and fut == 0, "TEMPORAL LEAK DETECTED"
print("  -> OK: no aggregate contains data from after t_decision")
""")

md("""
---
## Part 2: What it decides on

### A census, not a sample

The 41.5 GB core archive is served by a Python `SimpleHTTP` that **ignores `Range` headers**: no
resume, and it dropped every 1 to 4 hours. The fix was to stop writing a monolithic `.tar` and
**extract in stream** (`curl | tar -xf -`), so each completed member survives the next drop. The
negative-class member then landed whole, 14,955,899,885 bytes, byte-exact against the tar header, 
and parses to **5,059,880 deployment transactions, 0 failures**.

So there is **no sampling here at all**: no case-control design, no reweighting, no time matching.
Train is 12 Mar to 31 May; test is **the whole of June, never seen**, at its true base rate.

An earlier version of this work reported PR-AUC 0.276 with `hour` and `dow` as its top two features
,  a harvested batch covered ~15 minutes of one moment while positives spanned all of June, so the
model had learned *which slots we sampled*. With every slot present, that artefact cannot exist.
""")

code("""
d = pd.read_parquet(P("census_dataset.parquet"))
d["uri_host_cat"] = d.uri_host_cat.astype("category")
TRAIN_END = 1780272000          # 2026-06-01

tr, te = d[d.block_time < TRAIN_END], d[d.block_time >= TRAIN_END]
print(f"universe over the bot's active life: {len(d):,} deployments, "
      f"{d.label.sum():,} achetes ({d.label.mean():.4%})")
print(f"train 12 mars - 31 mai : {len(tr):,} ({tr.label.sum():,} pos, {tr.label.mean():.4%})")
print(f"test  juin entier      : {len(te):,} ({te.label.sum():,} pos, {te.label.mean():.4%})")
""")

code("""
# Deux familles historiques, gardees separees a dessein.
REPUT = ["h_n_prev_bought","h_since_last_bought_s","h_bot_knows_deployer",
         "h_prev_notch_max","h_prev_notch_mean","h_n_bought_24h"]
VERDICT = ["h_n_prev_bought_by_bot","h_share_prev_bought"]          # references the bot
CARRIERE = ["h_n_prev_deploys","h_prev_devbuy_sum_sol","h_prev_devbuy_max_sol",
            "h_prev_devbuy_mean_sol","h_since_prev_deploy_s","h_career_span_s",
            "h_deploys_per_day"]                                     # references nobody
NUM = ["dev_buy_lamports","dev_buy_tokens","signer_pre_lamports","signer_spent_lamports",
       "fee","cu_consumed","cu_limit","cu_price","jito_tip_lamports","is_bundled",
       "name_len","symbol_len","name_eq_symbol","name_is_upper","name_has_digit",
       "name_has_space","name_non_ascii","name_n_words",
       "log_dev_buy","log_signer_pre","log_cu_price","dev_buy_over_balance","hour","dow"]

FEATS = [c for c in NUM + REPUT + VERDICT + CARRIERE if c in d.columns] + ["uri_host_cat"]
BOTREF = set(REPUT) | set(VERDICT)
FEATS_NOBOT = [f for f in FEATS if f not in BOTREF]
print(f"{len(FEATS)} features, of which {len(BOTREF & set(FEATS))} reference the bot's own past buys")
""")

code("""
import lightgbm as lgb
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score

def fit_eval(fs):
    m = lgb.LGBMClassifier(n_estimators=900, learning_rate=0.05, num_leaves=63,
                           min_child_samples=200, subsample=0.85, subsample_freq=1,
                           colsample_bytree=0.85, reg_lambda=2.0, random_state=0, verbose=-1)
    num = [c for c in fs if c != "uri_host_cat"]
    X = tr[fs].copy(); X[num] = X[num].astype("float32")
    m.fit(X, tr.label, categorical_feature=["uri_host_cat"])
    X = te[fs].copy(); X[num] = X[num].astype("float32")
    return m, m.predict_proba(X)[:,1]

m, score  = fit_eval(FEATS)
_, sc_nb  = fit_eval(FEATS_NOBOT)

base = te.label.mean()
ap   = average_precision_score(te.label, score)
prec, rec, _ = precision_recall_curve(te.label, score)
f1 = 2*prec*rec/np.clip(prec+rec, 1e-12, None); i = int(np.nanargmax(f1))
print(f"true base rate (no reweighting): {base:.4%}")
print(f"PR-AUC  : {ap:.4f}   (lift x{ap/base:.0f} over chance)")
print(f"ROC-AUC : {roc_auc_score(te.label, score):.4f}")
print(f"best F1 : {f1[i]:.4f} (precision {prec[i]:.4f}, recall {rec[i]:.4f})")
for t in [0.10, 0.25, 0.50]:
    j = int(np.argmin(np.abs(rec-t)))
    print(f"  precision at {t:.0%} recall: {prec[j]:.4f}  (lift x{prec[j]/base:.0f})")

ap_nb = average_precision_score(te.label, sc_nb)
print(f"\\nWITHOUT the {len(BOTREF & set(FEATS))} features referencing the bot: "
      f"PR-AUC {ap_nb:.4f} ({ap_nb/ap-1:+.1%})")
print("-> about half the predictive power owes nothing to circularity")
""")

md("""
### The rules, in plain language

Univariate AUC involves no hyperparameter choice, so we report it alongside the model's importances.
""")

code("""
from sklearn.metrics import roc_auc_score
rows = []
for f in [c for c in FEATS if c != "uri_host_cat"]:
    x = tr[f].astype(float).replace([np.inf,-np.inf], np.nan)
    if x.notna().sum() < 100 or x.nunique() < 3: continue
    a = roc_auc_score(tr.label[x.notna()], x[x.notna()])
    rows.append((f, a, max(a, 1-a), tr.loc[tr.label==1,f].median(), tr.loc[tr.label==0,f].median()))
u = pd.DataFrame(rows, columns=["feature","auc","separation","median_bought","median_not"])
print(u.sort_values("separation", ascending=False).head(10).to_string(index=False))
""")

code("""
from sklearn.tree import DecisionTreeClassifier, export_text
num = [c for c in FEATS if c != "uri_host_cat"]
X = tr[num].astype(float).replace([np.inf,-np.inf], np.nan).fillna(-1)
t3 = DecisionTreeClassifier(max_depth=3, min_samples_leaf=80,
                            class_weight="balanced", random_state=0).fit(X, tr.label)
print(export_text(t3, feature_names=num, decimals=3, show_weights=True))
""")

md("""
**The root split is not a property of the token.** It is *"has the bot bought this deployer before,
at a rate above 0.2%"*. The dominant positive leaf reads:

> the deployer **has a track record** with the bot, **waited more than ~90 s** since its own previous
> launch, and **was last bought within ~3 days**.

Its mirror, the dominant negative leaf: a wallet the bot has **no record with**, that fired its
previous launch **less than ~92 minutes ago**, and that it has bought nothing from in 24 hours.
For an unknown wallet one branch still opens, wait more than ~92 minutes **and** stake more than
**0.555 SOL** of your own money.

In words: **the bot underwrites reputations, not tokens, and treats launch cadence as a
disqualifier.** A wallet firing a token every few minutes is spam; one that waits and puts several
SOL behind each launch is a business.

This **overturns our own earlier reading**. On the sampled panel `dev_buy_lamports` topped the
univariate table at AUC 0.712 and we called the bot a reader of "money and tooling". On the census
it ranks eleventh, the dev-buy still matters (3.04 SOL vs 0.55) but the deployer's history
dominates what the transaction says about itself.
""")

md("""
---
## Part 3: Replica and backtest

### Why the P&L is not computed from market caps

Candles give the *marginal* price. Selling ~5% of supply into a bonding curve realises far less.
Measured on the bot's own June trades, a naive mcap-ratio backtest overstates median ROI by
**7.2x (10-second close)** to **16.6x (10-second high)**.

We therefore use an execution model fitted on the bot's **4,195 real June fills**, out-of-sample
correlation 0.692, R2 0.473, mean predicted ROI +14.64% against +14.41% actual.
""")

code("""
exec_model, exec_cols = joblib.load(P("execution_model.pkl"))

GAS, PRIO = 0.097, 0.050            # medians actually paid by the bot
COST = GAS + PRIO

def exec_features(df, delay_s=0):
    ref = df[f"mcap_close_{delay_s}s"].where(df[f"mcap_close_{delay_s}s"] > 0, df.mcap_entry) \\
          if delay_s else df.mcap_entry
    X = pd.DataFrame(index=df.index)
    for h in [3,5,10,30,60]:
        X[f"mx{h}"] = np.log(df[f"mcap_max_{h}s"]/ref)
        X[f"cl{h}"] = np.log(df[f"mcap_close_{h}s"]/ref)
    X["lv10"] = np.log1p(df.vol_10s); X["lv60"] = np.log1p(df.vol_60s)
    X["lsize"] = np.log(df.size_sol); X["lnc"] = np.log1p(df.n_candles)
    return X.replace([np.inf,-np.inf], np.nan)

def simulate(df, delay_s=0):
    X = exec_features(df, delay_s)
    ok = X[exec_cols].notna().all(axis=1)
    roi = pd.Series(np.nan, index=df.index)
    roi.loc[ok] = exec_model.predict(X.loc[ok, exec_cols])
    roi = roi.clip(-1, 5)
    return roi, roi*df.size_sol - COST

def boot_ci(x, n=2000, seed=0):
    r = np.random.default_rng(seed); x = np.asarray(x)
    if len(x) < 10: return (np.nan, np.nan)
    s = [np.median(r.choice(x, len(x), True)) for _ in range(n)]
    return np.percentile(s, 2.5), np.percentile(s, 97.5)
""")

code("""
te2 = te.copy(); te2["score"] = score
bt = te2.merge(out[out.mcap_entry>0], on="mint", how="inner")
bt["size_sol"] = 2.469                      # the bot's own median, for a like-for-like comparison
print(f"out-of-sample test universe: {len(bt):,} June tokens, "
      f"{bt.label.sum():,} taken by the bot\\n")

def row(lab, df, delay=0):
    roi, pnl = simulate(df, delay)
    v = pnl.notna(); roi, pnl = roi[v], pnl[v]
    if not len(roi): return
    eq = pnl.cumsum().values
    lo, hi = boot_ci(roi.values)
    dd = float((eq - np.maximum.accumulate(eq)).min())
    print(f"  {lab:<34} n={len(roi):>5} | ROI med {roi.median():+7.2%} "
          f"[{lo:+.2%}, {hi:+.2%}] | hit {(pnl>0).mean():6.2%} | "
          f"P&L {pnl.sum():+8.1f} SOL | DD {dd:7.1f}")

for q in [0.999, 0.995, 0.99]:
    sel = bt[bt.score >= bt.score.quantile(q)]
    print(f"=== top {(1-q)*100:.1f}%: {len(sel):,} entries ===")
    row("zero-block entry", sel, 0)
    row("1 slot late", sel, 1)
    row("2 slots late", sel, 2)
    # Reflexivity: the execution model is calibrated on tokens carried by the
    # bot's own flow, so we split instead of assuming it makes no difference.
    row("  of which the bot also took", sel[sel.label==1], 0)
    row("  of which the bot ignored (extrapolation)", sel[sel.label==0], 0)
    print(f"  -> overlap: recall {sel.label.sum()/max(bt.label.sum(),1):.2%} "
          f"| precision {sel.label.mean():.2%}\\n")

print("reference: the bot returns +6.6% median ROI and 64.7% hit rate over the period")
""")

md("""
### Sizing the ladder, not just picking the token

The bot grades its conviction in notches, and the notch predicts its own hit rate (55.6% to 79.3%).
A replica betting a flat 2.469 SOL throws that away. We regress the notch on the **same
`t_decision` features**, train-period positives only, then redistribute the **identical capital**
across the **identical trades**.

This is not free money: the execution model takes `log(size)` as an input, so larger positions get
worse fills. The net effect has to be measured.
""")

code("""
import lightgbm as lgb
BASE_UNIT = 4/81

# Le regresseur de cran ne voit que les positifs de la periode d'entrainement.
notch = (trades.set_index("token_address").entry_sol / BASE_UNIT).round()
ptr = tr[tr.label == 1].copy()
ptr["notch"] = ptr.mint.map(notch)
ptr = ptr[ptr.notch.between(1, 400)]
reg = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.05, num_leaves=31,
                        min_child_samples=40, subsample=0.85, subsample_freq=1,
                        random_state=0, verbose=-1)
reg.fit(ptr[FEATS], np.log1p(ptr.notch), categorical_feature=["uri_host_cat"])
print(f"regresseur de cran entraine sur {len(ptr):,} positifs (train seul)")

bt["pred_notch"] = np.expm1(reg.predict(bt[FEATS])).clip(24, 120)

def bilan(lab, df):
    roi, pnl = simulate(df, 0)
    v = pnl.notna(); roi, pnl = roi[v], pnl[v]
    eq = pnl.cumsum().values
    dd = float((eq - np.maximum.accumulate(eq)).min())
    cap = df.size_sol[v].sum()
    print(f"  {lab:<28} ROI med {roi.median():+7.2%} | hit {(pnl>0).mean():6.2%} | "
          f"P&L {pnl.sum():+8.1f} SOL | capital {cap:8.1f} | "
          f"P&L/capital {pnl.sum()/cap:+6.2%} | DD {dd:6.1f}")

for q in [0.995, 0.99]:
    sel = bt[bt.score >= bt.score.quantile(q)].copy()
    print(f"=== top {(1-q)*100:.1f}% : {len(sel):,} entrees ===")
    a = sel.copy(); a["size_sol"] = 2.469
    bilan("taille fixe 2.469 SOL", a)
    b = sel.copy()
    b["size_sol"] = (2.469 * b.pred_notch / b.pred_notch.mean()).clip(0.5, 8.0)
    bilan("cran predit, capital egal", b)
    print()
""")

md("""
### What the delay row means

The replica beats the bot's **+6.6%** median ROI at every selectivity, at a comparable hit rate and
a drawdown under 6 SOL, and at top-1% it recovers **45.8% of everything the bot bought in June**.

**Read the delay rows precisely.** The finest price granularity available is the **1-second**
candle, while a Solana slot is ~400 ms. The rows labelled "+1 slot" therefore re-price entry a full
second late, roughly **2.5 slots**: a deliberately conservative upper bound, not the literal 1-slot
penalty. On that basis +11.9% becomes **-18.4%** and the hit rate collapses to **1.5%**. The true
1-slot cost sits between the zero-block result and this figure; we cannot resolve below one second
and do not pretend to. The direction is unambiguous: the edge lives in the zero block, and any
replica reporting healthy returns a slot late has not modelled execution.

### Honest limits

- **No deployer *activity* history, either class.** That member never finished downloading, and a
  second extraction attempt truncated the half we did have. Using the positives' activity alone
  would be gross leakage, so the whole family is excluded.
- **Reflexivity.** The execution model is calibrated on tokens the bot bought, carried by its own
  flow and its followers'. Applying it to tokens it skipped is an extrapolation, which is why the
  cell above reports the two groups separately. The gap is about 3 ROI points.
- **Deployer identity across folds.** The split is temporal, the right primary guard, but a wallet
  can appear in both train and test, so part of the career signal is memorised identity rather than
  learned behaviour. Grouping folds by deployer would lower the headline; we have not done it.
""")

nb = {"cells": cells,
      "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                   "language_info": {"name": "python", "version": "3.11"}},
      "nbformat": 4, "nbformat_minor": 5}

out_path = os.path.join(ROOT, "notebooks", "solana_sniper_reverse_engineering.ipynb")
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(nb, f, ensure_ascii=False, indent=1)
print(f"{len(cells)} cellules -> {out_path}")
