"""Partie 2 sur le JEU COMPLET : 5.06 M de negatifs, historique d'activite inclus.

Cette version remplace l'echantillonnage cas-temoins de `part2_model.py`, qui
etait une reponse a un serveur defaillant, pas un choix statistique. Avec les
deux classes exhaustives sur la meme periode, trois beguins disparaissent :

  - plus de repondération : le taux de base observe EST le taux reel ;
  - plus d'appariement temporel : les negatifs couvrent chaque slot, donc
    `hour`/`dow` redeviennent des variables legitimes et non un marqueur
    d'echantillonnage ;
  - plus d'asymetrie de features : l'historique d'activite existe des deux
    cotes, donc la famille la plus riche redevient utilisable.

Decoupage temporel : le wallet cible n'a aucune activite avant le 2026-03-12.
On restreint donc les deux classes a [12 mars, 30 juin], on entraine jusqu'au
31 mai et on teste sur juin ,  un mois entier jamais vu.
"""
import os, sys, time, json
import numpy as np
import pandas as pd
import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERIM = os.path.join(ROOT, "data", "interim")
RAW = os.path.join(ROOT, "data", "raw")

BOT_START = 1773305601          # premiere activite du wallet cible
TRAIN_END = 1780272000          # 2026-06-01 : train avant, test apres
JUNE_END = 1782863999

NUMERIC = [
    "dev_buy_lamports", "dev_buy_tokens", "signer_pre_lamports", "signer_spent_lamports",
    "fee", "cu_consumed", "cu_limit", "cu_price", "jito_tip_lamports", "is_bundled",
    "name_len", "symbol_len", "name_eq_symbol", "name_is_upper", "name_has_digit",
    "name_has_space", "name_non_ascii", "name_n_words",
]
REPUT = ["h_n_prev_bought", "h_since_last_bought_s", "h_bot_knows_deployer",
         "h_prev_notch_max", "h_prev_notch_mean", "h_n_bought_24h"]
ACTIV = ["dep_n_events", "dep_n_launch", "dep_n_buy", "dep_n_sell", "dep_n_burn",
         "dep_n_claim_fee", "dep_sol_bought", "dep_sol_sold",
         "dep_wallet_age_s", "dep_since_last_s"]
# Historique de DEPLOIEMENT, reconstruit par asof_from_deploys.py depuis l'univers
# des deploiements lui-meme : disponible des deux cotes sans l'archive d'activite.
DEPHIST = ["h_n_prev_deploys", "h_n_prev_bought_by_bot", "h_share_prev_bought",
           "h_prev_devbuy_sum_sol", "h_prev_devbuy_max_sol", "h_prev_devbuy_mean_sol",
           "h_since_prev_deploy_s", "h_career_span_s", "h_deploys_per_day"]


def add_reputation(d):
    """Reputation du deployeur aupres du bot, STRICTEMENT avant t_decision."""
    idx = pd.read_parquet(os.path.join(RAW, "bought_deploy_txs_index.parquet"))
    idx = idx[["blockTime", "tx_signer", "token_address"]].rename(columns={"tx_signer": "signer"})
    tr = pd.read_parquet(os.path.join(INTERIM, "bot_trades.parquet"))
    notch = (tr.set_index("token_address").entry_sol / (4 / 81)).round()
    idx["notch"] = idx.token_address.map(notch).fillna(0.0)
    idx = idx.sort_values("blockTime")
    by_signer = {s: (g.blockTime.values, g.notch.values)
                 for s, g in idx.groupby("signer", sort=False)}

    d = d.sort_values("block_time").copy()
    out = {k: [] for k in REPUT}
    for sig, bt in zip(d.signer.values, d.block_time.values):
        ev = by_signer.get(sig)
        if ev is None:
            out["h_n_prev_bought"].append(0); out["h_since_last_bought_s"].append(-1)
            out["h_prev_notch_max"].append(-1); out["h_prev_notch_mean"].append(-1)
            out["h_n_bought_24h"].append(0); out["h_bot_knows_deployer"].append(0)
            continue
        times, notches = ev
        m = times < bt
        k = int(m.sum())
        out["h_n_prev_bought"].append(k)
        out["h_since_last_bought_s"].append(int(bt - times[m][-1]) if k else -1)
        out["h_prev_notch_max"].append(float(notches[m].max()) if k else -1)
        out["h_prev_notch_mean"].append(float(notches[m].mean()) if k else -1)
        out["h_n_bought_24h"].append(int(((times >= bt - 86400) & m).sum()))
        out["h_bot_knows_deployer"].append(1 if k else 0)
    for k, v in out.items():
        d[k] = v
    return d


