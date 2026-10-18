"""Historique du deployeur reconstruit depuis l'UNIVERS DES DEPLOIEMENTS lui-meme.

Pourquoi : le membre 6 de l'archive (23.5 GiB d'activite des deployeurs de la
classe negative) arrive trop tard pour la livraison. Sans lui, utiliser
l'historique des positifs seuls serait une fuite grossiere.

Mais bought_deploy_txs + not_bought_deploy_txs forment ENSEMBLE la totalite des
deploiements pump.fun de la periode. On peut donc reconstruire, de facon
strictement symetrique entre les deux classes, l'historique de deploiement de
chaque wallet - qui est precisement la famille de signaux que le bot exploite.

TRONCATURE : agregats cumulatifs par deployeur tries par slot croissant, puis
ASOF JOIN sur slot STRICTEMENT INFERIEUR. Un deploiement ne voit jamais que ce
qui le precede. audit() le reverifie par recomptage brut.

LEGITIMITE DES ACHATS PASSES DU BOT COMME FEATURE : les achats anterieurs du
wallet cible sont publics on-chain avant t_decision. "Ce deployeur a deja ete
achete par le bot" est donc connu au moment de la decision. On rapporte
neanmoins les performances AVEC et SANS cette famille, pour transparence.
"""
import os, sys, time
import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(ROOT, "data", "duckdb_tmp")

SQL = """
CREATE OR REPLACE TEMP VIEW allmints AS
SELECT mint, creator AS dep, slot, block_time, dev_buy_lamports, label, uri_host
FROM read_parquet({sources});

-- RANGE et non ROWS : avec ROWS ... 1 PRECEDING, deux deploiements d'un meme
-- wallet dans le MEME slot se voient l'un l'autre. RANGE ... 1 PRECEDING borne
-- sur la valeur (slot <= courant - 1), donc strictement anterieur.
-- Pas d'auto-jointure : les fenetres sont evaluees directement sur la source,
-- ce qui evite aussi de dependre de l'unicite de mint.
COPY (
  SELECT mint, dep AS deployer, slot, block_time, label,
         c_prev AS h_n_prev_deploys,
         c_bought AS h_n_prev_bought_by_bot,
         CASE WHEN c_prev > 0 THEN c_bought::DOUBLE / c_prev ELSE -1 END
                                              AS h_share_prev_bought,
         devbuy_sum/1e9                       AS h_prev_devbuy_sum_sol,
         devbuy_max/1e9                       AS h_prev_devbuy_max_sol,
         CASE WHEN c_prev > 0 THEN (devbuy_sum/1e9)/c_prev ELSE -1 END
                                              AS h_prev_devbuy_mean_sol,
         CASE WHEN prev_bt IS NULL THEN -1 ELSE block_time - prev_bt END
                                              AS h_since_prev_deploy_s,
         CASE WHEN first_bt IS NULL THEN -1 ELSE block_time - first_bt END
                                              AS h_career_span_s,
         CASE WHEN first_bt IS NULL OR block_time <= first_bt THEN -1
              ELSE c_prev::DOUBLE / ((block_time - first_bt)/86400.0) END
                                              AS h_deploys_per_day
  FROM (
    SELECT mint, dep, slot, block_time, label,
      COUNT(*)                                  OVER w AS c_prev,
      COALESCE(SUM(label) OVER w, 0)            AS c_bought,
      COALESCE(SUM(dev_buy_lamports) OVER w, 0) AS devbuy_sum,
      COALESCE(MAX(dev_buy_lamports) OVER w, 0) AS devbuy_max,
      MIN(block_time) OVER w                    AS first_bt,
      MAX(block_time) OVER w                    AS prev_bt
    FROM allmints
    WINDOW w AS (PARTITION BY dep ORDER BY slot
                 RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
  )
) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD);
"""

AUDIT = """
WITH s AS (SELECT * FROM read_parquet('{out}') WHERE h_n_prev_deploys > 0 USING SAMPLE {n} ROWS),
-- La cle d'un deploiement est (mint, deployeur, slot) : ~4% des mints
-- apparaissent deux fois dans l'univers, grouper sur mint seul fusionnerait
-- deux historiques distincts et ferait crier une fuite inexistante.
rc AS (
  SELECT s.mint, s.deployer, s.slot, s.h_n_prev_deploys, s.h_n_prev_bought_by_bot,
         COUNT(*) AS vrai_n,
         COALESCE(SUM(a.label),0) AS vrai_bought,
         COUNT(*) FILTER (WHERE a.slot >= s.slot) AS lignes_futures
  FROM s JOIN read_parquet({sources}) a
    ON a.creator = s.deployer AND a.slot < s.slot
  GROUP BY 1,2,3,4,5)
SELECT COUNT(*) n,
       SUM(CASE WHEN h_n_prev_deploys<>vrai_n THEN 1 ELSE 0 END) mauvais_n,
       SUM(CASE WHEN h_n_prev_bought_by_bot<>vrai_bought THEN 1 ELSE 0 END) mauvais_bought,
       SUM(lignes_futures) futures
FROM rc;
"""


def build(sources, out, mem="9GB", threads=10):
    os.makedirs(TMP, exist_ok=True)
    src = "[" + ",".join(f"'{s.replace(chr(92),'/')}'" for s in sources) + "]"
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{mem}'; SET threads={threads}; SET preserve_insertion_order=false;")
    con.execute(f"SET temp_directory='{TMP.replace(chr(92),'/')}';")
    t0 = time.time()
    con.execute(SQL.format(sources=src, out=out.replace("\\", "/")))
    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out.replace(chr(92),'/')}')").fetchone()[0]
    print(f"{n:,} lignes en {time.time()-t0:.0f}s -> {out}")

    r = con.execute(AUDIT.format(out=out.replace("\\", "/"), sources=src, n=200)).fetchone()
    print(f"[AUDIT FUITE] {r[0]} deploiements recomptes a la main")
    print(f"  nb de deploiements anterieurs incoherent : {r[1]}")
    print(f"  nb d'achats du bot anterieurs incoherent : {r[2]}")
    print(f"  lignes au slot courant ou apres incluses : {r[3]}")
    assert r[1] == 0 and r[2] == 0 and r[3] == 0, "FUITE TEMPORELLE DETECTEE"
    print("  -> OK : aucun agregat ne contient de deploiement posterieur")
    con.close()


if __name__ == "__main__":
    out = sys.argv[1]
    build(sys.argv[2:], out)
