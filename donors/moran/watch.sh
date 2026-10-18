#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# watch.sh — live log watcher with selectable views
# Usage:
#   ./watch.sh           # default: key signals
#   ./watch.sh entry     # entry scoring only
#   ./watch.sh funnel    # pipeline funnel only
#   ./watch.sh buys      # buy/sell execution only
#   ./watch.sh all       # everything (verbose, no filter)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR"
LOG="$ROOT/logs/logs_sim.err"

MODE="${1:-default}"

if [[ ! -f "$LOG" ]]; then
  echo "error: log file not found: $LOG" >&2
  exit 1
fi

case "$MODE" in
  default)
    PATTERN='ENTRY_SCORE|HOLDER_VETO|JUPITER_FALLBACK|SWAP_FAILED|LIVE_BUY timing|HARD_CAP'
    ;;
  entry)
    PATTERN='ENTRY_SCORE|HOLDER_VETO|PRICE_UNVERIFIED'
    ;;
  funnel)
    PATTERN='DELAYED_ENTRY.*Cached|DELAYED_ENTRY_HOLDER|JUPITER_FALLBACK|PRICE_UNVERIFIED_EXHAUSTED|ALL_PASSED|ENTRY_SCORE'
    ;;
  buys)
    PATTERN='LIVE_BUY timing|LIVE_SELL|HARD_CAP|RAPID.DUMP|TRAIL'
    ;;
  all)
    echo "==> watching $LOG (no filter)"
    exec tail -f "$LOG"
    ;;
  *)
    echo "usage: $0 [default|entry|funnel|buys|all]" >&2
    exit 2
    ;;
esac

echo "==> watching $LOG | mode=$MODE"
echo "==> pattern: $PATTERN"
exec tail -f "$LOG" | grep -E --line-buffered "$PATTERN"