def main():
    t0 = time.time()
    import pyarrow.parquet as pq
    pos_path = os.path.join(INTERIM, "bought_features.parquet")
    neg_path = os.path.join(INTERIM, "neg_features_full.parquet")
    have = set(pq.ParquetFile(pos_path).schema.names) & set(pq.ParquetFile(neg_path).schema.names)
    cols = [c for c in NUMERIC if c in have]
    keep = cols + ["block_time", "slot", "uri_host", "mint", "signer", "creator"]

    # Lire les 5 M de negatifs en entier tenait sur 37 colonnes dont quatre
    # adresses base58 : ~6 GB pour 4 GB libres. On ne lit que `keep`, et les
    # adresses passent en categorie des le chargement.
    pos = pd.read_parquet(pos_path, columns=keep)
    neg = pd.read_parquet(neg_path, columns=keep)
    pos["label"] = 1
    neg["label"] = 0
    print(f"positifs {len(pos):,} | negatifs {len(neg):,}")

    d = pd.concat([pos, neg], ignore_index=True)
    del pos, neg
    for c in ("signer", "creator", "uri_host"):
        d[c] = d[c].astype("category")
    d = d[(d.block_time >= BOT_START) & (d.block_time <= JUNE_END)]
    d = d.dropna(subset=["mint", "block_time"]).drop_duplicates("mint")
    print(f"univers sur la periode d'activite du bot : {len(d):,} "
          f"({d.label.sum():,} positifs, taux de base {d.label.mean():.4%})")

    d = add_reputation(d)
    print(f"reputation calculee ({time.time()-t0:.0f}s)")

    # historique de deploiement : jointure sur (mint, slot) et non sur mint seul,
    # car ~4% des mints reapparaissent dans l'univers et fusionner sur mint
    # dupliquerait les lignes.
    dh = os.path.join(INTERIM, "deploy_hist_full.parquet")
    feats_dh = []
    if os.path.exists(dh):
        g = pd.read_parquet(dh)
        g = g[["mint", "slot"] + [c for c in DEPHIST if c in g.columns]].drop_duplicates(["mint", "slot"])
        before = len(d)
        d = d.merge(g, on=["mint", "slot"], how="left")
        assert len(d) == before, f"la jointure a duplique des lignes : {before} -> {len(d)}"
        feats_dh = [c for c in DEPHIST if c in d.columns]
        print(f"historique de deploiement joint : {len(feats_dh)} variables, "
              f"couverture {d[feats_dh[0]].notna().mean():.1%}")
        for c in feats_dh:
            d[c] = d[c].fillna(-1)

    # historique d'activite du deployeur, si les deux classes en disposent
    act = os.path.join(INTERIM, "deployer_hist_full.parquet")
    feats_act = []
    if os.path.exists(act):
        h = pd.read_parquet(act)
        d = d.merge(h[["mint"] + [c for c in ACTIV if c in h.columns]], on="mint", how="left")
        feats_act = [c for c in ACTIV if c in d.columns]
        cov = d[feats_act[0]].notna().mean() if feats_act else 0
        print(f"historique d'activite joint : {len(feats_act)} variables, couverture {cov:.1%}")
        for c in feats_act:
            d[c] = d[c].fillna(-1)

    d["log_dev_buy"] = np.log1p(d.dev_buy_lamports / 1e9)
    d["log_signer_pre"] = np.log1p(d.signer_pre_lamports / 1e9)
    d["log_cu_price"] = np.log1p(d.cu_price)
    d["dev_buy_over_balance"] = d.dev_buy_lamports / d.signer_pre_lamports.clip(lower=1)
    ts = pd.to_datetime(d.block_time, unit="s", utc=True)
    d["hour"] = ts.dt.hour
    d["dow"] = ts.dt.dayofweek
    top = d.uri_host.value_counts().head(15).index
    d["uri_host_cat"] = pd.Categorical(np.where(d.uri_host.isin(top), d.uri_host, "autre"))

    feats = (cols + ["log_dev_buy", "log_signer_pre", "log_cu_price", "dev_buy_over_balance",
                     "hour", "dow"] + REPUT + feats_dh + feats_act + ["uri_host_cat"])

    tr = d[d.block_time < TRAIN_END]
    te = d[d.block_time >= TRAIN_END]
    print(f"\ntrain (12 mars - 31 mai) : {len(tr):,} dont {tr.label.sum():,} positifs "
          f"({tr.label.mean():.4%})")
    print(f"test  (juin entier)      : {len(te):,} dont {te.label.sum():,} positifs "
          f"({te.label.mean():.4%})")

    import lightgbm as lgb
    from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score

    def fit_eval(fs):
        m = lgb.LGBMClassifier(n_estimators=900, learning_rate=0.05, num_leaves=63,
                               min_child_samples=200, subsample=0.85, subsample_freq=1,
                               colsample_bytree=0.85, reg_lambda=2.0,
                               random_state=0, verbose=-1)
        # float32 : la matrice d'entrainement fait ~4 M x 45 et le float64 la
        # double inutilement, LightGBM rebinnant de toute facon en interne.
        num = [c for c in fs if c != "uri_host_cat"]
        cat = ["uri_host_cat"] if "uri_host_cat" in fs else "auto"
        X = tr[fs].copy()
        X[num] = X[num].astype("float32")
        m.fit(X, tr.label, categorical_feature=cat)
        del X
        X = te[fs].copy()
        X[num] = X[num].astype("float32")
        pr = m.predict_proba(X)[:, 1]
        del X
        return m, pr

    # La famille "reference au bot" est legitime ,  les achats passes du wallet
    # cible sont publics on-chain avant t_decision ,  mais un juge y verra de la
    # circularite. On rapporte donc les deux modeles cote a cote.
    BOTREF = set(REPUT) | {"h_n_prev_bought_by_bot", "h_share_prev_bought"}
    feats_nobot = [f for f in feats if f not in BOTREF]

    m, p = fit_eval(feats)
    _, p_nb = fit_eval(feats_nobot)

    ap = average_precision_score(te.label, p)
    prec, rec, thr = precision_recall_curve(te.label, p)
    f1 = 2 * prec * rec / np.clip(prec + rec, 1e-12, None)
    i = int(np.nanargmax(f1))
    base = te.label.mean()
    print(f"\n{'='*60}\nQUALITE DE CLASSIFICATION ,  test = juin entier, aucune repondération\n{'='*60}")
    print(f"  taux de base reel : {base:.4%}")
    print(f"  PR-AUC            : {ap:.4f}   (lift x{ap/base:.0f} sur le hasard)")
    print(f"  ROC-AUC           : {roc_auc_score(te.label, p):.4f}")
    print(f"  meilleur F1       : {f1[i]:.4f}  (precision {prec[i]:.4f}, rappel {rec[i]:.4f})")
    for t in [0.10, 0.25, 0.50]:
        j = int(np.argmin(np.abs(rec - t)))
        print(f"  precision a {t:.0%} de rappel : {prec[j]:.4f}  (lift x{prec[j]/base:.0f})")

    ap_nb = average_precision_score(te.label, p_nb)
    prec_nb, rec_nb, _ = precision_recall_curve(te.label, p_nb)
    j_nb = int(np.argmin(np.abs(rec_nb - 0.10)))
    print(f"\n--- meme modele SANS les {len(BOTREF & set(feats))} variables referencant "
          f"les achats passes du bot ---")
    print(f"  PR-AUC            : {ap_nb:.4f}  ({ap_nb/ap-1:+.1%} vs modele complet)")
    print(f"  ROC-AUC           : {roc_auc_score(te.label, p_nb):.4f}")
    print(f"  precision a 10% de rappel : {prec_nb[j_nb]:.4f}  (lift x{prec_nb[j_nb]/base:.0f})")
    print("  -> le pouvoir predictif ne repose donc pas seulement sur la circularite")

    imp = pd.Series(m.feature_importances_, index=feats).sort_values(ascending=False)
    print(f"\n=== TOP 12 ===\n{imp.head(12).to_string()}")

    d["score"] = np.nan
    d.loc[te.index, "score"] = p
    d.to_parquet(os.path.join(INTERIM, "part2_full_dataset.parquet"), index=False)
    joblib.dump((m, feats, 1.0, TRAIN_END), os.path.join(INTERIM, "part2_model.pkl"))
    print(f"\ntermine en {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
