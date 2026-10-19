"""Parse un .jsonl.gz de transactions de deploiement en parquet de features.

Usage: python src/run_parse.py <input.jsonl.gz> <output.parquet> [--workers N]
"""
import sys, gzip, time, os
from multiprocessing import Pool
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_parser import parse_line

# 50 000 lignes x 18 workers saturaient les 15.7 GB de la machine : les workers
# se bloquaient sans consommer de CPU. Un bloc de 10 000 tient dans ~30 MB.
BATCH = 10_000


def main():
    src, dst = sys.argv[1], sys.argv[2]
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 10

    t0 = time.time()
    writer = None
    n = n_bad = 0
    buf = []

    def flush(rows):
        nonlocal writer
        tbl = pa.Table.from_pylist(rows)
        if writer is None:
            writer = pq.ParquetWriter(dst, tbl.schema, compression="zstd")
        writer.write_table(tbl)

    def read_block(f, k):
        """Lit au plus k lignes. Tolere un .gz tronque : on s'arrete a la
        derniere ligne complete, ce qui permet de parser pendant que le
        telechargement continue."""
        out = []
        try:
            for _ in range(k):
                ln = f.readline()
                if not ln:
                    break
                out.append(ln)
        except (EOFError, OSError) as e:
            print(f"  flux tronque ({type(e).__name__}) - arret propre", flush=True)
        return out

    # On alimente le pool par blocs bornes : imap() sur un fichier entier
    # empile les 5M de lignes dans la file des workers avant meme de consommer
    # les resultats, ce qui epuise la RAM.
    with gzip.open(src, "rt", encoding="utf-8") as f, Pool(workers) as pool:
        while True:
            block = read_block(f, BATCH)
            if not block:
                break
            n += len(block)
            recs = pool.map(parse_line, block, chunksize=100)
            buf = [r for r in recs if r is not None]
            n_bad += len(recs) - len(buf)
            if buf:
                flush(buf)
            el = time.time() - t0
            print(f"{n:>9,} lignes  {el:6.0f}s  {n/el:,.0f}/s", flush=True)
    buf = []
    if writer:
        writer.close()

    el = time.time() - t0
    print(f"TERMINE {n:,} lignes ({n_bad:,} echecs) en {el:.0f}s -> {dst}")


if __name__ == "__main__":
    main()
