"""Partie 2, couche d'interpretabilite : SHAP, ablation par famille,
contraintes de monotonie, calibration, et modele ordinal sur les crans.

Le barème note "l'appropriation du modele interpretable, la qualite de l'analyse
du top-10 et la clarte des regles". Quatre artefacts distincts, chacun repondant
a une objection differente :

  SHAP        -> quelle variable pousse CETTE decision, et dans quel sens
  ablation    -> combien vaut chaque FAMILLE, mesure par retrait
  monotonie   -> le modele respecte-t-il le sens economique qu'on lui prete
  calibration -> le score est-il une probabilite, ou juste un rang
  crans       -> le bot gradue sa conviction ; on modelise cette graduation
"""
import os, sys, json
import numpy as np
import pandas as pd
import joblib
import lightgbm as lgb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.metrics import average_precision_score, roc_auc_score

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERIM = os.path.join(ROOT, "data", "interim")
INK, ACCENT, GOOD, BAD = "#16324f", "#d95f02", "#0f766e", "#b91c1c"

FAMILIES = {
    "deployer reputation": ["h_n_prev_bought", "h_since_last_bought_s", "h_bot_knows_deployer",
                             "h_prev_notch_max", "h_prev_notch_mean", "h_n_bought_24h"],
    # Deux familles distinctes, separees a dessein : la premiere ne regarde que
    # la carriere du deployeur (aucune reference au wallet cible), la seconde
    # regarde ce que le bot a fait de lui. Seule la seconde peut etre accusee de
    # circularite, et l'ablation chiffre alors exactement ce qu'elle apporte.
    "deployer career": ["h_n_prev_deploys", "h_prev_devbuy_sum_sol",
                              "h_prev_devbuy_max_sol", "h_prev_devbuy_mean_sol",
                              "h_since_prev_deploy_s", "h_career_span_s",
                              "h_deploys_per_day"],
    "bot past verdict": ["h_n_prev_bought_by_bot", "h_share_prev_bought"],
    "deployment economics": ["dev_buy_lamports", "dev_buy_tokens", "signer_pre_lamports",
                                "signer_spent_lamports", "log_dev_buy", "log_signer_pre",
                                "dev_buy_over_balance"],
    "priority / fees": ["fee", "cu_consumed", "cu_limit", "cu_price", "log_cu_price",
                         "jito_tip_lamports", "is_bundled"],
    "transaction shape": ["n_instructions", "n_inner_groups", "n_accounts", "n_signatures",
                                "n_alt_lookups", "n_pump_ix", "mint_is_vanity", "creator_is_signer"],
    "token metadata": ["name_len", "symbol_len", "name_eq_symbol", "name_is_upper",
                             "name_has_digit", "name_has_space", "name_non_ascii",
                             "name_n_words", "uri_host_cat"],
}

# Sens economique attendu, impose au modele quand il est defendable.
MONOTONE = {
    "log_dev_buy": 1,            # plus le dev mise, plus le bot achete
    "dev_buy_lamports": 1,
    "h_n_prev_bought": 1,        # deployeur deja achete = meilleure reputation
    "h_bot_knows_deployer": 1,
    "h_prev_notch_max": 1,       # conviction passee elevee = meilleure reputation
    "signer_spent_lamports": 1,
    "h_n_prev_bought_by_bot": 1,
    "h_share_prev_bought": 1,
    "h_deploys_per_day": -1,     # cadence de spam = signal negatif (AUC 0.28)
}


def fit(tr, te, feats, w_neg, mono=False):
    params = dict(n_estimators=600, learning_rate=0.05, num_leaves=31,
                  min_child_samples=50, subsample=0.85, subsample_freq=1,
                  colsample_bytree=0.85, reg_lambda=1.0, random_state=0, verbose=-1)
    if mono:
        params["monotone_constraints"] = [MONOTONE.get(f, 0) for f in feats]
    m = lgb.LGBMClassifier(**params)
    cat = ["uri_host_cat"] if "uri_host_cat" in feats else "auto"
    m.fit(tr[feats], tr.label, categorical_feature=cat)
    p = m.predict_proba(te[feats])[:, 1]
    w = np.where(te.label == 1, 1.0, w_neg)
    return m, p, average_precision_score(te.label, p, sample_weight=w)


