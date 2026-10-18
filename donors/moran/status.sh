#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# status.sh — funnel snapshot + verdict for today.
# Non-blocking: runs and exits.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR"
LOG="$ROOT/logs/logs_sim.err"
TODAY="$(date +%Y-%m-%d)"

if [[ ! -f "$LOG" ]]; then
  echo "error: log file not found: $LOG" >&2
  exit 1
fi

# Today-only slice of the log. Assumes standard python log prefix "YYYY-MM-DD".
today_slice() {
  grep -E "^${TODAY}" "$LOG" 2>/dev/null || true
}

count() {
  local pattern="$1"
  today_slice | grep -cE "$pattern" || true
}

last_n() {
  local n="$1" pattern="$2"
  today_slice | grep -E "$pattern" | tail -n "$n" || true
}

migrations=$(count 'DELAYED_ENTRY\] Cached')
price_unverified_skip=$(count 'PRICE_UNVERIFIED(_RETRY|_SKIPPED)?([^E]|$)')
price_unverified_exhausted=$(count 'PRICE_UNVERIFIED_EXHAUSTED')
jupiter_fallback=$(count 'JUPITER_FALLBACK')
holder_vetoed=$(count 'HOLDER_VETO|holder_concentration')
entry_score_vetoed=$(count 'ENTRY_SCORE_VETO')
all_passed=$(count 'ALL_PASSED')

if [[ "$migrations" -gt 0 ]]; then
  pass_rate=$(awk -v a="$all_passed" -v m="$migrations" 'BEGIN { printf "%.1f", (a/m)*100 }')
else
  pass_rate="0.0"
fi

live_buy_attempts=$(count 'LIVE_BUY\] mint=')
swap_failed=$(count 'SWAP_FAILED')
hard_cap_exits=$(count 'HARD_CAP_EXIT|HARD_CAPITAL_CAP_EXIT')

echo "=============================================================="
echo " STATUS — ${TODAY}"
echo " log: $LOG"
echo "=============================================================="
echo
echo "1. FUNNEL"
printf "   Migrations detected           : %s\n" "$migrations"
printf "   Price unverified skipped      : %s\n" "$price_unverified_skip"
printf "   Price unverified exhausted    : %s\n" "$price_unverified_exhausted"
printf "   Jupiter fallback used         : %s\n" "$jupiter_fallback"
printf "   Holder vetoed                 : %s\n" "$holder_vetoed"
printf "   Entry score vetoed            : %s\n" "$entry_score_vetoed"
printf "   ALL_PASSED (reached exec)     : %s\n" "$all_passed"
printf "   Pass rate                     : %s%%\n" "$pass_rate"
echo

echo "2. RECENT SIGNALS"
echo "   ── Last 5 ENTRY_SCORE ──"
last_n 5 'ENTRY_SCORE' | sed 's/^/   /'
echo
echo "   ── Last 5 HOLDER_VETO ──"
last_n 5 'HOLDER_VETO|holder_concentration' | sed 's/^/   /'
echo
echo "   ── Last 3 JUPITER_FALLBACK ──"
last_n 3 'JUPITER_FALLBACK' | sed 's/^/   /'
echo

echo "3. EXECUTION"
printf "   LIVE_BUY attempts             : %s\n" "$live_buy_attempts"
printf "   SWAP_FAILED count             : %s\n" "$swap_failed"
printf "   HARD_CAP exits                : %s\n" "$hard_cap_exits"
printf "   Holder veto count             : %s\n" "$holder_vetoed"
echo

echo "4. VERDICT"
pr_int=$(awk -v p="$pass_rate" 'BEGIN { printf "%d", p+0 }')
if   [[ "$pr_int" -ge 15 ]]; then echo "   FUNNEL: OK"
elif [[ "$pr_int" -ge 5  ]]; then echo "   FUNNEL: SLOW (market filtering heavily)"
else                              echo "   FUNNEL: BLOCKED — investigate"
fi

if [[ "$swap_failed" -gt 0 ]]; then
  echo "   SWAP_FAILED: $swap_failed — check routing"
fi

if [[ "$hard_cap_exits" -gt 5 ]]; then
  echo "   HARD_CAP: HIGH ($hard_cap_exits)"
else
  echo "   HARD_CAP: OK ($hard_cap_exits)"
fi
echo "=============================================================="
