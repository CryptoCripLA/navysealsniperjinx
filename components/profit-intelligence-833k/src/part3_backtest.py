"""Partie 3 : strategie replique et backtest honnete, face au bot cible.

TROIS EXIGENCES D'HONNETETE, traitees explicitement
--------------------------------------------------
1. EXECUTION. Le P&L n'est PAS calcule en ratio de market cap. Les chandelles
   donnent le prix marginal ; vendre 5% de la supply dans une bonding curve
   encaisse bien moins. On utilise le modele d'execution calibre sur les 4 195
   executions REELLES du bot (voir execution_model.pkl). Sur ces memes trades,
   l'approche naive en ratio de mcap surestime le ROI d'un facteur 7 a 17.

2. FAISABILITE D'ENTREE. Le bot atterrit en mediane +118 transactions apres le
   deploiement, dans le meme bloc - jamais dans le bundle. Une replique ne peut
   donc pas supposer mieux. On teste la sensibilite a 0, 1 et 2 slots de retard
   en re-tarifant l'entree sur la chandelle correspondante.

3. FRAIS. Priority fee, tip Jito et gas sont preleves aux niveaux medians
   reellement payes par le bot.

4. REFLEXIVITE. Le modele d'execution est calibre sur des tokens que le bot a
   achetes, donc portes par son propre flux et celui de ses suiveurs. L'appliquer
   a des tokens qu'il a ignores est optimiste. Les resultats sont donc rapportes
   SEPAREMENT selon que le bot a pris le token ou non.
"""
import os, sys
import numpy as np
import pandas as pd
import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERIM = os.path.join(ROOT, "data", "interim")

GAS_SOL = 0.097          # gas total median mesure sur le bot (aller + retour)
PRIO_SOL = 0.050         # priority fee mediane du bot
TIP_SOL = 0.0            # tip median (32% seulement en paient un)
COST_SOL = GAS_SOL + PRIO_SOL + TIP_SOL


def max_drawdown(eq):
    peak = np.maximum.accumulate(eq)
    return float((eq - peak).min())


def bootstrap_ci(x, fn=np.median, n=2000, seed=0):
    rng = np.random.default_rng(seed)
    x = np.asarray(x)
    if len(x) < 10:
        return (np.nan, np.nan)
    s = [fn(rng.choice(x, len(x), replace=True)) for _ in range(n)]
    return float(np.percentile(s, 2.5)), float(np.percentile(s, 97.5))


def build_exec_features(d, delay_s=0):
    """Rejoue les features du modele d'execution, avec un retard d'entree.

    Un slot Solana dure ~0.4 s. Un retard de k slots re-tarife l'entree sur la
    chandelle a t_deploy + ceil(0.4k) secondes : on paie plus cher ce que le
    marche a deja bouge, et le mouvement restant est mecaniquement plus petit.
    """
    e = d.copy()
    if delay_s > 0:
        # nouvelle reference d'entree = mcap au moment ou l'on arrive vraiment
        ref = e[f"mcap_close_{delay_s}s"].where(e[f"mcap_close_{delay_s}s"] > 0, e.mcap_entry)
    else:
        ref = e.mcap_entry
    e["ref"] = ref
    out = pd.DataFrame(index=e.index)
    for h in [3, 5, 10, 30, 60]:
        out[f"mx{h}"] = np.log(e[f"mcap_max_{h}s"] / e.ref)
        out[f"cl{h}"] = np.log(e[f"mcap_close_{h}s"] / e.ref)
    out["lv10"] = np.log1p(e.vol_10s)
    out["lv60"] = np.log1p(e.vol_60s)
    out["lsize"] = np.log(e.size_sol)
    out["lnc"] = np.log1p(e.n_candles)
    return out


def simulate(d, delay_s=0):
    """Rend le ROI par trade et le P&L en SOL, frais inclus."""
    model, cols = joblib.load(os.path.join(INTERIM, "execution_model.pkl"))
    X = build_exec_features(d, delay_s)
    X = X.replace([np.inf, -np.inf], np.nan)
    ok = X[cols].notna().all(axis=1)
    roi = pd.Series(np.nan, index=d.index)
    roi.loc[ok] = model.predict(X.loc[ok, cols])
    roi = roi.clip(-1, 5)
    pnl = roi * d.size_sol - COST_SOL
    return roi, pnl


