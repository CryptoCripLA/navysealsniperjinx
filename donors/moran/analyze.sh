#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# analyze.sh — run the full post-batch analysis suite in one pass
# Usage:
#   ./analyze.sh             # defaults: scorecard/attribution N=20/30
#   ./analyze.sh --n 10      # override scorecard/attribution N
# Output:
#   - streamed to terminal (tee)
#   - saved to logs/analysis_YYYY-MM-DD_HHMMSS.txt
# ---------------------------------------------------------------------------
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR"
cd "$ROOT"

LOG_DIR="$ROOT/logs"
SIM_ERR="$LOG_DIR/logs_sim.err"
mkdir -p "$LOG_DIR"

# ── Arg parse ────────────────────────────────────────────────────────────────
OVERRIDE_N=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --n)
      OVERRIDE_N="${2:-}"
      shift 2
      ;;
    -h|--help)
      sed -n '2,10p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

SCORECARD_N="${OVERRIDE_N:-20}"
ATTRIB_N="${OVERRIDE_N:-30}"
EXITLAB_N=30
REPLAY_EXP_N=50

TS="$(date +%Y-%m-%d_%H%M%S)"
REPORT="$LOG_DIR/analysis_${TS}.txt"
TODAY_YMD="$(date +%Y-%m-%d)"

# ── Python shim (prefer .venv if present) ────────────────────────────────────
if [[ -x "$ROOT/.venv/bin/python3" ]]; then
  PY="$ROOT/.venv/bin/python3"
else
  PY="python3"
fi

# ── Read .env values (no sourcing; grep to avoid executing comment text) ─────
if [[ ! -f "$ROOT/.env" ]]; then
  echo "error: $ROOT/.env not found — cannot resolve profile/config values" >&2
  exit 1
fi

env_get() {
  local key="$1"
  local val
  val="$(grep -E "^${key}=" "$ROOT/.env" 2>/dev/null | tail -1 | cut -d'=' -f2- | tr -d '"' | tr -d "'")"
  printf '%s' "${val:-<unset>}"
}

TRADE_SIZE="$(env_get CANARY_TRADE_SIZE_USD)"
DAILY_CAP="$(env_get CANARY_MAX_DAILY_LOSS_USD)"
MAX_CONCURRENT="$(env_get CANARY_MAX_CONCURRENT)"
ACTIVE_PROFILE="$(env_get ACTIVE_PROFILE)"

