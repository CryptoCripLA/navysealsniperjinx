#!/usr/bin/env bash
# Le serveur coupe toutes les 1-4 h et n'accepte pas les Range : une tentative
# unique ne peut pas tenir les ~3 h necessaires pour atteindre le membre 5.
# On ouvre donc N flux independants dans des repertoires separes. Chacun a sa
# propre chance de survivre ; le promoteur recupere tout membre COMPLET chez
# n'importe quel worker et le fige dans data/raw.
#
# Regle d'or : on ne remplace JAMAIS un membre complet par un partiel
# (le bug de la version precedente, qui faisait reculer la progression).
BASE="/c/Users/techa/Desktop/solana-sniper"
D="$BASE/data/raw"; S="$BASE/data/stream"
URL="http://65.21.203.147:48102/half_year_dataset.tar"
N=4
MEMBERS="bought_deploy_txs.jsonl.gz bought_deploy_txs_index.parquet bought_deployers_activity.parquet not_bought_deploy_txs.jsonl.gz not_bought_deploy_txs_index.parquet not_bought_deployers_activity.parquet"

check() {
  [ -s "$1" ] || return 1
  case "$1" in
    *.parquet) python -c "import pyarrow.parquet as pq,sys; pq.ParquetFile(sys.argv[1]).metadata" "$1" 2>/dev/null ;;
    *.gz)      gzip -t "$1" 2>/dev/null ;;
    *) return 1 ;;
  esac
}

worker() {
  local i=$1; local w="$S/w$i"
  mkdir -p "$w"
  for a in $(seq 1 30); do
    # on s'arrete des que le promoteur a tout fige
    if [ -f "$S/DONE" ]; then return 0; fi
    echo "[w$i] tentative $a $(date -u +%H:%M:%S)"
    curl -sS --connect-timeout 30 --speed-limit 20000 --speed-time 240 "$URL" \
      | tar -xf - -C "$w" 2>/dev/null
    rm -f "$w"/*.parquet.tmp 2>/dev/null
  done
}

promoter() {
  while [ ! -f "$S/DONE" ]; do
    for m in $MEMBERS; do
      if ! check "$D/$m"; then
        for i in $(seq 1 $N); do
          if check "$S/w$i/$m"; then
            cp "$S/w$i/$m" "$D/$m.new" && mv "$D/$m.new" "$D/$m"
            echo "[promo] $m fige depuis w$i ($(stat -c%s "$D/$m") octets) $(date -u +%H:%M:%S)"
            break
          fi
        done
      fi
    done
    ok=1
    for m in $MEMBERS; do check "$D/$m" || ok=0; done
    if [ $ok -eq 1 ]; then touch "$S/DONE"; echo "[promo] TOUS LES MEMBRES SONT FIGES"; return 0; fi
    # etat des lieux
    printf "[etat %s]" "$(date -u +%H:%M)"
    for i in $(seq 1 $N); do
      f="$S/w$i/not_bought_deploy_txs.jsonl.gz"
      printf " w%d=%.1fG" "$i" "$(echo "$(stat -c%s "$f" 2>/dev/null || echo 0)/1073741824" | python -c "import sys;print(eval(sys.stdin.read()))" 2>/dev/null || echo 0)"
    done
    printf "\n"
    sleep 300
  done
}

mkdir -p "$S"; rm -f "$S/DONE"
for i in $(seq 1 $N); do worker $i & done
promoter &
wait
