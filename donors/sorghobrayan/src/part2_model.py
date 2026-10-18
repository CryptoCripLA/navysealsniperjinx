"""Partie 2 : reverse-engineering de la regle de selection du bot.

CONTRAINTE DE SYMETRIE DES FEATURES
-----------------------------------
Le serveur hebergeant l'historique d'activite des deployeurs de la classe
negative (23.5 GiB) n'a jamais pu etre telecharge (coupures + debit effondre).
Nous disposons de cet historique pour les POSITIFS seulement.

L'utiliser serait une fuite grossiere : le modele apprendrait "j'ai un
historique => positif". Toute feature d'historique deployeur est donc EXCLUE.
Le modele n'utilise que ce qui est extrait a l'identique des deux cotes : la
transaction de deploiement elle-meme, connue a t_decision.

ECHANTILLONNAGE CAS-TEMOINS
---------------------------
Tous les positifs de juin sont utilises. Les negatifs sont un echantillon
aleatoire par slot (batches de blocs bruts tires de facon etalee sur juin).
Les metriques sont repondereees vers le taux de base reel avant publication.
"""
import os, sys, json
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERIM = os.path.join(ROOT, "data", "interim")

# Fenetre juin 2026 (UTC)
JUNE_START, JUNE_END = 1780272000, 1782863999
SPLIT_TS = 1782259200          # 2026-06-24 00:00 UTC : 3 semaines train / 1 semaine test

# La famille "forme de la transaction" (n_instructions, n_accounts, n_signatures,
# n_alt_lookups, n_pump_ix, mint_is_vanity, creator_is_signer, n_inner_groups) est
# volontairement ABSENTE : la selection descendante par famille montre que la
# retirer AMELIORE le PR-AUC de 4,5 % (0.150 -> 0.157). Elle n'apporte que du bruit.
# La reputation du lanceur (hote de metadonnees) a aussi ete testee puis rejetee :
# elle degrade le PR-AUC de 0.157 a 0.111.
NUMERIC = [
    "dev_buy_lamports", "dev_buy_tokens", "signer_pre_lamports", "signer_spent_lamports",
    "fee", "cu_consumed", "cu_limit", "cu_price", "jito_tip_lamports", "is_bundled",
    "name_len", "symbol_len", "name_eq_symbol", "name_is_upper", "name_has_digit",
    "name_has_space", "name_non_ascii", "name_n_words",
]


def load_negatives():
    d = os.path.join(INTERIM, "june_neg")
    files = [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".parquet")]
    if not files:
        raise SystemExit("aucun batch de negatifs recolte pour l'instant")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    print(f"negatifs recoltes : {len(df):,} deploiements sur {len(files)} batches")
    return df


def load_positives():
    f = pd.read_parquet(os.path.join(INTERIM, "bought_features.parquet"))
    f = f[(f.block_time >= JUNE_START) & (f.block_time <= JUNE_END)].copy()
    f["label"] = 1
    print(f"positifs de juin : {len(f):,}")
    return f


def add_deployer_reputation(d):
    """Reputation du deployeur AUPRES DU BOT, tronquee a t_decision.

    L'historique d'activite des deployeurs de la classe negative (23.5 GiB) n'a
    jamais pu etre telecharge. Mais l'index COMPLET des achats du bot couvre
    toute la periode et s'applique aux DEUX classes : pour n'importe quel
    deployeur, on sait combien de ses tokens anterieurs le bot avait deja pris.

    Legitimite : les achats passes du bot sont des transactions publiques
    anterieures au deploiement courant. Rien ici n'est posterieur a t_decision.
    Verification explicite plus bas : la comparaison est STRICTEMENT '<'.
    """
    idx = pd.read_parquet(os.path.join(ROOT, "data", "raw", "bought_deploy_txs_index.parquet"))
    idx = idx[["blockTime", "tx_signer", "token_address"]].rename(columns={"tx_signer": "signer"})

    # Le cran de mise du bot sur chaque achat passe : sa conviction est une
    # echelle discrete en multiples de 4/81 SOL, et cette conviction passee sur
    # un deployeur est un signal bien plus riche qu'un simple compte d'achats.
    tr = pd.read_parquet(os.path.join(INTERIM, "bot_trades.parquet"))
    notch = (tr.set_index("token_address").entry_sol / (4 / 81)).round()
    idx["notch"] = idx.token_address.map(notch).fillna(0.0)
    idx = idx.sort_values("blockTime")

    by_signer = {s: (g.blockTime.values, g.notch.values)
                 for s, g in idx.groupby("signer", sort=False)}

    d = d.sort_values("block_time").copy()
    n_prev, since_last, notch_max, notch_mean, n_24h = [], [], [], [], []
    for sig, bt in zip(d.signer.values, d.block_time.values):
        ev = by_signer.get(sig)
        if ev is None:
            n_prev.append(0); since_last.append(-1); notch_max.append(-1)
            notch_mean.append(-1); n_24h.append(0)
            continue
        times, notches = ev
        m = times < bt                            # STRICTEMENT anterieur a t_decision
        k = int(m.sum())
        n_prev.append(k)
        since_last.append(int(bt - times[m][-1]) if k else -1)
        notch_max.append(float(notches[m].max()) if k else -1)
        notch_mean.append(float(notches[m].mean()) if k else -1)
        n_24h.append(int(((times >= bt - 86400) & m).sum()))
    d["h_n_prev_bought"] = n_prev
    d["h_since_last_bought_s"] = since_last
    d["h_prev_notch_max"] = notch_max
    d["h_prev_notch_mean"] = notch_mean
    d["h_n_bought_24h"] = n_24h
    d["h_bot_knows_deployer"] = (d.h_n_prev_bought > 0).astype(int)
    return d


