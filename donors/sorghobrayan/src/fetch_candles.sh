#!/usr/bin/env bash
# Serveur nginx : Range supporte -> 6 morceaux en parallele, puis concatenation.
URL="http://154.12.118.112:48113/mcap_candles.parquet"
D="/c/Users/techa/Desktop/solana-sniper/data/raw"; TOTAL=2840322917; N=6
CH=$(( (TOTAL + N - 1) / N ))
for i in $(seq 0 $((N-1))); do
  s=$((i*CH)); e=$((s+CH-1)); [ $e -ge $TOTAL ] && e=$((TOTAL-1))
  ( for a in 1 2 3 4 5; do
      have=$(stat -c%s "$D/cand.$i" 2>/dev/null || echo 0); want=$((e-s+1))
      [ "$have" -eq "$want" ] && break
      # pas de -C - : incompatible avec -r. La reprise se fait via l'offset $have.
      curl -sS --connect-timeout 30 -r $((s+have))-$e "$URL" >> "$D/cand.$i" || true
    done ) &
done
wait
cat $D/cand.0 $D/cand.1 $D/cand.2 $D/cand.3 $D/cand.4 $D/cand.5 > $D/mcap_candles.parquet
echo "size=$(stat -c%s $D/mcap_candles.parquet) attendu=$TOTAL"
[ "$(stat -c%s $D/mcap_candles.parquet)" -eq "$TOTAL" ] && rm -f $D/cand.[0-5] && echo "CANDLES OK"