# ── Everything from here on tees into $REPORT ────────────────────────────────
{
  echo "════════════════════════════════════════════════════════════════════════"
  echo " SOLANA MICRO-SNIPER — ANALYSIS REPORT"
  echo " Generated: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo " Report:    $REPORT"
  echo "════════════════════════════════════════════════════════════════════════"
  echo
  echo "── CONFIG (.env) ────────────────────────────────────────────────────────"
  printf "  ACTIVE_PROFILE        %s\n" "$ACTIVE_PROFILE"
  printf "  trade size (USD)      %s\n" "$TRADE_SIZE"
  printf "  daily loss cap (USD)  %s\n" "$DAILY_CAP"
  printf "  max concurrent        %s\n" "$MAX_CONCURRENT"
  echo
  echo "── RECENT TELEMETRY (logs_sim.err) ──────────────────────────────────────"
  if [[ -f "$SIM_ERR" ]]; then
    echo "  Last 5 DELAYED_ENTRY_HOLDER_VETO lines:"
    grep -E "DELAYED_ENTRY_HOLDER_VETO" "$SIM_ERR" 2>/dev/null | tail -5 | sed 's/^/    /' \
      || echo "    (none)"
    echo
    echo "  Last 5 SWAP_FAILED lines:"
    grep -E "SWAP_FAILED" "$SIM_ERR" 2>/dev/null | tail -5 | sed 's/^/    /' \
      || echo "    (none)"
    echo
    PU_COUNT="$(grep -E "PRICE_UNVERIFIED_SKIP" "$SIM_ERR" 2>/dev/null | grep -c "$TODAY_YMD" || true)"
    PU_COUNT="${PU_COUNT:-0}"
    printf "  PRICE_UNVERIFIED_SKIP count today (%s): %s\n" "$TODAY_YMD" "$PU_COUNT"
  else
    echo "  WARNING: logs_sim.err not found at $SIM_ERR — telemetry section skipped" >&2
    echo "  WARNING: logs_sim.err not found at $SIM_ERR"
    PU_COUNT=0
  fi
  echo

  run_step() {
    local title="$1"; shift
    echo "════════════════════════════════════════════════════════════════════════"
    echo " $title"
    echo " cmd: $*"
    echo "════════════════════════════════════════════════════════════════════════"
    "$@" || echo "[analyze.sh] step FAILED (exit=$?): $*"
    echo
  }

  run_step "[1/5] batch_scorecard  (N=$SCORECARD_N)"      "$PY" batch_scorecard.py --n "$SCORECARD_N"
  run_step "[2/5] trade_attribution (N=$ATTRIB_N)"        "$PY" trade_attribution.py --n "$ATTRIB_N"
  run_step "[3/5] exit_lab           (N=$EXITLAB_N)"      "$PY" exit_lab.py --n "$EXITLAB_N"

  echo "════════════════════════════════════════════════════════════════════════"
  echo " [4/5] replay_v2 (writes REPLAY_REPORT_V2.md)"
  echo "════════════════════════════════════════════════════════════════════════"
  "$PY" replay_v2.py || echo "[analyze.sh] replay_v2 FAILED (exit=$?)"
  echo
  if [[ -f "$ROOT/REPLAY_REPORT_V2.md" ]]; then
    echo "── REPLAY_REPORT_V2.md ──"
    cat "$ROOT/REPLAY_REPORT_V2.md"
  else
    echo "(REPLAY_REPORT_V2.md not produced)"
  fi
  echo

  run_step "[5/5] replay_experiment_runner (N=$REPLAY_EXP_N)" "$PY" replay_experiment_runner.py --n "$REPLAY_EXP_N"
} 2>&1 | tee "$REPORT"

# ── One-line summary (read back from the saved report) ──────────────────────
# Strip ANSI colour escapes from the report copy so grep sees plain text.
TMP_PLAIN="$(mktemp -t analyze_plain.XXXXXX)"
# shellcheck disable=SC2016
LC_ALL=C sed $'s/\x1b\\[[0-9;]*[a-zA-Z]//g' "$REPORT" > "$TMP_PLAIN"

# Scorecard verdict: grep the indented "  PASS" / "  FAIL" verdict line directly
# (the section header "VERDICT" is not the verdict itself).
VERDICT="$(grep -E '^  (PASS|FAIL)$' "$TMP_PLAIN" | head -1 | awk '{print $1}')"
VERDICT="${VERDICT:-UNKNOWN}"

# HARD_CAP count: look at trade_attribution's EXIT bucket summary.
# Fall back to any HARD_CAP line count in the attribution section.
HARD_CAP_COUNT="$(grep -cE "EXIT_HARD_CAP" "$TMP_PLAIN" 2>/dev/null || echo 0)"
HARD_CAP_COUNT="${HARD_CAP_COUNT:-0}"

EXEC_FAIL_COUNT="$(grep -cE "EXEC_SWAP_FAILED" "$TMP_PLAIN" 2>/dev/null || echo 0)"
EXEC_FAIL_COUNT="${EXEC_FAIL_COUNT:-0}"

rm -f "$TMP_PLAIN"

SUMMARY="SUMMARY: verdict=${VERDICT}  hard_cap=${HARD_CAP_COUNT}  exec_swap_failed=${EXEC_FAIL_COUNT}"
{
  echo
  echo "════════════════════════════════════════════════════════════════════════"
  echo "$SUMMARY"
  echo "════════════════════════════════════════════════════════════════════════"
} | tee -a "$REPORT"

echo
echo "Full report saved to: $REPORT"
