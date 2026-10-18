"""Enchaine les etapes qui suivent le parse des 5.06 M de negatifs.

  1. etiquetage des deux classes, colonnes reduites a ce dont l'as-of a besoin ;
  2. historique de DEPLOIEMENT par as-of strict, avec audit de fuite ;
  3. modele Partie 2 sur l'univers complet.

L'etape 2 est la seule famille historique disponible symetriquement sans
l'archive d'activite : elle se reconstruit depuis l'union des deux classes,
qui EST la totalite des deploiements pump.fun de la periode.

Usage: python src/run_full_pipeline.py [--skip-model]
"""
import os, sys, time
import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERIM = os.path.join(ROOT, "data", "interim")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ASOF_COLS = ["mint", "creator", "slot", "block_time", "dev_buy_lamports", "uri_host"]


def q(p):
    return p.replace("\\", "/")


def step1_label():
    """Ecrit les deux classes au format attendu par l'as-of, colonnes reduites.

    On ne materialise pas les 37 colonnes des 5 M de negatifs : l'as-of n'en
    utilise que six, et la memoire disponible est le facteur limitant.
    """
    neg_in = os.path.join(INTERIM, "neg_features_full.parquet")
    pos_in = os.path.join(INTERIM, "bought_features.parquet")
    out = os.path.join(INTERIM, "asof_sources.parquet")
    cols = ", ".join(ASOF_COLS)
    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'; SET threads=6; SET preserve_insertion_order=false;")
    con.execute(f"""
        COPY (
          SELECT {cols}, 1 AS label FROM read_parquet('{q(pos_in)}')
          UNION ALL
          SELECT {cols}, 0 AS label FROM read_parquet('{q(neg_in)}')
        ) TO '{q(out)}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    n, npos = con.execute(
        f"SELECT COUNT(*), SUM(label) FROM read_parquet('{q(out)}')").fetchone()
    con.close()
    print(f"[1] univers etiquete : {n:,} deploiements dont {npos:,} achetes "
          f"({npos/n:.4%})", flush=True)
    return out


def step2_deploy_hist(src):
    from asof_from_deploys import build
    out = os.path.join(INTERIM, "deploy_hist_full.parquet")
    # 2.5 GB : la machine n'a que ~3 GB reellement libres. Au-dela, duckdb ne
    # deborde pas sur disque assez tot et c'est l'OS qui pagine, ce qui avait
    # deja fait chuter le parse de 2 500 a 227 lignes/s.
    build([src], out, mem="2500MB", threads=8)
    return out


def main():
    t0 = time.time()
    src = step1_label()
    step2_deploy_hist(src)
    print(f"[2] historique de deploiement pret ({time.time()-t0:.0f}s)", flush=True)
    if "--skip-model" not in sys.argv:
        import part2_full
        part2_full.main()
    print(f"\npipeline complet en {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
