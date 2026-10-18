"""Recolte les deploiements de juin depuis les blocs bruts (serveur avec reprise).

Le serveur core (archive de 41 GB) coupe toutes les 1-4 h et n'accepte pas les
Range : 34 h de tentatives n'ont jamais depasse 3.3 GiB sur 15. Le serveur des
supplements, lui, supporte les Range -> progression garantie et reprenable.

Chaque batch (250 slots, ~180 MB zstd) contient ~265 deploiements pump.fun.
On telecharge, on parse, on ecrit les features, on supprime le batch. Les
batches sont pris dans un ordre etale sur tout juin pour que l'echantillon
reste equilibre dans le temps, meme si on doit s'arreter en cours de route.

SOURCE LEGITIME : ce sont les transactions de deploiement elles-memes, connues
a t_decision. On n'utilise PAS pumpfun_trades.parquet ni mcap_candles comme
features - le barème les interdit explicitement en entree de decision.
"""
import os, sys, io, json, time, subprocess
import zstandard as zstd
import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from deploy_parser import parse_tx, b58decode, PUMP_PROGRAM, DISC_CREATE

BASE_URL = "http://154.12.118.112:48110/batches"
OUT_DIR = os.path.join(ROOT, "data", "interim", "june_neg")
N_BATCHES = 2883

# Plusieurs instances tournent en parallele : le debit agrege du serveur est
# environ 4x celui d'un flux unique. Chacune prend une classe de batches
# modulo NWORK et ecrit dans son propre fichier temporaire.
WID = int(sys.argv[1]) if len(sys.argv) > 1 else 0
NWORK = int(sys.argv[2]) if len(sys.argv) > 2 else 1
TMP = os.path.join(ROOT, "data", "interim", f"batch.tmp.w{WID}.zst")


def batch_order():
    """Subdivision par bissection : tout prefixe de la liste couvre juin de
    facon uniforme. Un pas initial trop fin (32 sur 2883) concentrait les
    premiers batches sur les 5 premiers pourcents du mois, ce qui rendait le
    jeu de test inutilisable faute de negatifs en fin de periode."""
    order, seen, step = [], set(), 1024
    while step >= 1:
        for b in range(0, N_BATCHES, step):
            if b not in seen:
                seen.add(b); order.append(b)
        step //= 2
    return order


def download(idx):
    url = f"{BASE_URL}/batch_{idx:06d}.jsonl.zst"
    # Un partiel laisse par un batch precedent ferait reprendre -C - par-dessus
    # des octets d'un AUTRE fichier : on repart toujours propre.
    if os.path.exists(TMP):
        safe_remove(TMP)
    for attempt in range(6):
        r = subprocess.run(["curl", "-sS", "--connect-timeout", "30",
                            "--speed-limit", "10000", "--speed-time", "120",
                            "-C", "-", "-o", TMP, url], capture_output=True)
        # taille attendue via HEAD
        h = subprocess.run(["curl", "-sSI", "--max-time", "30", url],
                           capture_output=True, text=True).stdout
        want = next((int(l.split()[1]) for l in h.splitlines()
                     if l.lower().startswith("content-length")), None)
        have = os.path.getsize(TMP) if os.path.exists(TMP) else 0
        if want and have == want:
            return True
    return False


def safe_remove(path, tries=10):
    """Sous Windows, le lecteur zstd garde la poignee du fichier un instant
    apres la sortie du bloc ; un os.remove immediat leve PermissionError et
    tuait le recolteur des le premier batch."""
    for _ in range(tries):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            time.sleep(0.5)
    return False


def parse_batch(path, bought_mints):
    rows = []
    dctx = zstd.ZstdDecompressor(max_window_size=2**31)
    with open(path, "rb") as f, dctx.stream_reader(f) as r,          io.TextIOWrapper(r, encoding="utf-8") as txt:
        for line in txt:
            try:
                rec = json.loads(line)
            except Exception:
                break                      # fin de flux tronquee : on s'arrete proprement
            blk = rec.get("block") or {}
            txs = blk.get("transactions") or []
            bt = blk.get("blockTime") or rec.get("expected_block_time")
            for i, t in enumerate(txs):
                try:
                    ins = t["transaction"]["message"]["instructions"]
                except Exception:
                    continue
                is_deploy = False
                for ix in ins:
                    if ix.get("programId") == PUMP_PROGRAM and "data" in ix:
                        if b58decode(ix["data"])[:8] == DISC_CREATE:
                            is_deploy = True
                            break
                if not is_deploy:
                    continue
                t2 = dict(t)
                t2["slot"] = rec["slot"]
                t2["blockTime"] = bt
                t2["transactionIndex"] = i     # position parmi les tx conservees
                try:
                    feat = parse_tx(t2)
                except Exception:
                    continue
                feat["label"] = int(feat["mint"] in bought_mints)
                rows.append(feat)
    return rows


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    idx = pd.read_parquet(os.path.join(ROOT, "data", "raw", "bought_deploy_txs_index.parquet"))
    bought = set(idx.token_address.astype(str))
    print(f"{len(bought):,} mints achetes par le bot (toutes periodes)", flush=True)

    done = {int(f.split("_")[1].split(".")[0]) for f in os.listdir(OUT_DIR) if f.startswith("batch_")}
    total_rows = total_pos = 0
    t0 = time.time()
    for k, b in enumerate(batch_order()):
        if b in done or (b % NWORK) != WID:
            continue
        if os.path.exists(os.path.join(OUT_DIR, f"batch_{b:06d}.parquet")):
            continue
        t1 = time.time()
        if not download(b):
            print(f"[w{WID} {b:04d}] echec du telechargement, on passe", flush=True)
            continue
        size_mb = os.path.getsize(TMP) / 1e6
        rows = parse_batch(TMP, bought)
        safe_remove(TMP)
        if not rows:
            continue
        tbl = pa.Table.from_pylist(rows)
        pq.write_table(tbl, os.path.join(OUT_DIR, f"batch_{b:06d}.parquet"), compression="zstd")
        npos = sum(r["label"] for r in rows)
        total_rows += len(rows); total_pos += npos
        el = time.time() - t0
        print(f"[w{WID} {b:04d}] {size_mb:6.1f} MB -> {len(rows):4d} deploiements ({npos:3d} positifs) "
              f"| {time.time()-t1:5.0f}s | cumul {total_rows:,} lignes / {total_pos:,} positifs "
              f"| {el/60:.0f} min", flush=True)


if __name__ == "__main__":
    main()
