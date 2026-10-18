"""Partie 3, amelioration : miser le CRAN predit plutot qu'une taille fixe.

Le bot ne mise pas au hasard : son cran (multiple de 4/81 SOL) predit son propre
hit rate de 55.6% a 79.3%. Notre replique, elle, misait 2.469 SOL sur chaque
entree — la mediane du bot — ce qui jette l'information de conviction.

On entraine donc un regresseur de cran sur les positifs de la periode
d'entrainement, avec les MEMES variables t_decision, et on dimensionne chaque
entree sur le cran predit.

Ce n'est pas de l'argent gratuit : le modele d'execution prend log(size) en
entree, donc miser plus degrade le prix moyen obtenu. Le gain net doit etre
mesure, pas suppose.

Usage: python src/part3_sizing.py
"""
import os, sys
import numpy as np
import pandas as pd
import joblib
import lightgbm as lgb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERIM = os.path.join(ROOT, "data", "interim")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from part3_backtest import simulate, bootstrap_ci, max_drawdown, COST_SOL

BASE_UNIT = 4 / 81
FLAT = 2.469                 # mediane du bot, notre reference actuelle
# Bande de crans reellement operee par le bot : on ne l'extrapole pas.
NOTCH_LO, NOTCH_HI = 24, 120


def fit_notch(tr, feats):
    """Regresse le cran sur les positifs d'entrainement uniquement."""
    trades = pd.read_parquet(os.path.join(INTERIM, "bot_trades.parquet"))
    notch = (trades.set_index("token_address").entry_sol / BASE_UNIT).round()
    p = tr[tr.label == 1].copy()
    p["notch"] = p.mint.map(notch)
    p = p[p.notch.between(1, 400)]
    r = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.05, num_leaves=31,
                          min_child_samples=40, subsample=0.85, subsample_freq=1,
                          random_state=0, verbose=-1)
    r.fit(p[feats], np.log1p(p.notch), categorical_feature=["uri_host_cat"])
    print(f"regresseur de cran entraine sur {len(p):,} positifs (train seul)")
    return r


def run(sel, name):
    roi, pnl = simulate(sel, 0)
    v = pnl.notna()
    s, roi, pnl = sel[v], roi[v], pnl[v]
    if not len(roi):
        return None
    eq = pnl.loc[s.sort_values("deploy_time_s").index].cumsum().values
    lo, hi = bootstrap_ci(roi.values)
    cap = s.size_sol.sum()
    print(f"  {name:<26} n={len(roi):>5} | ROI med {roi.median():+7.2%} "
          f"[{lo:+.2%}, {hi:+.2%}] | hit {(pnl > 0).mean():6.2%} | "
          f"P&L {pnl.sum():+8.1f} SOL | capital {cap:8.1f} | "
          f"P&L/capital {pnl.sum()/cap:+6.2%} | DD {max_drawdown(eq):7.1f}")
    return dict(n=len(roi), roi=roi.median(), hit=(pnl > 0).mean(),
                pnl=pnl.sum(), cap=cap, dd=max_drawdown(eq))


def main():
    ds = pd.read_parquet(os.path.join(INTERIM, "part2_full_dataset.parquet"))
    _, feats, _, SPLIT = joblib.load(os.path.join(INTERIM, "part2_model.pkl"))
    ds["uri_host_cat"] = ds.uri_host_cat.astype("category")
    tr, te = ds[ds.block_time < SPLIT], ds[ds.block_time >= SPLIT].copy()

    r = fit_notch(tr, feats)
    te["pred_notch"] = np.expm1(r.predict(te[feats])).clip(NOTCH_LO, NOTCH_HI)
    print(f"cran predit sur juin : mediane {te.pred_notch.median():.1f} "
          f"(P10 {te.pred_notch.quantile(.1):.1f}, P90 {te.pred_notch.quantile(.9):.1f})")

    out = pd.read_parquet(os.path.join(INTERIM, "june_outcomes.parquet"))
    bt = te.merge(out[out.mcap_entry > 0], on="mint", how="inner")
    print(f"univers de test : {len(bt):,} tokens, {bt.label.sum():,} pris par le bot\n")

    for q in [0.999, 0.995, 0.99]:
        sel = bt[bt.score >= bt.score.quantile(q)].copy()
        print(f"=== top {(1-q)*100:.1f}% : {len(sel):,} entrees ===")

        a = sel.copy(); a["size_sol"] = FLAT
        ref = run(a, "taille fixe 2.469 SOL")

        # Meme capital total, redistribue selon la conviction : le gain mesure
        # est alors purement une question d'allocation, pas de levier.
        b = sel.copy()
        w = b.pred_notch / b.pred_notch.mean()
        b["size_sol"] = (FLAT * w).clip(0.5, 8.0)
        got = run(b, "cran predit, capital egal")

        c = sel.copy()
        c["size_sol"] = (c.pred_notch * BASE_UNIT).clip(1.0, 6.0)
        run(c, "cran predit, echelle du bot")

        if ref and got:
            print(f"  -> allocation par cran : ROI {got['roi']-ref['roi']:+.2%}, "
                  f"P&L {got['pnl']-ref['pnl']:+.1f} SOL a capital quasi egal "
                  f"({got['cap']/ref['cap']:.2f}x)")
        print()


if __name__ == "__main__":
    main()