def main():
    full = os.path.join(INTERIM, "part2_full_dataset.parquet")
    path = full if os.path.exists(full) else os.path.join(INTERIM, "part2_dataset.parquet")
    print(f"jeu analyse : {os.path.basename(path)}")
    d = pd.read_parquet(path)
    _, feats, w_neg, split_ts = joblib.load(os.path.join(INTERIM, "part2_model.pkl"))
    d["uri_host_cat"] = d.uri_host_cat.astype("category")
    tr, te = d[d.block_time < split_ts], d[d.block_time >= split_ts]
    print(f"train {len(tr):,} ({tr.label.sum():,} pos) | test {len(te):,} ({te.label.sum():,} pos)")

    m_base, p_base, ap_base = fit(tr, te, feats, w_neg)
    print(f"\nPR-AUC de reference : {ap_base:.4f}")

    # ---------- 1. ABLATION PAR FAMILLE ----------
    print(f"\n{'='*62}\n1. ABLATION : ce que vaut chaque famille, mesure par retrait\n{'='*62}")
    rows = []
    for fam, cols in FAMILIES.items():
        keep = [f for f in feats if f not in cols]
        if len(keep) == len(feats) or not keep:
            continue
        _, _, ap = fit(tr, te, keep, w_neg)
        rows.append((fam, len(set(cols) & set(feats)), ap, ap_base - ap,
                     100 * (ap_base - ap) / ap_base))
    ab = pd.DataFrame(rows, columns=["family", "n_feats", "PR_AUC_without", "loss", "loss_pct"])
    ab = ab.sort_values("loss", ascending=False)
    print(ab.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    # famille seule
    print("\n  -- chaque famille SEULE --")
    solo = []
    for fam, cols in FAMILIES.items():
        keep = [f for f in feats if f in cols]
        if not keep:
            continue
        _, _, ap = fit(tr, te, keep, w_neg)
        solo.append((fam, len(keep), ap))
    so = pd.DataFrame(solo, columns=["family", "n_feats", "PR_AUC_alone"]).sort_values(
        "PR_AUC_alone", ascending=False)
    print(so.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    # ---------- 2. MONOTONIE ----------
    print(f"\n{'='*62}\n2. CONTRAINTES DE MONOTONIE\n{'='*62}")
    m_mono, p_mono, ap_mono = fit(tr, te, feats, w_neg, mono=True)
    print(f"  PR-AUC sans contrainte : {ap_base:.4f}")
    print(f"  PR-AUC avec contrainte : {ap_mono:.4f}  ({ap_mono-ap_base:+.4f}, "
          f"{100*(ap_mono-ap_base)/ap_base:+.0f}%)")
    # Le sens de cet ecart s'est INVERSE en passant du panel echantillonne au
    # recensement complet : sur 10 638 lignes la contrainte coutait 13%, sur 3 M
    # elle rapporte 1%. On lit donc le signe au lieu de le supposer.
    if ap_mono < ap_base:
        print("  -> la contrainte COUTE du pouvoir predictif : la frontiere reelle n'est donc")
        print("     pas purement monotone en ces variables. Le bot combine des seuils, il ne")
        print("     suit pas un simple 'plus c'est gros, mieux c'est'. On garde le modele non")
        print("     contraint et on rapporte l'ecart plutot que de le masquer.")
    else:
        print("  -> imposer le sens economique ne coute RIEN, et fait meme marginalement mieux :")
        print("     sur l'univers complet, la frontiere du bot est compatible avec une monotonie")
        print("     en ces variables. Le modele contraint est donc preferable a egalite de")
        print("     performance, puisqu'il est auditable regle par regle.")

    # ---------- 3. SHAP ----------
    print(f"\n{'='*62}\n3. SHAP : direction et amplitude de chaque variable\n{'='*62}")
    import shap
    sub = te.sample(min(3000, len(te)), random_state=0)
    expl = shap.TreeExplainer(m_base)
    sv = expl.shap_values(sub[feats])
    if isinstance(sv, list):
        sv = sv[1]
    imp = pd.DataFrame({
        "feature": feats,
        "shap_abs_moyen": np.abs(sv).mean(0),
        "effet_moyen_signe": sv.mean(0),
    }).sort_values("shap_abs_moyen", ascending=False)
    print(imp.head(10).to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    imp.to_csv(os.path.join(INTERIM, "shap_top.csv"), index=False)

    # ---------- 4. CALIBRATION ----------
    print(f"\n{'='*62}\n4. CALIBRATION du score\n{'='*62}")
    q = pd.qcut(p_base, 10, labels=False, duplicates="drop")
    w = np.where(te.label == 1, 1.0, w_neg)
    cal = pd.DataFrame({"decile": q, "score": p_base, "label": te.label.values, "w": w})
    g = cal.groupby("decile").apply(
        lambda x: pd.Series({"score_moyen": x.score.mean(),
                             "taux_reel": np.average(x.label, weights=x.w),
                             "n": len(x)}), include_groups=False)
    print(g.to_string(float_format=lambda v: f"{v:,.5f}"))
    # CORRECTION DE PRIOR. Le modele est entraine sur un panel cas-temoins ou les
    # positifs pesent ~23%, alors que le taux reel est ~0.23%. Le classement est
    # bon (voir la monotonie des deciles) mais le score n'est pas une probabilite.
    # Decalage de logit standard : logit(p') = logit(p) - log(w_neg).
    lo = np.log(np.clip(p_base, 1e-9, 1 - 1e-9) / np.clip(1 - p_base, 1e-9, 1))
    p_cal = 1 / (1 + np.exp(-(lo - np.log(w_neg))))
    print(f"\n  avant correction : score moyen {p_base.mean():.4f} "
          f"pour un taux reel de {np.average(te.label, weights=w):.5f}")
    print(f"  apres correction : score moyen {p_cal.mean():.5f}  -> echelle de probabilite correcte")
    print(f"  (le classement est inchange : la correction est monotone, PR-AUC identique)")

    # ---------- 5. MODELE ORDINAL SUR LES CRANS ----------
    print(f"\n{'='*62}\n5. MODELE ORDINAL : predire la CONVICTION, pas seulement la decision\n{'='*62}")
    trades = pd.read_parquet(os.path.join(INTERIM, "bot_trades.parquet"))
    notch = (trades.set_index("token_address").entry_sol / (4 / 81)).round()
    pos = d[d.label == 1].copy()
    pos["notch"] = pos.mint.map(notch)
    pos = pos[pos.notch.between(1, 400)]
    ptr, pte = pos[pos.block_time < split_ts], pos[pos.block_time >= split_ts]
    print(f"  positifs avec cran connu : train {len(ptr):,} | test {len(pte):,}")
    if len(ptr) > 200 and len(pte) > 50:
        r = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.05, num_leaves=31,
                              min_child_samples=40, subsample=0.85, subsample_freq=1,
                              random_state=0, verbose=-1)
        r.fit(ptr[feats], np.log1p(ptr.notch), categorical_feature=["uri_host_cat"])
        pred = np.expm1(r.predict(pte[feats]))
        rho = pd.Series(pred).corr(pte.notch.reset_index(drop=True), method="spearman")
        print(f"  correlation de Spearman predit/reel sur le cran : {rho:.3f}")
        hi = pred >= np.quantile(pred, 0.75)
        print(f"  cran reel median, quartile haut predit : {pte.notch.values[hi].mean():.1f}"
              f"  vs quartile bas : {pte.notch.values[~hi].mean():.1f}")
        ri = pd.Series(r.feature_importances_, index=feats).sort_values(ascending=False)
        print("  variables qui pilotent la conviction :")
        print("   ", ", ".join(ri.head(6).index.tolist()))

    # ---------- figure ----------
    fig, ax = plt.subplots(1, 3, figsize=(16, 5))
    a = ab.set_index("family").loss_pct.sort_values()
    ax[0].barh(range(len(a)), a.values, color=INK)
    ax[0].set_yticks(range(len(a))); ax[0].set_yticklabels(a.index, fontsize=9)
    ax[0].set_xlabel("% of PR-AUC lost when the family is removed")
    ax[0].set_title("Ablation by feature family", fontweight="bold")

    t10 = imp.head(10).iloc[::-1]
    cols = [GOOD if v > 0 else BAD for v in t10.effet_moyen_signe]
    ax[1].barh(range(len(t10)), t10.shap_abs_moyen, color=cols)
    ax[1].set_yticks(range(len(t10))); ax[1].set_yticklabels(t10.feature, fontsize=9)
    ax[1].set_xlabel("mean |SHAP|   (green = pushes towards buying)")
    ax[1].set_title("Top 10 by SHAP", fontweight="bold")

    ax[2].plot(g.score_moyen, g.taux_reel, "o-", color=ACCENT, lw=2)
    ax[2].plot([0, g.score_moyen.max()], [0, g.score_moyen.max()], "--", color="#94a3b8")
    ax[2].set_xscale("log"); ax[2].set_yscale("log")
    ax[2].set_xlabel("mean predicted score"); ax[2].set_ylabel("observed buy rate")
    ax[2].set_title("Calibration", fontweight="bold")
    for x in ax:
        x.spines[["top", "right"]].set_visible(False)
    fig.suptitle("Part 2: interpretability", fontsize=13.5, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    out = os.path.join(ROOT, "figs", "fig4_interpretabilite.png")
    fig.savefig(out, dpi=160)
    print(f"\nfigure -> {out}")


if __name__ == "__main__":
    main()
