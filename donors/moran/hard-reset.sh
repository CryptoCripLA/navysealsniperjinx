#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# hard-reset.sh — stop existing triad, reload profile, restart clean
# Usage: ./hard-reset.sh [profile_name]   default: paper (fail-safe)
# Reason for default: an arg-less invocation previously loaded the live
# profile silently, causing real-money buys when the operator expected paper.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR"
VENV="$ROOT/.venv"
LOG_DIR="$ROOT/logs"
PROFILE_FILE="${1:-paper}"
PROFILE_PATH="$ROOT/.env.${PROFILE_FILE}"

# NOTE: no trap here that kills $SIM_PID / $HUD_PID.
# nohup only suppresses SIGHUP; an EXIT trap with `kill $PID` sends SIGTERM
# which uvicorn handles as a graceful shutdown — killing the process we just
# started.  Background processes are detached via `disown` below instead.

mkdir -p "$LOG_DIR"
cd "$ROOT"

echo "==> Logging pre-restart state"
"$VENV/bin/python3" variable_logger.py scan --note "pre hard-reset scan" || true
"$VENV/bin/python3" variable_logger.py session session_stop --note "hard-reset about to stop triad" || true

echo "==> Stopping existing processes (graceful SIGTERM, 10s)"
# SIGTERM first; sim's handler writes shutting_down=true to runtime_state.json.
# Poll until both pgrep entries are gone or 10s elapses, then SIGKILL stragglers.
pkill -TERM -f "simulation_mode.py" || true
pkill -TERM -f "uvicorn sniper_hud_api:app" || true

for i in {1..10}; do
  _SIM_ALIVE=$(pgrep -f "simulation_mode.py" || true)
  _HUD_ALIVE=$(pgrep -f "uvicorn sniper_hud_api:app" || true)
  if [ -z "$_SIM_ALIVE" ] && [ -z "$_HUD_ALIVE" ]; then
    echo "    graceful shutdown complete (${i}s)"
    break
  fi
  sleep 1
  if [ "$i" -eq 10 ]; then
    echo "    WARNING: processes still alive after 10s, escalating to SIGKILL"
    pkill -KILL -f "simulation_mode.py" || true
    pkill -KILL -f "uvicorn sniper_hud_api:app" || true
    sleep 1
  fi
done

echo "==> Rotating log files >50MB"
# Must rotate while processes are down. `nohup >>` keeps writing to the old
# inode after a rename, so any online rotation gets silently lost.
for _LOG in "$LOG_DIR/logs_sim.out" "$LOG_DIR/logs_sim.err" \
            "$LOG_DIR/logs_hud.out" "$LOG_DIR/logs_hud.err"; do
  if [ -f "$_LOG" ]; then
    _SIZE=$(wc -c < "$_LOG" | tr -d ' ')
    if [ "${_SIZE:-0}" -gt 52428800 ]; then
      _LOG_TS=$(date -u +%Y%m%dT%H%M%SZ)
      mv "$_LOG" "${_LOG}.${_LOG_TS}"
      gzip "${_LOG}.${_LOG_TS}" || true
      echo "    rotated ${_LOG} (${_SIZE}B) → ${_LOG}.${_LOG_TS}.gz"
    fi
  fi
done

echo "==> Snapshotting current .env"
if [ -f "$ROOT/.env" ]; then
  _SNAP_TS=$(date -u +%Y%m%dT%H%M%SZ)
  _SNAP_DIR="$ROOT/.env_snapshots"
  mkdir -p "$_SNAP_DIR"
  cp "$ROOT/.env" "$_SNAP_DIR/.env.${_SNAP_TS}"
  echo "    saved $_SNAP_DIR/.env.${_SNAP_TS}"
fi

echo "==> Loading profile: $PROFILE_FILE"
cp "$PROFILE_PATH" "$ROOT/.env"

echo "==> Starting simulation"
nohup "$VENV/bin/python3" simulation_mode.py \
  >> "$LOG_DIR/logs_sim.out" 2>> "$LOG_DIR/logs_sim.err" &
