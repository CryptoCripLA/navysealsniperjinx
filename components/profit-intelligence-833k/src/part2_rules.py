"""Regles de decision en langage clair + figure des importances.

Un arbre peu profond, entraine sur les MEMES donnees appariees que le modele
principal, sert de lecture humaine de la frontiere. On rapporte aussi le
pouvoir separateur univarie de chaque feature (AUC), qui ne depend d'aucun
choix d'hyperparametre.
"""
import os
import numpy as np
import pandas as pd
import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.metrics import roc_auc_score

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERIM = os.path.join(ROOT, "data", "interim")
INK, ACCENT = "#16324f", "#d95f02"


def main():
    full = os.path.join(INTERIM, "part2_full_dataset.parquet")
    d = pd.read_parquet(full if os.path.exists(full)
                        else os.path.join(INTERIM, "part2_dataset.parquet"))
    model, feats, w_neg, split_ts = joblib.load(os.path.join(INTERIM, "part2_model.pkl"))
    num = [f for f in feats if f != "uri_host_cat"]
    tr = d[d.block_time < split_ts]

    # ---- pouvoir separateur univarie ----
    rows = []
    for f in num:
        x = tr[f].astype(float).replace([np.inf, -np.inf], np.nan)
        if x.notna().sum() < 100 or x.nunique() < 3:
            continue
        m = x.notna()
        a = roc_auc_score(tr.label[m], x[m])
        rows.append((f, a, max(a, 1 - a),
                     tr.loc[tr.label == 1, f].median(), tr.loc[tr.label == 0, f].median()))
    u = pd.DataFrame(rows, columns=["feature", "auc", "sep", "med_pos", "med_neg"])
    u = u.sort_values("sep", ascending=False)
    print("=== POUVOIR SEPARATEUR UNIVARIE (train) ===")
    print(u.head(12).to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    # ---- arbre lisible ----
    X = tr[num].astype(float).replace([np.inf, -np.inf], np.nan).fillna(-1)
    t = DecisionTreeClassifier(max_depth=3, min_samples_leaf=80,
                               class_weight="balanced", random_state=0)
    t.fit(X, tr.label)
    print("\n=== ARBRE DE LECTURE (profondeur 3) ===")
    print(export_text(t, feature_names=num, decimals=3, show_weights=True))

    # ---- figure ----
    imp = pd.Series(model.feature_importances_, index=feats).sort_values()
    imp = imp[imp > 0].tail(12)
    fig, ax = plt.subplots(1, 2, figsize=(13, 5.4))
    ax[0].barh(range(len(imp)), imp.values, color=INK)
    ax[0].set_yticks(range(len(imp))); ax[0].set_yticklabels(imp.index, fontsize=9)
    ax[0].set_title("LightGBM importance (gain)", fontweight="bold")
    ax[0].spines[["top", "right"]].set_visible(False)

    top = u.head(8).iloc[::-1]
    y = np.arange(len(top))
    ax[1].barh(y, top.sep - 0.5, left=0.5, color=ACCENT)
    ax[1].set_yticks(y); ax[1].set_yticklabels(top.feature, fontsize=9)
    ax[1].axvline(0.5, color="#94a3b8", lw=1)
    ax[1].set_xlim(0.5, max(0.62, top.sep.max() + 0.02))
    ax[1].set_title("Univariate separating power (AUC)", fontweight="bold")
    ax[1].spines[["top", "right"]].set_visible(False)
    fig.suptitle("What the bot decides on", fontsize=13.5, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    out = os.path.join(ROOT, "figs", "fig3_features.png")
    fig.savefig(out, dpi=160)
    print("\nfigure ->", out)


if __name__ == "__main__":
    main()
