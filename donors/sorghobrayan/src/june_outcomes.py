"""Issues par token pour juin 2026, depuis les chandelles de market cap a la seconde.

USAGE AUTORISE : le barème permet d'utiliser les données post-déploiement pour
calculer les labels de résultat et simuler le P&L du backtest. Ce fichier ne
produit QUE des sorties de ce type. Rien de ce qu'il produit ne doit entrer
comme feature d'entrée dans les parties 2-3 ,  voir asof_deployer.py pour les
features, qui sont les seules a alimenter la decision d'achat.

Le bot detient 6 secondes en mediane : les horizons courts sont ceux qui comptent.
"""
import os, sys, time
import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(ROOT, "data", "duckdb_tmp")
CANDLES = os.path.join(ROOT, "data", "raw", "mcap_candles.parquet")
OUT = os.path.join(ROOT, "data", "interim", "june_outcomes.parquet")

SQL = """
CREATE OR REPLACE TEMP VIEW c AS
SELECT token_address AS mint,
       deploy_time_s,
       candle_time_s - deploy_time_s AS dt,
       open_mcap, high_mcap, low_mcap, close_mcap, volume
FROM read_parquet('{candles}')
WHERE resolution = '1s' AND candle_time_s >= deploy_time_s;

-- mcap de reference : la premiere chandelle observee (etat de la courbe juste
-- apres le deploiement et le dev-buy) = ce que payerait une entree bloc zero.
CREATE OR REPLACE TEMP TABLE entry AS
SELECT mint, deploy_time_s,
       arg_min(open_mcap, dt) AS mcap_entry,
       min(dt) AS first_dt
FROM c GROUP BY 1, 2;

COPY (
  SELECT e.mint, e.deploy_time_s, e.mcap_entry,
         COUNT(*) AS n_candles,
         MAX(c.dt) AS last_dt,
         {horizons}
         SUM(c.volume) FILTER (WHERE c.dt <= 10) AS vol_10s,
         SUM(c.volume) FILTER (WHERE c.dt <= 60) AS vol_60s,
         SUM(c.volume) AS vol_total,
         MAX(c.high_mcap) AS mcap_max_all
  FROM entry e JOIN c USING (mint)
  GROUP BY 1, 2, 3
) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD);
"""

HORIZONS = [1, 2, 3, 5, 10, 30, 60, 300]


def horizon_sql():
    parts = []
    for h in HORIZONS:
        parts.append(f"MAX(c.high_mcap) FILTER (WHERE c.dt <= {h}) AS mcap_max_{h}s")
        parts.append(f"arg_max(c.close_mcap, c.dt) FILTER (WHERE c.dt <= {h}) AS mcap_close_{h}s")
    return ",\n         ".join(parts) + ","


def main():
    os.makedirs(TMP, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET memory_limit='10GB'; SET threads=10; SET preserve_insertion_order=false;")
    con.execute(f"SET temp_directory='{TMP.replace(chr(92), '/')}';")
    t0 = time.time()
    con.execute(SQL.format(candles=CANDLES.replace("\\", "/"),
                           out=OUT.replace("\\", "/"),
                           horizons=horizon_sql()))
    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT.replace(chr(92),'/')}')").fetchone()[0]
    print(f"{n:,} tokens de juin avec issues, en {time.time()-t0:.0f}s -> {OUT}")

    q = f"""
    SELECT
      COUNT(*) AS n,
      ROUND(AVG(CASE WHEN mcap_max_5s  > mcap_entry*1.5 THEN 1 ELSE 0 END), 4) AS pct_x1_5_en_5s,
      ROUND(AVG(CASE WHEN mcap_max_10s > mcap_entry*2   THEN 1 ELSE 0 END), 4) AS pct_x2_en_10s,
      ROUND(AVG(CASE WHEN mcap_max_60s > mcap_entry*2   THEN 1 ELSE 0 END), 4) AS pct_x2_en_60s,
      ROUND(MEDIAN(mcap_close_10s / mcap_entry), 4) AS ret_median_10s,
      ROUND(MEDIAN(mcap_max_10s  / mcap_entry), 4)  AS max_median_10s,
      ROUND(MEDIAN(n_candles), 1) AS candles_med,
      ROUND(MEDIAN(vol_10s), 2) AS vol_10s_med
    FROM read_parquet('{OUT.replace(chr(92),'/')}') WHERE mcap_entry > 0;
    """
    print("\n=== taux de base sur TOUS les tokens de juin ===")
    for k, v in zip([d[0] for d in con.execute(q).description], con.execute(q).fetchone()):
        print(f"  {k:<18} {v}")
    con.close()


if __name__ == "__main__":
    main()