SIM_PID=$!
# disown removes the job from bash's job table.  Even if an EXIT trap fires
# (e.g. from set -e on a later command), bash will not send any signal to a
# disowned process.  This is the only reliable guard against trap-based kills.
disown $SIM_PID
echo "    SIM started (PID $SIM_PID)"

echo "==> Starting HUD API"
nohup "$VENV/bin/python3" -m uvicorn sniper_hud_api:app \
  --host 127.0.0.1 --port 8000 --app-dir "$ROOT" \
  >> "$LOG_DIR/logs_hud.out" 2>> "$LOG_DIR/logs_hud.err" &
HUD_PID=$!
disown $HUD_PID
echo "    HUD started (PID $HUD_PID)"

echo "==> Waiting for health"
for i in {1..20}; do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "    Health OK (attempt $i)"
    break
  fi
  if [ "$i" -eq 20 ]; then
    echo "ERROR: Health check timed out after 20s — HUD did not come up"
    echo "--- last 10 lines of $LOG_DIR/logs_hud.err ---"
    tail -10 "$LOG_DIR/logs_hud.err" 2>/dev/null || true
    echo "---"
    exit 1
  fi
  sleep 1
done

echo "==> Process check"
ps -p "$SIM_PID" -o pid=,command= 2>/dev/null || echo "  WARNING: SIM process $SIM_PID not found"
ps -p "$HUD_PID" -o pid=,command= 2>/dev/null || echo "  WARNING: HUD process $HUD_PID not found"

echo "==> Health check"
curl -fsS http://127.0.0.1:8000/health || {
  echo
  echo "Health check failed. Recent logs:"
  tail -10 "$LOG_DIR/logs_hud.err" 2>/dev/null || true
  exit 1
}

echo
echo "OK: triad restarted"
echo "Profile copied to .env from: $PROFILE_FILE"
echo "Simulation PID: $SIM_PID"
echo "HUD PID: $HUD_PID"

echo "==> Queueing metrics-only session reset"
curl -fsS -X POST http://127.0.0.1:8000/operator/reset_session \
  -H "Content-Type: application/json" \
  -d '{"mode":"metrics_only"}' || {
  echo "Session reset request failed (non-fatal)"
}

# ---------------------------------------------------------------------------
# Final health check: 3s after the script would normally exit, confirm both
# processes are still alive.  If either died, print its last 10 log lines.
# This catches EXIT-trap kills, immediate crashes, and port conflicts.
# ---------------------------------------------------------------------------
echo "==> Final health check (3s)"
sleep 3

_FINAL_OK=true

if ! kill -0 "$SIM_PID" 2>/dev/null; then
  _FINAL_OK=false
  echo "ERROR: Simulation (PID $SIM_PID) has died"
  echo "--- last 10 lines of $LOG_DIR/logs_sim.err ---"
  tail -10 "$LOG_DIR/logs_sim.err" 2>/dev/null || echo "(log not readable)"
  echo "---"
fi

if ! kill -0 "$HUD_PID" 2>/dev/null; then
  _FINAL_OK=false
  echo "ERROR: HUD (PID $HUD_PID) has died"
  echo "--- last 10 lines of $LOG_DIR/logs_hud.err ---"
  tail -10 "$LOG_DIR/logs_hud.err" 2>/dev/null || echo "(log not readable)"
  echo "---"
fi

if $_FINAL_OK; then
  echo "==> Both processes alive and healthy"
  echo "    SIM PID: $SIM_PID"
  echo "    HUD PID: $HUD_PID"
  echo "==> Logging session_start to variable_changes.jsonl"
  "$VENV/bin/python3" variable_logger.py scan --note "post hard-reset scan" || true
  "$VENV/bin/python3" variable_logger.py session session_start \
    --note "hard-reset profile=$PROFILE_FILE sim_pid=$SIM_PID hud_pid=$HUD_PID" || true
fi
