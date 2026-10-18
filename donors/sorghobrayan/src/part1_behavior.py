"""Partie 1 : analyse comportementale du wallet cible 5brv79e.

Produit data/interim/bot_trades.parquet (une ligne = un token tradé par le bot)
et les statistiques exigées par le barème : taille d'entrée, latence en slots et
en secondes, part de bloc zéro, position intra-bloc, durée de détention,
structure de sortie, hit rate, distribution du P&L.
"""
import gzip, json, sys, os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
INTERIM = os.path.join(ROOT, "data", "interim")
WALLET = "5brv79eFZ2rGprXNvqgVJBkBptkkw8GJX1XydJyZLyAr"


def load_wallet_tx_positions():
    """slot + position intra-bloc de chaque transaction du bot."""
    rows = []
    with gzip.open(os.path.join(RAW, "5brv79e_activity_txs.jsonl.gz"), "rt", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            rows.append((d["transaction"]["signatures"][0], d["slot"],
                         d.get("transactionIndex"), d["meta"].get("fee", 0)))
    return pd.DataFrame(rows, columns=["tx_hash", "buy_slot", "buy_tx_index", "tx_fee"])


def main():
    act = pd.read_parquet(os.path.join(RAW, "5brv79e_activity.parquet"))
    for c in ["quote_amount", "token_amount", "cost_usd", "gas_native", "priority_fee", "tip_fee", "price"]:
        act[c] = pd.to_numeric(act[c], errors="coerce")
    act["ts"] = pd.to_datetime(act["timestamp"], unit="s", utc=True)

    pos = load_wallet_tx_positions()
    act = act.merge(pos, on="tx_hash", how="left")

    dep = pd.read_parquet(os.path.join(RAW, "bought_deploy_txs_index.parquet"))
    dep = dep.sort_values("blockSlot").drop_duplicates("token_address", keep="first")
    depf = pd.read_parquet(os.path.join(INTERIM, "bought_features.parquet"))
    depf = depf.sort_values("slot").drop_duplicates("mint", keep="first")
    dep = dep.merge(depf[["mint", "tx_index", "dev_buy_lamports", "uri_host", "cu_price"]],
                    left_on="token_address", right_on="mint", how="left")

    buys = act[act.event_type == "buy"].copy()
    sells = act[act.event_type == "sell"].copy()

    # --- une ligne par token ---
    g_buy = buys.groupby("token_address").agg(
        n_buys=("tx_hash", "size"),
        sol_in=("quote_amount", "sum"),
        first_buy_ts=("timestamp", "min"),
        first_buy_slot=("buy_slot", "min"),
        entry_sol=("quote_amount", "first"),
        gas_in=("gas_native", "sum"),
    )
    first_buy_idx = buys.sort_values("timestamp").groupby("token_address").head(1).set_index("token_address")
    g_buy["first_buy_tx_index"] = first_buy_idx["buy_tx_index"]
    g_buy["entry_priority_fee"] = first_buy_idx["priority_fee"]
    g_buy["entry_tip_fee"] = first_buy_idx["tip_fee"]

    g_sell = sells.groupby("token_address").agg(
        n_sells=("tx_hash", "size"),
        sol_out=("quote_amount", "sum"),
        last_sell_ts=("timestamp", "max"),
        first_sell_ts=("timestamp", "min"),
        tokens_sold=("token_amount", "sum"),
        gas_out=("gas_native", "sum"),
    )
    tok_bought = buys.groupby("token_address")["token_amount"].sum().rename("tokens_bought")

    t = g_buy.join(g_sell, how="left").join(tok_bought)
    t = t.merge(dep.set_index("token_address")[
        ["blockTime", "blockSlot", "tx_index", "dev_buy_lamports", "uri_host", "tx_signer"]],
        left_index=True, right_index=True, how="left")

    # --- latence ---
    t["latency_s"] = t.first_buy_ts - t.blockTime
    t["latency_slots"] = t.first_buy_slot - t.blockSlot
    t["inblock_delta"] = np.where(t.latency_slots == 0, t.first_buy_tx_index - t.tx_index, np.nan)

    # --- detention & sortie ---
    t["hold_s"] = t.last_sell_ts - t.first_buy_ts
    t["time_to_first_sell_s"] = t.first_sell_ts - t.first_buy_ts
    t["n_sells"] = t.n_sells.fillna(0).astype(int)
    t["sol_out"] = t.sol_out.fillna(0.0)
    t["sold_frac"] = (t.tokens_sold / t.tokens_bought).clip(upper=2)

    # --- P&L (SOL natif, frais inclus) ---
    t["gas_total"] = t.gas_in.fillna(0) + t.gas_out.fillna(0)
    t["pnl_sol"] = t.sol_out - t.sol_in - t.gas_total
    t["roi"] = t.pnl_sol / t.sol_in
    t["is_win"] = (t.pnl_sol > 0).astype(int)
    t["closed"] = (t.sold_frac.fillna(0) > 0.90).astype(int)

    t = t.reset_index().rename(columns={"index": "token_address"})
    t.to_parquet(os.path.join(INTERIM, "bot_trades.parquet"), index=False)

    # ---------------- rapport ----------------
    n = len(t)
    P = lambda x: print(x)
    P(f"\n{'='*68}\nPARTIE 1 ,  COMPORTEMENT DU BOT {WALLET[:8]}\n{'='*68}")
    P(f"Periode : {pd.to_datetime(act.timestamp.min(),unit='s',utc=True)} -> {pd.to_datetime(act.timestamp.max(),unit='s',utc=True)}")
    P(f"Evenements: {len(act):,} | achats {len(buys):,} | ventes {len(sells):,} | tokens distincts {n:,}")

    P(f"\n--- TAILLE D'ENTREE (SOL) ---")
    P(t.entry_sol.describe(percentiles=[.05, .25, .5, .75, .95]).round(3).to_string())
    P(f"coef. de dispersion (std/mean) : {t.entry_sol.std()/t.entry_sol.mean():.3f}")
    P(f"achats multiples sur un meme token : {(t.n_buys>1).mean():.1%}")

    P(f"\n--- LATENCE D'ENTREE ---")
    m = t.latency_slots.notna()
    P(f"tokens apparies au deploiement : {m.sum():,} / {n:,}")
    P(f"latence secondes : {t.latency_s[m].describe(percentiles=[.5,.9,.99]).round(1).to_string()}")
    P(f"latence slots    : {t.latency_slots[m].describe(percentiles=[.5,.9,.99]).round(1).to_string()}")
    P(f"PART BLOC ZERO   : {(t.latency_slots[m]==0).mean():.2%}")
    P(f"<= 1 slot        : {(t.latency_slots[m]<=1).mean():.2%}")
    P(f"\nposition intra-bloc (buy_tx_index - deploy_tx_index), bloc zero seulement :")
    ib = t.inblock_delta.dropna()
    P(ib.describe(percentiles=[.05, .25, .5, .75, .95]).round(1).to_string())
    P(f"  juste apres le deploy (delta=1) : {(ib==1).mean():.2%}")
    P(f"  delta <= 5                      : {(ib<=5).mean():.2%}")
    P(f"  avant le deploy (delta<0)       : {(ib<0).mean():.2%}")

    P(f"\n--- COUT D'ENTREE ---")
    P(f"priority fee mediane : {t.entry_priority_fee.median():.4f} SOL | p90 {t.entry_priority_fee.quantile(.9):.4f}")
    P(f"tip Jito median      : {t.entry_tip_fee.median():.4f} SOL | part avec tip>0 : {(t.entry_tip_fee>0).mean():.1%}")
    P(f"gas total median     : {t.gas_total.median():.4f} SOL")

    P(f"\n--- DETENTION & SORTIE ---")
    P(f"duree de detention (s) : {t.hold_s.describe(percentiles=[.25,.5,.75,.95]).round(0).to_string()}")
    P(f"delai avant 1re vente (s) : mediane {t.time_to_first_sell_s.median():.0f}")
    P(f"nb de ventes par token : {t.n_sells.describe(percentiles=[.5,.9]).round(2).to_string()}")
    P(f"  jamais vendu   : {(t.n_sells==0).mean():.1%}")
    P(f"  1 seule vente  : {(t.n_sells==1).mean():.1%}")
    P(f"  >=3 ventes     : {(t.n_sells>=3).mean():.1%}")
    P(f"  position soldee (>90% des tokens) : {t.closed.mean():.1%}")

    P(f"\n--- P&L ---")
    cl = t[t.closed == 1]
    P(f"hit rate (toutes positions)  : {t.is_win.mean():.2%}")
    P(f"hit rate (positions soldees) : {cl.is_win.mean():.2%}  (n={len(cl):,})")
    P(f"P&L total : {t.pnl_sol.sum():,.1f} SOL | investi {t.sol_in.sum():,.1f} SOL | ROI global {t.pnl_sol.sum()/t.sol_in.sum():.2%}")
    P(f"gain moyen  : {t.loc[t.is_win==1,'pnl_sol'].mean():.3f} SOL")
    P(f"perte moyenne : {t.loc[t.is_win==0,'pnl_sol'].mean():.3f} SOL")
    P(f"distribution du P&L par trade (SOL) :")
    P(t.pnl_sol.describe(percentiles=[.01, .05, .25, .5, .75, .95, .99]).round(3).to_string())
    P(f"ROI par trade :")
    P(t.roi.describe(percentiles=[.05, .25, .5, .75, .95]).round(3).to_string())

    P(f"\n--- ECHANTILLON DE DEV-BUY SUR CE QU'IL ACHETE ---")
    P(f"dev_buy median : {(t.dev_buy_lamports/1e9).median():.3f} SOL")
    P(f"\nSauvegarde -> data/interim/bot_trades.parquet")


if __name__ == "__main__":
    main()
