#!/usr/bin/env bash
# start_hud.sh — hardened HUD server launcher
# Always uses the project venv Python to avoid wrong-interpreter failures.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python3"
HUD_API="$SCRIPT_DIR/sniper_hud_api.py"
PORT="${HUD_PORT:-8000}"
HOST="${HUD_HOST:-127.0.0.1}"

# Validate venv
if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "[start_hud] ERROR: venv python not found at $VENV_PYTHON" >&2
    echo "[start_hud] Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

# Validate HUD api file
if [[ ! -f "$HUD_API" ]]; then
    echo "[start_hud] ERROR: $HUD_API not found" >&2
    exit 1
fi

# Syntax check before launching
if ! "$VENV_PYTHON" -m py_compile "$HUD_API" 2>&1; then
    echo "[start_hud] ERROR: syntax check failed for sniper_hud_api.py" >&2
    exit 1
fi

# Kill any existing HUD server on this port
if lsof -ti tcp:"$PORT" &>/dev/null; then
    echo "[start_hud] Stopping existing process on port $PORT..."
    lsof -ti tcp:"$PORT" | xargs kill -TERM 2>/dev/null || true
    sleep 1
fi

echo "[start_hud] Starting HUD on $HOST:$PORT using $VENV_PYTHON"
cd "$SCRIPT_DIR"
exec "$VENV_PYTHON" -m uvicorn sniper_hud_api:app \
    --host "$HOST" \
    --port "$PORT" \
    --loop asyncio \
    --no-access-log