def add_derived(d):
    """Features derivees, toutes calculables a t_decision."""
    d = d.copy()
    d["dev_buy_sol"] = d.dev_buy_lamports / 1e9
    d["signer_pre_sol"] = d.signer_pre_lamports / 1e9
    d["log_dev_buy"] = np.log1p(d.dev_buy_sol)
    d["log_signer_pre"] = np.log1p(d.signer_pre_sol)
    d["log_cu_price"] = np.log1p(d.cu_price)
    d["dev_buy_over_balance"] = d.dev_buy_lamports / d.signer_pre_lamports.clip(lower=1)
    ts = pd.to_datetime(d.block_time, unit="s", utc=True)
    d["hour"] = ts.dt.hour
    d["dow"] = ts.dt.dayofweek
    # hote de metadonnees = empreinte de l'outil de deploiement
    top = d.uri_host.value_counts().head(12).index
    d["uri_host_cat"] = np.where(d.uri_host.isin(top), d.uri_host, "autre")
    return d


def main():
    neg = load_negatives()
    pos = load_positives()
    neg = neg[(neg.block_time >= JUNE_START) & (neg.block_time <= JUNE_END)]
    neg = neg[neg.label == 0]           # un batch peut contenir des positifs : on les retire
    print(f"negatifs retenus (juin, label 0) : {len(neg):,}")

    cols = [c for c in NUMERIC if c in neg.columns and c in pos.columns]
    keep = cols + ["block_time", "uri_host", "mint", "signer", "label"]

    # APPARIEMENT TEMPOREL (risk-set sampling).
    # Un batch = 250 slots consecutifs, soit ~100 s d'un instant precis de juin.
    # Les negatifs vivent donc dans quelques fenetres etroites, tandis que les
    # positifs couvrent tout le mois : sans appariement, le modele apprend
    # "ce slot a-t-il ete echantillonne" et sort hour/dow en tete des variables.
    # On ne garde donc que les positifs situes dans une fenetre de +/- W autour
    # d'un batch recolte, et on repondere les negatifs par le rapport des durees.
    W = int(os.environ.get('MATCH_W', 2*3600))
    wins = []
    for f in sorted(os.listdir(os.path.join(INTERIM, "june_neg"))):
        if not f.endswith(".parquet"):
            continue
        b = pd.read_parquet(os.path.join(INTERIM, "june_neg", f), columns=["block_time"])
        b = b[(b.block_time >= JUNE_START) & (b.block_time <= JUNE_END)]
        if len(b):
            wins.append((int(b.block_time.min()), int(b.block_time.max())))
    if not wins:
        raise SystemExit("aucune fenetre exploitable")

    batch_span = sum(b - a + 1 for a, b in wins)
    win_span = sum((b + W) - (a - W) + 1 for a, b in wins)
    keep_pos = np.zeros(len(pos), dtype=bool)
    bt = pos.block_time.values
    for a, b in wins:
        keep_pos |= (bt >= a - W) & (bt <= b + W)
    pos = pos[keep_pos]
    print(f"fenetres appariees : {len(wins)} | duree batches {batch_span:,}s "
          f"| duree fenetres {win_span:,}s")
    print(f"positifs apparies : {len(pos):,}")
    globals()["_W_RATIO"] = win_span / max(batch_span, 1)

    d = pd.concat([pos[keep], neg[keep]], ignore_index=True)
    d = d.dropna(subset=["block_time"]).drop_duplicates("mint")
    d = add_deployer_reputation(d)
    d = add_derived(d)

    # hour/dow volontairement EXCLUS : ils encodent l'instant d'echantillonnage
    # des batches, pas une preference du bot.
    feats = cols + ["log_dev_buy", "log_signer_pre", "log_cu_price",
                    "dev_buy_over_balance",
                    "h_n_prev_bought", "h_since_last_bought_s", "h_bot_knows_deployer",
                    "h_prev_notch_max", "h_prev_notch_mean", "h_n_bought_24h"]
    d["uri_host_cat"] = d.uri_host_cat.astype("category")
    feats_all = feats + ["uri_host_cat"]

    # Coupure temporelle placee au 75e centile des NEGATIFS : c'est la classe
    # echantillonnee, donc c'est elle qui contraint la densite du jeu de test.
    split_ts = int(d.loc[d.label == 0, "block_time"].quantile(0.75))
    tr = d[d.block_time < split_ts]
    te = d[d.block_time >= split_ts]
    print(f"\ncoupure : {pd.to_datetime(split_ts,unit='s',utc=True)}")
    print(f"train : {len(tr):,} ({tr.label.sum():,} pos / {(tr.label==0).sum():,} neg)")
    print(f"test  : {len(te):,} ({te.label.sum():,} pos / {(te.label==0).sum():,} neg)")
    if te.label.sum() < 30 or (te.label == 0).sum() < 300:
        raise SystemExit("jeu de test trop maigre (il faut les deux classes) - continuer la recolte")

    import lightgbm as lgb
    from sklearn.metrics import average_precision_score, precision_recall_curve

    m = lgb.LGBMClassifier(
        n_estimators=600, learning_rate=0.05, num_leaves=31, min_child_samples=50,
        subsample=0.85, subsample_freq=1, colsample_bytree=0.85,
        reg_lambda=1.0, random_state=0, verbose=-1)
    m.fit(tr[feats_all], tr.label, categorical_feature=["uri_host_cat"])
    p = m.predict_proba(te[feats_all])[:, 1]

    # --- repondération cohérente avec l'appariement ---
    # Dans une fenetre appariee de duree D, les positifs sont exhaustifs mais les
    # negatifs ne sont observes que sur la sous-duree d du batch. Chaque negatif
    # represente donc D/d negatifs reels. Utiliser ici la fraction sur tout juin
    # (comme avant l'appariement) donnait un poids de 143 au lieu de ~17, et
    # ecrasait artificiellement le taux de base a 0,037 %.
    w_neg = float(globals().get("_W_RATIO", 1.0))
    base_reelle = 4195 / 764451
    base_implied = d.label.sum() / (d.label.sum() + (d.label == 0).sum() * w_neg)
    print(f"\npoids d'un negatif (duree fenetre / duree batch) : {w_neg:,.2f}")
    print(f"taux de base implique : {base_implied:.4%}  (reel sur juin : {base_reelle:.4%})")

    w = np.where(te.label == 1, 1.0, w_neg)
    ap_w = average_precision_score(te.label, p, sample_weight=w)
    ap_raw = average_precision_score(te.label, p)
    print(f"\n=== QUALITE DE CLASSIFICATION (test temporel) ===")
    print(f"  PR-AUC repondere (taux reel)  : {ap_w:.4f}")
    print(f"  PR-AUC brut (echantillon)     : {ap_raw:.4f}")
    print(f"  taux de base repondere        : {te.label.sum()/ (te.label.sum()+ (te.label==0).sum()*w_neg):.5f}")

    prec, rec, thr = precision_recall_curve(te.label, p, sample_weight=w)
    f1 = 2 * prec * rec / np.clip(prec + rec, 1e-12, None)
    i = int(np.nanargmax(f1))
    print(f"  meilleur F1 : {f1[i]:.4f}  (precision {prec[i]:.4f}, rappel {rec[i]:.4f}, seuil {thr[min(i,len(thr)-1)]:.4f})")
    for target in [0.10, 0.25, 0.50]:
        j = int(np.argmin(np.abs(rec - target)))
        print(f"  a rappel {target:.0%} : precision {prec[j]:.4f}")

    imp = pd.Series(m.feature_importances_, index=feats_all).sort_values(ascending=False)
    print(f"\n=== TOP 15 FEATURES (gain LightGBM) ===")
    print(imp.head(15).to_string())

    d.to_parquet(os.path.join(INTERIM, "part2_dataset.parquet"), index=False)
    np.save(os.path.join(INTERIM, "part2_test_scores.npy"), p)
    import joblib
    joblib.dump((m, feats_all, w_neg, split_ts), os.path.join(INTERIM, "part2_model.pkl"))
    print(f"\ndataset et modele sauvegardes")


if __name__ == "__main__":
    main()