def report(name, d, roi, pnl):
    v = pnl.notna()
    d, roi, pnl = d[v], roi[v], pnl[v]
    if len(d) == 0:
        print(f"\n--- {name} : aucun trade ---")
        return None
    order = d.sort_values("deploy_time_s").index
    eq = pnl.loc[order].cumsum().values
    lo, hi = bootstrap_ci(roi.values)
    print(f"\n--- {name} ---")
    print(f"  trades              : {len(d):,}")
    print(f"  ROI median          : {roi.median():+.2%}   IC95% [{lo:+.2%}, {hi:+.2%}]")
    print(f"  ROI moyen           : {roi.mean():+.2%}")
    print(f"  hit rate            : {(pnl>0).mean():.2%}")
    print(f"  P&L total           : {pnl.sum():+,.1f} SOL")
    print(f"  P&L median / trade  : {pnl.median():+.4f} SOL")
    print(f"  capital engage      : {d.size_sol.sum():,.1f} SOL")
    print(f"  drawdown max        : {max_drawdown(eq):,.1f} SOL")
    return dict(n=len(d), roi_med=roi.median(), hit=(pnl > 0).mean(),
                pnl=pnl.sum(), dd=max_drawdown(eq))


def main():
    full = os.path.join(INTERIM, "part2_full_dataset.parquet")
    path = full if os.path.exists(full) else os.path.join(INTERIM, "part2_dataset.parquet")
    model, feats, w_neg, SPLIT = joblib.load(os.path.join(INTERIM, "part2_model.pkl"))
    print(f"jeu de scoring : {os.path.basename(path)}")
    ds = pd.read_parquet(path)
    out = pd.read_parquet(os.path.join(INTERIM, "june_outcomes.parquet"))
    out = out[out.mcap_entry > 0]

    # Sur l'univers complet, scorer les 5 M de lignes pour n'en garder que juin
    # coute des minutes et de la RAM pour rien : on coupe d'abord.
    ds = ds[ds.block_time >= SPLIT].copy()
    # Le score hors echantillon est deja ecrit par la partie 2. Le reutiliser
    # evite de re-encoder les variables categorielles a l'identique, source
    # silencieuse de divergence entre entrainement et rejeu.
    if "score" in ds.columns and ds.score.notna().all():
        print("scores hors echantillon repris de la partie 2")
    else:
        ds["score"] = model.predict_proba(ds[feats])[:, 1]
    d = ds.merge(out, on="mint", how="inner")
    print(f"tokens scorables avec issues : {len(d):,} (dont {d.label.sum():,} pris par le bot)")

    d = d[d.block_time >= SPLIT].copy()   # hors echantillon : meme coupure que la partie 2
    print(f"periode de test (hors echantillon) : {len(d):,} tokens, {d.label.sum():,} pris par le bot")

    # taille fixe = mediane du bot, pour une comparaison a armes egales
    d["size_sol"] = 2.469

    for q in [0.999, 0.995, 0.99]:
        thr = d.score.quantile(q)
        sel = d[d.score >= thr]
        print(f"\n{'='*64}\nSEUIL score >= {thr:.4f}  (top {(1-q)*100:.1f}%) -> {len(sel):,} entrees")
        roi, pnl = simulate(sel, 0)
        report("replique, entree bloc zero", sel, roi, pnl)
        for dl in [1, 2]:
            r2, p2 = simulate(sel, dl)
            report(f"replique, retard {dl} slot(s)", sel, r2, p2)
        both = sel[sel.label == 1]
        only = sel[sel.label == 0]
        if len(both):
            r, p = simulate(both, 0)
            report("  dont : tokens QUE LE BOT A AUSSI PRIS", both, r, p)
        if len(only):
            r, p = simulate(only, 0)
            report("  dont : tokens QUE LE BOT A IGNORES (extrapolation)", only, r, p)
        rec = sel.label.sum() / max(d.label.sum(), 1)
        prec = sel.label.mean()
        print(f"\n  recouvrement avec le bot : rappel {rec:.2%} | precision {prec:.2%}")


if __name__ == "__main__":
    main()
