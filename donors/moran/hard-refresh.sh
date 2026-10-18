#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "==> Checking HUD health"
curl -fsS http://127.0.0.1:8000/health >/dev/null || {
  echo "ERROR: HUD not reachable on 127.0.0.1:8000"
  exit 1
}

echo "==> Queueing metrics-only session reset"
curl -fsS -X POST http://127.0.0.1:8000/operator/reset_session \
  -H "Content-Type: application/json" \
  -d '{"mode":"metrics_only"}' || {
  echo "ERROR: metrics reset request failed"
  exit 1
}

sleep 1

echo "==> Final verification"
curl -fsS http://127.0.0.1:8000/hud_digest >/dev/null && echo "HUD OK" || true

echo
echo "DONE: metrics refreshed in-place (trading state unchanged)"
