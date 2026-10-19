#!/usr/bin/env bash
# Le serveur core est un SimpleHTTP Python : il IGNORE les Range, donc une
# coupure impose de repartir de l'octet 0. Ecrire dans un .tar monolithique
# perd alors TOUT (vecu : 19.9 GB perdus a 48%).
#
# On extrait donc EN FLUX : chaque membre complete est ecrit sur disque et
# survit definitivement a une coupure. L'ordre des membres dans l'archive est
# alphabetique, donc les 5M de transactions negatives (membre 4) et leur index
# (membre 5) sont acquis des ~16 GiB, bien avant l'activite des deployeurs.
URL="http://65.21.203.147:48102/half_year_dataset.tar"
D="/c/Users/techa/Desktop/solana-sniper/data/raw"
cd "$D" || exit 1

# Tailles exactes lues dans les en-tetes tar de l'archive.
declare -A SIZES=(
  [bought_deploy_txs.jsonl.gz]=50815788
  [bought_deploy_txs_index.parquet]=1947279
  [bought_deployers_activity.parquet]=656959815
  [not_bought_deploy_txs.jsonl.gz]=14955899885
)

check() {
  [ -f "$1" ] || return 1
  local have want
  have=$(stat -c%s "$1")
  want=${SIZES[$1]}
  # Taille connue : test O(1). Un gzip -t sur 10 GB partiels coutait des minutes
  # a chaque tentative, pour un echec certain.
  if [ -n "$want" ]; then [ "$have" -eq "$want" ]; return $?; fi
  # Taille inconnue : la lecture du pied de page parquet reste peu couteuse.
  case "$1" in
    *.parquet) python -c "import pyarrow.parquet as pq,sys; pq.ParquetFile(sys.argv[1]).metadata; print('ok')" "$1" 2>/dev/null | grep -q ok ;;
    *)         [ "$have" -gt 1000000 ] ;;
  esac
}

status() {
  echo "--- etat des membres $(date -u +%H:%M:%S) ---"
  for f in bought_deploy_txs.jsonl.gz bought_deploy_txs_index.parquet \
           bought_deployers_activity.parquet not_bought_deploy_txs.jsonl.gz \
           not_bought_deploy_txs_index.parquet not_bought_deployers_activity.parquet; do
    if [ -f "$f" ]; then
      if check "$f"; then echo "  COMPLET  $f ($(stat -c%s "$f"))"
      else echo "  partiel  $f ($(stat -c%s "$f"))"; fi
    else echo "  absent   $f"; fi
  done
}

for attempt in $(seq 1 12); do
  status
  if check not_bought_deploy_txs.jsonl.gz && check not_bought_deploy_txs_index.parquet \
     && check not_bought_deployers_activity.parquet; then
    echo "=== TOUS LES MEMBRES SONT COMPLETS ==="; exit 0
  fi
  echo "=== tentative $attempt : $(date -u +%H:%M:%S) ==="
  # --speed-time 300 : on tolere 5 min de lenteur avant d'abandonner la tentative
  curl -sS --connect-timeout 30 --speed-limit 30000 --speed-time 300 "$URL" \
    | tar -xf - 2>&1 | tail -3
  echo "fin de tentative $attempt"
done
status
echo "=== ABANDON apres 12 tentatives ==="
