#!/usr/bin/env bash
D="$(dirname "$0")/../data/raw"; mkdir -p "$D"
for f in 5brv79e_activity.parquet 5brv79e_activity_txs.jsonl.gz 5brv79e_activity_txs_index.parquet; do
  curl -sS --retry 5 -C - -o "$D/$f" "http://154.12.118.112:48114/$f" && echo "$f ok"
done
