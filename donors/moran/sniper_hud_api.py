import os
import re
import json
import time
import uuid
import asyncio
import fcntl
from pathlib import Path

_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _validate_mint(mint: str) -> str:
    if not isinstance(mint, str) or not _BASE58_RE.match(mint):
        raise HTTPException(status_code=400, detail="mint must be a base58 string (32-44 chars)")
    return mint
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from dotenv import load_dotenv

load_dotenv(override=True)  # .env must win over inherited shell env (same fix as simulation_mode.py)

# Pre-import live_canary at module level so the 10-14s slow import happens
# once at server startup rather than blocking the event loop on the first request.
_live_canary = None
try:
    import live_canary as _live_canary
except Exception:
    pass

app = FastAPI(title="Sniper HUD API")
BASE = Path(__file__).parent
STATE_FILE = BASE / "runtime_state.json"
STATE_LOCK_FILE = BASE / ".runtime_state.lock"
HISTORY_FILE = BASE / "history.json"
# P5.1 / P5.6: operator commands are an append-only jsonl consumed by the sim
# engine. HUD never writes runtime_state.json — the sim loop owns that file.
OPERATOR_COMMANDS_FILE = BASE / "data" / "operator_commands.jsonl"
OPERATOR_COMMANDS_LOCK = BASE / "data" / ".operator_commands.lock"
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0, s-maxage=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

SNIPER_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SNIPER_TG_CHAT_ID = os.getenv("SNIPER_ALERT_CHAT_ID", "")
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
WALLET_PUBKEY = os.getenv("WALLET_PUBKEY", "")
JITO_BLOCK_ENGINE_URL = os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf/api/v1/bundles")

CLIENTS: set[WebSocket] = set()
CLIENT_LAST_PONG: dict[WebSocket, float] = {}
WS_PING_INTERVAL_SEC = 30
WS_STALE_TIMEOUT_SEC = 90
LAST_WARN_TS = 0
HUD_HEALTH_HISTORY: list[dict] = []

# ── /hud_digest cache — avoids running the full enrich path on every 2s frontend poll ──
_DIGEST_CACHE_TTL_S = 1.0  # serve cached digest for up to 1s
_digest_cache: dict | None = None
_digest_cache_ts: float = 0.0
HUD_JITO_BUNDLE_TOTAL = 0
HUD_JITO_BUNDLE_SUCCESS = 0


def _safe_int(v) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def _position_taxonomy_bucket(row: dict) -> tuple[str, str]:
    row = dict(row or {})
    status = str(row.get("status") or "")
    qty_remaining = float(row.get("qty_remaining") or 0.0)
    terminalized_ts = _safe_int(row.get("terminalized_ts"))
    if status == "closed":
        return "TERMINALIZED", "closed row; audit-only lifecycle"
    if qty_remaining > 1e-9:
        if bool(row.get("mark_stale")):
            return "ACTIVE", "wallet-backed managed position with stale mark; remains actively managed"
        return "ACTIVE", "wallet-backed managed position; actively managed and contributes to unrealized"
    if terminalized_ts:
        return "TERMINALIZED", "terminalized zero-quantity row; audit-only lifecycle"
    return "ORPHANED", "zero quantity row retained for audit/history; not actively managed"


def _threshold_context_from_reason(reason: str, current_policy_usd: float) -> dict:
    import re
    reason = str(reason or "")
    m = re.search(r"Liquidity\s*<\s*\$?([0-9]+(?:\.[0-9]+)?)", reason, re.I)
    historical = float(m.group(1)) if m else None
    historical_note = None
    if historical is not None:
        if abs(historical - float(current_policy_usd or 0.0)) < 1e-9:
            historical_note = "historical threshold matches current policy"
        else:
            historical_note = "historical-at-event threshold; not a current policy mismatch"
    return {
        "historical_threshold_usd": historical,
        "current_policy_threshold_usd": float(current_policy_usd or 0.0),
        "historical_context_only": historical is not None and abs(historical - float(current_policy_usd or 0.0)) >= 1e-9,
        "historical_note": historical_note,
    }


def read_state():
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text())
            st.setdefault("operator_commands", [])
            st.setdefault("operator_controls", {
                "disable_new_entries": False,
                "default_downside_stop_pct": float(os.getenv("DEFAULT_DOWNSIDE_STOP_PCT", "0.12")),
                "absolute_failsafe_stop_pct": float(os.getenv("ABSOLUTE_FAILSAFE_STOP_PCT", "0.20")),
            })
            return st
        except Exception:
            pass
    return {"killswitch": False, "avg_latency_ms": 0.0, "pnl_usd": 0.0, "updated_at": int(time.time()), "operator_commands": [], "operator_controls": {"disable_new_entries": False, "default_downside_stop_pct": float(os.getenv("DEFAULT_DOWNSIDE_STOP_PCT", "0.12")), "absolute_failsafe_stop_pct": float(os.getenv("ABSOLUTE_FAILSAFE_STOP_PCT", "0.22"))}}


# P5.1: _locked_state_update removed from HUD — the HUD no longer writes
# runtime_state.json. All operator actions go through the append-only
# operator_commands.jsonl below, which the sim loop tails.
def enqueue_operator_command(action: str, **payload):
    """Append one operator command to OPERATOR_COMMANDS_FILE (P5.1 + P5.6).
    Commands are never truncated — full audit trail. Sim engine tracks a
    cursor (highest processed cmd_id) in runtime_state.
    """
    cmd = {
        "cmd_id": uuid.uuid4().hex,
        "action": action,
        "ts": int(time.time()),
        **payload,
    }
    OPERATOR_COMMANDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = None
    try:
        lock_fh = open(OPERATOR_COMMANDS_LOCK, "a+")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        with open(OPERATOR_COMMANDS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(cmd) + "\n")
            f.flush()
            os.fsync(f.fileno())
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                lock_fh.close()
            except Exception:
                pass
    return cmd




def _annotate_position(row: dict) -> dict:
    row = dict(row or {})
    anomalies = []
    raw_source = str(row.get("mark_price_source") or row.get("current_px_source") or "fallback")
    _total_marks = int(row.get("total_mark_observations") or 0)
    _no_live_mark = (_total_marks == 0) and (raw_source in ("fallback", "entry_reference", "entry_reference_stale"))
    # When no live mark has ever been observed, the stored unrealized_roi_pct
    # is purely fee-drag at entry price — misleading as a live ROI signal.
    # Zero it out and flag the position so the HUD can show "PENDING MARK"
    # instead of a stale negative number.
    if _no_live_mark:
        row["unrealized_roi_pct"] = 0.0
        row["mark_pending"] = True
    else:
        row["mark_pending"] = False
    roi = float(row.get("unrealized_roi_pct") or 0.0)
    source_map = {
        "executable_notional_quote": "executable_notional_quote",
        "live_quote": "mid_price",
        "live_quote_recent": "mid_price",
        "dexscreener": "dexscreener",
        "rpc_reserves": "rpc_reserves",
        "entry_reference": "fallback",
        "entry_reference_stale": "stale",
        "stale_hold": "stale",
        "mid_price": "mid_price",
        "fallback": "fallback",
        "stale": "stale",
    }
    source = source_map.get(raw_source, "fallback")
    confidence = str(row.get("mark_confidence_level") or ({"executable_notional_quote":"HIGH","mid_price":"MED","dexscreener":"MED","rpc_reserves":"LOW","fallback":"LOW","stale":"LOW"}.get(source, "LOW"))).upper()
    row["mark_price_source"] = source
    row["mark_confidence_level"] = confidence
    row["mark_notional_size_used"] = float(row.get("mark_notional_size_used") or 0.0)
    realized_roi = float(row.get("realized_roi_pct") or 0.0)
    mark_stale = bool(row.get("mark_stale"))
    mark_age = float(row.get("mark_staleness_seconds") or 0.0)
    current_px = float(row.get("current_px") or row.get("last_price") or 0.0)
    entry_px = float(row.get("entry_price") or row.get("entry_px") or 0.0)
    high_px = float(row.get("high_px") or 0.0)

    if mark_stale:
        anomalies.append({"code": "stale_mark", "severity": "warn", "detail": f"mark stale {int(mark_age)}s"})
    if source != "executable_notional_quote":
        anomalies.append({"code": "roi_indicative", "severity": "info", "detail": f"ROI indicative only — source={source} confidence={confidence}"})
    if abs(roi) >= 1000:
        anomalies.append({"code": "roi_outlier", "severity": "warn", "detail": f"unrealized ROI {roi:.2f}%"})
    if abs(realized_roi) >= 1000:
        anomalies.append({"code": "realized_roi_outlier", "severity": "warn", "detail": f"realized ROI {realized_roi:.2f}%"})
    if entry_px > 0 and current_px > 0 and (current_px / entry_px) >= 100:
        anomalies.append({"code": "mark_jump_gt_100x", "severity": "warn", "detail": f"mark jump {current_px / entry_px:.1f}x"})
    if high_px > 0 and current_px > 0 and (high_px / current_px) >= 25:
        anomalies.append({"code": "retraced_from_peak", "severity": "info", "detail": f"retraced {(1 - (current_px / high_px)) * 100:.1f}% from peak"})

    row["anomalies"] = anomalies
    row["has_anomaly"] = bool(anomalies)
    return row



_history_cache: list = []
_history_cache_ts: float = 0.0
_HISTORY_CACHE_TTL_S: float = 10.0  # re-read at most once per 10s — file is 1-2MB, reads are slow under concurrent writes
_history_local = __import__("threading").local()  # per-call pin so enrich_hud_state never re-reads mid-call

# Rollup result cache — eliminates repeated O(N) scans of 2941+ history events on
# every HUD poll. Each rollup function checks this before scanning history.
# Key = (_history_cache_ts, <function-specific discriminator>).
# Invalidated automatically when history is re-read (TTL or new events).
_rollup_cache: dict = {
    "latency_key": None,   "latency": None,
    "lifetime_key": None,  "lifetime": None,
    "session": {},         # session_key → result (bounded by unique session_start_ts values)
}

def _read_history() -> list:
    global _history_cache, _history_cache_ts
    # Thread-local pin: within a single enrich_hud_state call, all 8+ calls
    # use the same snapshot so the cache never expires mid-call.
    pinned = getattr(_history_local, "pinned", None)
    if pinned is not None:
        return pinned
    now = time.time()
    if now - _history_cache_ts < _HISTORY_CACHE_TTL_S:
        return _history_cache
    if not HISTORY_FILE.exists():
        _history_cache = []
        _history_cache_ts = now
        return []
    try:
        # P5.9: shared-lock prevents torn reads if sim is mid-write. Sim acquires
        # LOCK_EX for its atomic replace; our LOCK_SH blocks until that completes.
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                data = f.read()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        _history_cache = json.loads(data)
        _history_cache_ts = now
        return _history_cache
    except Exception:
        return _history_cache  # return stale on parse error rather than empty



def _latest_runtime_policy_threshold(st: dict | None = None) -> float:
    st = dict(st or {})
    current = st.get("min_liquidity_usd_active")
    # Prefer current runtime state written by SimEngine (startup + any live changes)
    if current is not None:
        try:
            return float(current)
        except Exception:
            pass
    # Fall back: most recent execution_attempt in history carries the threshold-at-event
    hist = _read_history()
    for row in reversed(hist):
        if str(row.get("type") or "") != "execution_attempt":
            continue
        v = row.get("min_liquidity_usd_active")
        if v is not None:
            try:
                return float(v)
            except Exception:
                break
    return 0.0


def _history_latency_rollup() -> dict:
    _key = _history_cache_ts
    if _rollup_cache["latency_key"] == _key and _rollup_cache["latency"] is not None:
        return _rollup_cache["latency"]
    hist = _read_history()
    execs = [x for x in hist if str(x.get("type") or "") == "execution_attempt" and bool(x.get("ok", True))]
    avg_total = round(sum(float(x.get("latency_ms") or 0.0) for x in execs) / len(execs), 6) if execs else 0.0
    avg_exec = round(sum(float(x.get("execution_latency_ms") or 0.0) for x in execs) / len(execs), 6) if execs else 0.0
    avg_pre_exec = round(max(0.0, avg_total - avg_exec), 6)
    def avg_component(key: str) -> float:
        vals = [float((x.get("decision_breakdown_ms") or {}).get(key) or 0.0) for x in execs if x.get("decision_breakdown_ms")]
        return round(sum(vals) / len(vals), 6) if vals else 0.0
    p95_total = sorted(float(x.get("latency_ms") or 0.0) for x in execs)
    p95_exec = sorted(float(x.get("execution_latency_ms") or 0.0) for x in execs)
    def p95(vals):
        if not vals:
            return 0.0
        idx = max(0, min(len(vals) - 1, int(round(0.95 * (len(vals) - 1)))))
        return round(vals[idx], 6)
    _latency_result = {
        "execution_attempts": len(execs),
        "avg_total_latency_ms": avg_total,
        "avg_execution_latency_ms": avg_exec,
        "avg_pre_execution_latency_ms": avg_pre_exec,
        "avg_dependency_delay_ms": avg_component("price_meta_rug_ms"),
        "avg_rpc_wait_ms": avg_component("rpc_wait_ms"),
        "avg_metadata_lookup_ms": avg_component("metadata_lookup_ms"),
        "avg_price_lookup_ms": avg_component("price_lookup_ms"),
        "avg_rugcheck_primary_ms": avg_component("rugcheck_primary_ms"),
        "avg_fallback_ms": avg_component("fallback_ms"),
        "avg_fast_veto_checks_ms": avg_component("fast_veto_checks_ms"),
        "avg_serial_gap_ms": avg_component("serial_gap_ms"),
        "avg_rug_check_ms": avg_component("rug_check_ms"),
        "avg_decision_finalize_ms": avg_component("decision_finalize_ms"),
        "p50_total_latency_ms": round(sorted(float(x.get("latency_ms") or 0.0) for x in execs)[len(execs)//2], 6) if execs else 0.0,
        "p95_total_latency_ms": p95(p95_total),
        "p95_execution_latency_ms": p95(p95_exec),
        # P1-1 / P1-2: detect → execution stage latency (populated by new instrumentation)
        "avg_detect_to_exec_ms": round(
            sum(float(x["detect_to_exec_ms"]) for x in execs if x.get("detect_to_exec_ms") is not None)
            / max(1, sum(1 for x in execs if x.get("detect_to_exec_ms") is not None)),
            2,
        ) if any(x.get("detect_to_exec_ms") is not None for x in execs) else None,
        "source": "history.json:execution_attempt",
    }
    _rollup_cache["latency_key"] = _key
    _rollup_cache["latency"] = _latency_result
    return _latency_result



def _history_lifetime_rollup(st: dict | None = None) -> dict:
    _n_pos = len((st or {}).get("paper_positions") or [])
    _key = (_history_cache_ts, _n_pos)
    if _rollup_cache["lifetime_key"] == _key and _rollup_cache["lifetime"] is not None:
        return _rollup_cache["lifetime"]
    hist = _read_history()
    st = dict(st or {})
    trade_closes = [x for x in hist if str(x.get("type") or "") == "trade_close"]
    execution_attempts = [x for x in hist if str(x.get("type") or "") == "execution_attempt" and bool(x.get("ok", True))]
    realized = round(sum(float(x.get("pnl_usd") or 0.0) for x in trade_closes), 6)
    wins = [x for x in trade_closes if float(x.get("pnl_usd") or 0.0) > 0]
    losses = [x for x in trade_closes if float(x.get("pnl_usd") or 0.0) <= 0]
    avg_win = round(sum(float(x.get("pnl_usd") or 0.0) for x in wins) / len(wins), 6) if wins else 0.0
    avg_loss = round(sum(float(x.get("pnl_usd") or 0.0) for x in losses) / len(losses), 6) if losses else 0.0
    win_rate_pct = round((len(wins) / len(trade_closes)) * 100.0, 4) if trade_closes else 0.0
    expectancy = round(realized / len(trade_closes), 6) if trade_closes else 0.0
    avg_hold = round(sum(float(x.get("hold_time_seconds") or 0.0) for x in trade_closes) / len(trade_closes), 6) if trade_closes else 0.0
    avg_mfe = round(sum(float(x.get("mfe_pct") or 0.0) for x in trade_closes) / len(trade_closes), 6) if trade_closes else 0.0
    avg_mae = round(sum(float(x.get("mae_pct") or 0.0) for x in trade_closes) / len(trade_closes), 6) if trade_closes else 0.0
    by_reason = {}
    for x in trade_closes:
        reason = str(x.get("close_reason") or x.get("tag") or x.get("reason") or "UNKNOWN")
        pnl = float(x.get("pnl_usd") or 0.0)
        roi = float(x.get("roi_pct") or 0.0)
        row = by_reason.setdefault(reason, {"count": 0, "wins": 0, "losses": 0, "realized_pnl_usd": 0.0, "avg_realized_roi_pct_sum": 0.0})
        row["count"] += 1
        row["wins"] += 1 if pnl > 0 else 0
        row["losses"] += 1 if pnl <= 0 else 0
        row["realized_pnl_usd"] = round(row["realized_pnl_usd"] + pnl, 6)
        row["avg_realized_roi_pct_sum"] += roi
    for reason,row in by_reason.items():
        count = max(1, int(row["count"]))
        row["avg_realized_roi_pct"] = round(float(row.pop("avg_realized_roi_pct_sum", 0.0)) / count, 6)

    # Candidate age from signal_accept events
    signal_accepts = [x for x in hist if str(x.get("type") or "") == "signal_accept"]
    raw_ages = [float(x["candidate_age_ms"]) for x in signal_accepts if x.get("candidate_age_ms")]
    avg_candidate_age_ms = round(sum(raw_ages) / len(raw_ages), 1) if raw_ages else 0.0

    # Quoteability exits
    quoteability_exit_count = sum(1 for x in trade_closes if "QUOTEABILITY" in str(x.get("close_reason") or ""))

    # Top-5 close reasons by count (stable sort)
    top_close_reasons = sorted(
        [{"reason": k, "count": v["count"], "pnl_usd": round(v["realized_pnl_usd"], 4),
          "wins": v["wins"], "losses": v["losses"], "avg_roi_pct": v.get("avg_realized_roi_pct", 0.0)}
         for k, v in by_reason.items()],
        key=lambda x: -x["count"]
    )[:5]

    by_mint_events = {}
    for x in hist:
        mint = x.get("mint")
        if not mint:
            continue
        by_mint_events.setdefault(mint, []).append(x)
    current_positions = {p.get("mint"): p for p in list(st.get("paper_positions") or []) if p.get("mint")}
    terminal_positions = {p.get("mint"): p for p in list(st.get("terminal_positions") or []) if p.get("mint")}
    taxonomy = {
        "successful_entries": 0,
        "open_active": 0,
        "open_partial": 0,
        "closed_win": 0,
        "closed_loss": 0,
        "stale_terminalized": 0,
        "orphaned": 0,
        "orphaned_missing_trade_close": 0,
        "orphaned_runtime_pruned_lost_state": 0,
        "orphaned_legacy_schema_artifact": 0,
        "orphaned_duplicate_execution_artifact": 0,
        "orphaned_unknown": 0,
        "cancelled_invalidated": 0,
        "merged_child_row": 0,
        "other": 0,
        "source": "history.json:execution_attempt(ok)+runtime_state",
        "examples": {},
    }
    for e in execution_attempts:
        mint = e.get("mint")
        if not mint:
            taxonomy["other"] += 1
            continue
        taxonomy["successful_entries"] += 1
        pos = current_positions.get(mint)
        terminal = terminal_positions.get(mint)
        events = by_mint_events.get(mint, [])
        closes = [x for x in events if str(x.get("type") or "") == "trade_close"]
        category = "other"
        orphan_cause = None
        if pos and str(pos.get("status") or "") == "closed":
            category = "closed_win" if float((closes[-1] if closes else {}).get("pnl_usd") or pos.get("realized_pnl_usd") or 0.0) > 0 else "closed_loss"
        elif closes:
            category = "closed_win" if float(closes[-1].get("pnl_usd") or 0.0) > 0 else "closed_loss"
        elif terminal:
            category = "stale_terminalized"
        elif pos and str(pos.get("status") or "") == "partial_exit":
            category = "open_partial"
        elif pos and str(pos.get("status") or "") == "open":
            category = "open_active"
        else:
            category = "orphaned"
            exec_count = sum(1 for x in events if str(x.get("type") or "") == "execution_attempt")
            partial_count = sum(1 for x in events if str(x.get("type") or "") == "partial_realize")
            if exec_count > 1:
                orphan_cause = "orphaned_duplicate_execution_artifact"
            elif partial_count > 0:
                orphan_cause = "orphaned_missing_trade_close"
            elif len(events) == 1:
                orphan_cause = "orphaned_runtime_pruned_lost_state"
            elif any(str(x.get("type") or "") == "signal_accept" for x in events):
                orphan_cause = "orphaned_legacy_schema_artifact"
            else:
                orphan_cause = "orphaned_unknown"
            taxonomy[orphan_cause] += 1
        taxonomy[category] += 1
        ex = taxonomy["examples"].setdefault(category, [])
        if len(ex) < 5:
            ex.append(mint)
        if orphan_cause:
            ex2 = taxonomy["examples"].setdefault(orphan_cause, [])
            if len(ex2) < 5:
                ex2.append(mint)
    taxonomy["reconciled_total"] = (
        taxonomy["open_active"]
        + taxonomy["open_partial"]
        + taxonomy["closed_win"]
        + taxonomy["closed_loss"]
        + taxonomy["stale_terminalized"]
        + taxonomy["orphaned"]
        + taxonomy["cancelled_invalidated"]
        + taxonomy["merged_child_row"]
        + taxonomy["other"]
    )
    taxonomy["reconciles_successful_entries"] = taxonomy["successful_entries"] == taxonomy["reconciled_total"]
    _lifetime_result = {
        "realized_pnl_usd": realized,
        "closed_trades": len(trade_closes),
        "successful_entries": len(execution_attempts),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate_pct,
        "expectancy_per_trade_usd": expectancy,
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "avg_hold_time_seconds": avg_hold,
        "avg_mfe_pct": avg_mfe,
        "avg_mae_pct": avg_mae,
        "source": "history.json:trade_close",
        "by_reason": by_reason,
        "taxonomy": taxonomy,
        "avg_candidate_age_ms": avg_candidate_age_ms,
        "quoteability_exit_count": quoteability_exit_count,
        "top_close_reasons": top_close_reasons,
    }
    _rollup_cache["lifetime_key"] = _key
    _rollup_cache["lifetime"] = _lifetime_result
    return _lifetime_result



def _position_debug_row(row: dict, rendered: bool, reason: str = "") -> dict:
    row = dict(row or {})
    return {
        "mint": row.get("mint"),
        "trade_id": row.get("trade_id") or row.get("id") or row.get("entry_ts"),
        "status": row.get("status"),
        "terminal_flag": bool(row.get("terminalized_ts")) or bool(row.get("terminal_flag")),
        "qty_remaining": float(row.get("qty_remaining") or 0.0),
        "remaining_basis_usd": float(row.get("remaining_basis_usd") or 0.0),
        "entry_price": float(row.get("entry_price") or row.get("entry_px") or 0.0),
        "last_mark": float(row.get("current_px") or row.get("last_price") or 0.0),
        "unrealized_pnl_usd": float(row.get("unrealized_pnl_usd") or 0.0),
        "rendered_in_active_positions": bool(rendered),
        "excluded_reason": reason,
        "mark_stale": bool(row.get("mark_stale")),
        "mark_staleness_seconds": float(row.get("mark_staleness_seconds") or 0.0),
    }



def _session_history_rollup(session_start_ts: int) -> dict:
    _key = (_history_cache_ts, int(session_start_ts or 0))
    _cached = _rollup_cache["session"].get(_key)
    if _cached is not None:
        return _cached
    hist = _read_history()
    closes = [x for x in hist if str(x.get("type") or "") == "trade_close" and int(x.get("ts") or 0) >= int(session_start_ts or 0)]
    realized = round(sum(float(x.get("pnl_usd") or 0.0) for x in closes), 6)
    by_reason = {}
    for x in closes:
        reason = str(x.get("close_reason") or x.get("tag") or x.get("reason") or "UNKNOWN")
        row = by_reason.setdefault(reason, {"count": 0, "wins": 0, "losses": 0, "realized_pnl_usd": 0.0, "avg_realized_roi_pct_sum": 0.0})
        pnl = float(x.get("pnl_usd") or 0.0)
        roi = float(x.get("roi_pct") or 0.0)
        row["count"] += 1
        row["wins"] += 1 if pnl > 0 else 0
        row["losses"] += 1 if pnl <= 0 else 0
        row["realized_pnl_usd"] = round(row["realized_pnl_usd"] + pnl, 6)
        row["avg_realized_roi_pct_sum"] += roi
    for _, row in by_reason.items():
        count = max(1, int(row["count"]))
        row["avg_realized_roi_pct"] = round(float(row.pop("avg_realized_roi_pct_sum", 0.0)) / count, 6)
    wins = [x for x in closes if float(x.get("pnl_usd") or 0.0) > 0]
    losses = [x for x in closes if float(x.get("pnl_usd") or 0.0) <= 0]
    avg_win = round(sum(float(x.get("pnl_usd") or 0.0) for x in wins) / len(wins), 6) if wins else 0.0
    avg_loss = round(sum(float(x.get("pnl_usd") or 0.0) for x in losses) / len(losses), 6) if losses else 0.0
    quoteability_exit_count = sum(1 for x in closes if "QUOTEABILITY" in str(x.get("close_reason") or ""))
    session_signal_accepts = [x for x in hist if str(x.get("type") or "") == "signal_accept"
                              and int(x.get("ts") or 0) >= int(session_start_ts or 0)]
    raw_ages = [float(x["candidate_age_ms"]) for x in session_signal_accepts if x.get("candidate_age_ms")]
    avg_candidate_age_ms = round(sum(raw_ages) / len(raw_ages), 1) if raw_ages else 0.0
    top_close_reasons = sorted(
        [{"reason": k, "count": v["count"], "pnl_usd": round(v["realized_pnl_usd"], 4),
          "wins": v["wins"], "losses": v["losses"], "avg_roi_pct": v.get("avg_realized_roi_pct", 0.0)}
         for k, v in by_reason.items()],
        key=lambda x: -x["count"]
    )[:5]
    _session_result = {
        "realized_pnl_usd": realized,
        "closed_trades": len(closes),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round((len(wins) / len(closes)) * 100.0, 4) if closes else 0.0,
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "avg_hold_time_seconds": round(sum(float(x.get("hold_time_seconds") or 0.0) for x in closes) / len(closes), 6) if closes else 0.0,
        "avg_mfe_pct": round(sum(float(x.get("mfe_pct") or 0.0) for x in closes) / len(closes), 6) if closes else 0.0,
        "avg_mae_pct": round(sum(float(x.get("mae_pct") or 0.0) for x in closes) / len(closes), 6) if closes else 0.0,
        "expectancy_per_trade_usd": round(realized / len(closes), 6) if closes else 0.0,
        "quoteability_exit_count": quoteability_exit_count,
        "avg_candidate_age_ms": avg_candidate_age_ms,
        "top_close_reasons": top_close_reasons,
        "by_reason": by_reason,
        "source": "history.json:trade_close:session",
    }
    # Bound the session cache to avoid stale accumulation across many session resets
    if len(_rollup_cache["session"]) > 10:
        _rollup_cache["session"].clear()
    _rollup_cache["session"][_key] = _session_result
    return _session_result



def _canonical_session_view(st: dict) -> dict:
    st = dict(st or {})
    phase = dict(st.get("analytics_phase5c") or {})
    session = dict(phase.get("session") or {})
    lifetime = dict(phase.get("lifetime") or {})
    current_open = dict(phase.get("current_open") or {})
    daily = dict(st.get("daily_metrics") or {})
    all_time = dict(st.get("all_time_metrics") or {})
    session_start_ts = int(daily.get("session_start_ts") or st.get("process_start_ts") or 0)
    session_start_budget = float(st.get("session_start_budget_usd") or 100.0)
    raw_rows = []
    seen_rows = set()
    for source_name in ("active_positions", "paper_positions"):
        for raw in list(st.get(source_name) or []):
            row = dict(raw or {})
            key = (
                str(row.get("mint") or ""),
                int(row.get("entry_ts") or 0),
                str(row.get("reason") or ""),
            )
            if key in seen_rows:
                continue
            seen_rows.add(key)
            raw_rows.append(row)
    paper_positions = [_annotate_position(row) for row in raw_rows]

    canonical_positions = []
    lifetime_open_positions = []
    stale_excluded = 0
    anomaly_open_count = 0
    session_debug = []
    lifetime_debug = []
    position_taxonomy_counts = {
        "ACTIVE": 0,
        "STALE_OPEN": 0,
        "ORPHANED": 0,
        "TERMINALIZED": 0,
    }
    position_taxonomy_examples = {
        "ACTIVE": [],
        "STALE_OPEN": [],
        "ORPHANED": [],
        "TERMINALIZED": [],
    }
    for row in paper_positions:
        bucket, bucket_reason = _position_taxonomy_bucket(row)
        row["position_taxonomy_bucket"] = bucket
        row["position_taxonomy_reason"] = bucket_reason
        if bucket == "ACTIVE":
            position_taxonomy_counts["ACTIVE"] += 1
            if len(position_taxonomy_examples["ACTIVE"]) < 5:
                position_taxonomy_examples["ACTIVE"].append(str(row.get("mint") or ""))
        elif bucket == "STALE_OPEN":
            position_taxonomy_counts["STALE_OPEN"] += 1
            if len(position_taxonomy_examples["STALE_OPEN"]) < 5:
                position_taxonomy_examples["STALE_OPEN"].append(str(row.get("mint") or ""))
        elif bucket == "ORPHANED":
            position_taxonomy_counts["ORPHANED"] += 1
            if len(position_taxonomy_examples["ORPHANED"]) < 5:
                position_taxonomy_examples["ORPHANED"].append(str(row.get("mint") or ""))
        elif bucket == "TERMINALIZED":
            position_taxonomy_counts["TERMINALIZED"] += 1
            if len(position_taxonomy_examples["TERMINALIZED"]) < 5:
                position_taxonomy_examples["TERMINALIZED"].append(str(row.get("mint") or ""))

        status = str(row.get("status") or "")
        if status == "closed":
            continue

        lifetime_reason = ""
        if float(row.get("qty_remaining") or 0.0) <= 1e-9:
            lifetime_reason = "zero_qty_remaining"
        elif float(row.get("remaining_basis_usd") or 0.0) <= 1e-9 and float(row.get("market_value_usd") or 0.0) <= 1e-9:
            lifetime_reason = "zero_basis_and_value"
        else:
            lifetime_open_positions.append(row)
        lifetime_debug.append(_position_debug_row(row, rendered=False, reason=lifetime_reason))

        session_reason = ""
        if int(row.get("entry_ts") or 0) < session_start_ts:
            # Cross-session position: entered before current session start.
            # Include it in active positions so it can be monitored/exited,
            # but flag it so the HUD can show a "RESTORED" badge.
            row["cross_session"] = True
            if float(row.get("qty_remaining") or 0.0) <= 1e-9:
                session_reason = "zero_qty_remaining"
            elif float(row.get("remaining_basis_usd") or 0.0) <= 1e-9 and float(row.get("market_value_usd") or 0.0) <= 1e-9:
                session_reason = "zero_basis_and_value"
            else:
                if bool(row.get("has_anomaly")):
                    anomaly_open_count += 1
                canonical_positions.append(row)
        elif float(row.get("qty_remaining") or 0.0) <= 1e-9:
            session_reason = "zero_qty_remaining"
        elif float(row.get("remaining_basis_usd") or 0.0) <= 1e-9 and float(row.get("market_value_usd") or 0.0) <= 1e-9:
            session_reason = "zero_basis_and_value"
        else:
            if bool(row.get("has_anomaly")):
                anomaly_open_count += 1
            canonical_positions.append(row)
        session_debug.append(_position_debug_row(row, rendered=(session_reason == ""), reason=session_reason))

    history_rollup = _history_lifetime_rollup(st)
    session_history_rollup = _session_history_rollup(session_start_ts)
    latency_rollup = _history_latency_rollup()
    closed_positions = [row for row in paper_positions if str(row.get("status") or "") == "closed"]
    canonical_unrealized = round(sum(float(r.get("unrealized_pnl_usd", 0.0) or 0.0) for r in canonical_positions), 6)
    runtime_session_realized = float(session.get("realized_pnl_usd") or 0.0)
    session_realized = float(session_history_rollup.get("realized_pnl_usd") or 0.0)
    headline_pnl = round(session_realized + canonical_unrealized, 6)
    headline_budget = round(session_start_budget + headline_pnl, 6)
    headline_roi = (headline_pnl / session_start_budget) * 100.0 if session_start_budget > 0 else 0.0

    session["realized_pnl_usd"] = session_realized
    session["unrealized_pnl_usd"] = canonical_unrealized
    session["total_pnl_usd"] = headline_pnl
    session["closed_trades"] = int(session_history_rollup.get("closed_trades") or 0)
    session["avg_hold_time_seconds"] = float(session_history_rollup.get("avg_hold_time_seconds") or 0.0)
    session["avg_mfe_pct"] = float(session_history_rollup.get("avg_mfe_pct") or 0.0)
    session["avg_mae_pct"] = float(session_history_rollup.get("avg_mae_pct") or 0.0)
    session["expectancy"] = {
        "per_trade_usd": float(session_history_rollup.get("expectancy_per_trade_usd") or 0.0),
        "win_rate_pct": float(session_history_rollup.get("win_rate_pct") or 0.0),
        "avg_win_usd": float(session_history_rollup.get("avg_win_usd") or 0.0),
        "avg_loss_usd": float(session_history_rollup.get("avg_loss_usd") or 0.0),
        "source": "history.json:trade_close:session",
    }
    session["wins"] = int(session_history_rollup.get("wins") or 0)
    session["losses"] = int(session_history_rollup.get("losses") or 0)
    session["win_rate_pct"] = float(session_history_rollup.get("win_rate_pct") or 0.0)
    session["quoteability_exit_count"] = int(session_history_rollup.get("quoteability_exit_count") or 0)
    session["avg_candidate_age_ms"] = float(session_history_rollup.get("avg_candidate_age_ms") or 0.0)
    session["top_close_reasons"] = list(session_history_rollup.get("top_close_reasons") or [])
    session["win_loss_by_close_reason"] = dict(session_history_rollup.get("by_reason") or {})
    session["open_positions"] = len(canonical_positions)
    session["source"] = "history_trade_close_plus_canonical_unrealized"

    closed_ledger_realized = round(sum(float(r.get("realized_pnl_usd", 0.0) or 0.0) for r in closed_positions), 6)
    lifetime_closed_count = int(history_rollup.get("closed_trades") or max(len(closed_positions), int(all_time.get("closed_wins", 0) or 0) + int(all_time.get("closed_losses", 0) or 0)))
    lifetime_realized = float(history_rollup.get("realized_pnl_usd") or 0.0)
    lifetime_unrealized = round(sum(float(r.get("unrealized_pnl_usd", 0.0) or 0.0) for r in lifetime_open_positions), 6)
    lifetime["realized_pnl_usd"] = lifetime_realized
    lifetime["unrealized_pnl_usd"] = lifetime_unrealized
    lifetime["total_pnl_usd"] = round(lifetime_realized + lifetime_unrealized, 6)
    lifetime["closed_trades"] = lifetime_closed_count
    lifetime["open_positions"] = len(lifetime_open_positions)
    lifetime["avg_hold_time_seconds"] = float(history_rollup.get("avg_hold_time_seconds") or 0.0)
    lifetime["avg_mfe_pct"] = float(history_rollup.get("avg_mfe_pct") or 0.0)
    lifetime["avg_mae_pct"] = float(history_rollup.get("avg_mae_pct") or 0.0)
    lifetime["expectancy"] = {
        "per_trade_usd": float(history_rollup.get("expectancy_per_trade_usd") or 0.0),
        "win_rate_pct": float(history_rollup.get("win_rate_pct") or 0.0),
        "avg_win_usd": float(history_rollup.get("avg_win_usd") or 0.0),
        "avg_loss_usd": float(history_rollup.get("avg_loss_usd") or 0.0),
    }
    lifetime["win_loss_by_close_reason"] = dict(history_rollup.get("by_reason") or {})
    lifetime["successful_entries"] = int(history_rollup.get("successful_entries") or 0)
    lifetime["wins"] = int(history_rollup.get("wins") or 0)
    lifetime["losses"] = int(history_rollup.get("losses") or 0)
    lifetime["source"] = str(history_rollup.get("source") or "history_rollup")

    current_open.update({
        "count": len(canonical_positions),
        "unrealized_pnl_usd": canonical_unrealized,
        "stale_excluded_count": stale_excluded,
        "anomaly_count": anomaly_open_count,
    })
    phase["session"] = session
    phase["lifetime"] = lifetime
    phase["current_open"] = current_open
    st["analytics_phase5c"] = phase
    st["session_start_budget_usd"] = round(session_start_budget, 6)
    historical_positions = [row for row in paper_positions if str(row.get("status") or "") == "closed" or str(row.get("position_taxonomy_bucket") or "") in {"ORPHANED", "TERMINALIZED"}]
    st["paper_positions"] = historical_positions
    st["active_positions"] = canonical_positions
    st["closed_positions"] = closed_positions
    st["closed_positions_count"] = len(closed_positions)
    st["lifetime_taxonomy"] = dict(history_rollup.get("taxonomy") or {})
    session_contaminated_mints = []
    history_mint_set = set()
    hist = _read_history()
    for x in hist:
        if str(x.get("type") or "") == "trade_close" and int(x.get("ts") or 0) >= int(session_start_ts or 0):
            history_mint_set.add(str(x.get("mint") or ""))
    for row in closed_positions:
        mint = str(row.get("mint") or "")
        if mint and float(row.get("realized_pnl_usd") or 0.0) != 0.0 and mint not in history_mint_set:
            session_contaminated_mints.append(mint)
    zero_qty_missing_close = [
        str(row.get("mint") or "")
        for row in paper_positions
        if float(row.get("qty_remaining") or 0.0) <= 1e-9 and not bool(row.get("canonical_close_emitted") or row.get("close_event_emitted_ts") or str(row.get("status") or "") == "closed")
    ]
    telemetry_events = list(st.get("telemetry_events") or [])
    restored_tp_replay_blocked = [
        {"mint": str(ev.get("mint") or ""), "detail": str(ev.get("detail") or ""), "ts": _safe_int(ev.get("ts"))}
        for ev in telemetry_events[-1000:]
        if str(ev.get("type") or "") == "RESTORED_TP_REPLAY_BLOCKED" and str(ev.get("mint") or "")
    ][-20:]
    reentry_blocked_missing_close = [
        {"mint": str(ev.get("mint") or ""), "detail": str(ev.get("detail") or ""), "ts": _safe_int(ev.get("ts"))}
        for ev in telemetry_events[-1000:]
        if str(ev.get("type") or "") == "REENTRY_BLOCKED_MISSING_CANONICAL_CLOSE" and str(ev.get("mint") or "")
    ][-20:]

    runtime_canonical_capital = round(float(st.get("canonical_capital_usd") or headline_budget), 6)
    runtime_session_window_budget = round(float(st.get("session_window_budget_usd") or headline_budget), 6)

    headline_reconciliation = {
        "trusted_headline_pnl_usd": round(headline_pnl, 6),
        "trusted_headline_budget_usd": round(headline_budget, 6),
        "canonical_capital_usd": runtime_canonical_capital,
        "session_window_budget_usd": runtime_session_window_budget,
        "headline_source": "history_trade_close_plus_canonical_unrealized",
        "canonical_session_realized_trade_close_usd": round(session_realized, 6),
        "runtime_internal_session_realized_usd": round(runtime_session_realized, 6),
        "current_session_unrealized_usd": round(canonical_unrealized, 6),
        "contamination_gap_usd": round(runtime_session_realized - session_realized, 6),
        "session_contaminated_mint_count": len(session_contaminated_mints),
        "session_contaminated_mints": session_contaminated_mints,
        "trust_explanation": "Headline uses canonical trade_close ledger realized plus canonical current-session unrealized; runtime session realized is shown separately for contamination visibility.",
    }
    operator_anomalies = {
        "restored_tp_replay_blocked": restored_tp_replay_blocked,
        "restored_tp_replay_blocked_count": len(restored_tp_replay_blocked),
        "zero_qty_without_canonical_close_mints": zero_qty_missing_close,
        "zero_qty_without_canonical_close_count": len(zero_qty_missing_close),
        "reentry_blocked_missing_canonical_close": reentry_blocked_missing_close,
        "reentry_blocked_missing_canonical_close_count": len(reentry_blocked_missing_close),
        "session_contaminated_mints": session_contaminated_mints,
        "session_contaminated_mint_count": len(session_contaminated_mints),
    }
    taxonomy_visibility = {
        "ACTIVE": position_taxonomy_counts["ACTIVE"],
        "STALE_OPEN": position_taxonomy_counts["STALE_OPEN"],
        "ORPHANED": position_taxonomy_counts["ORPHANED"],
        "TERMINALIZED": position_taxonomy_counts["TERMINALIZED"],
        "examples": position_taxonomy_examples,
        "explanations": {
            "ACTIVE": "open wallet-backed managed positions; remain actively managed even when mark is stale",
            "STALE_OPEN": "reserved bucket for future temporary data-quality handling; should normally remain zero",
            "ORPHANED": "history/runtime artifacts that do not represent a clean active lifecycle",
            "TERMINALIZED": "closed or explicitly terminalized rows kept only for audit visibility",
        },
    }
    st["debug_reconciliation"] = {
        "session_start_ts": session_start_ts,
        "session_unrealized_contributors": session_debug,
        "lifetime_unrealized_contributors": lifetime_debug,
        "session_unrealized_sum": canonical_unrealized,
        "lifetime_unrealized_sum": lifetime_unrealized,
        "visible_active_positions_sum": round(sum(float(r.get("unrealized_pnl_usd", 0.0) or 0.0) for r in canonical_positions), 6),
        "closed_positions_realized_sum": closed_ledger_realized,
        "history_lifetime_realized_sum": lifetime_realized,
        "history_session_realized_sum": session_realized,
        "runtime_session_realized_sum": runtime_session_realized,
        "session_realized_gap_vs_history": round(runtime_session_realized - session_realized, 6),
        "session_contaminated_mints": session_contaminated_mints,
        "zero_qty_missing_close_mints": zero_qty_missing_close,
        "history_lifetime_closed_count": lifetime_closed_count,
        "history_lifetime_successful_entries": int(history_rollup.get("successful_entries") or 0),
        "history_lifetime_wins": int(history_rollup.get("wins") or 0),
        "history_lifetime_losses": int(history_rollup.get("losses") or 0),
        "history_lifetime_win_rate_pct": float(history_rollup.get("win_rate_pct") or 0.0),
        "history_lifetime_expectancy_per_trade_usd": float(history_rollup.get("expectancy_per_trade_usd") or 0.0),
        "history_lifetime_avg_hold_time_seconds": float(history_rollup.get("avg_hold_time_seconds") or 0.0),
        "history_lifetime_avg_mfe_pct": float(history_rollup.get("avg_mfe_pct") or 0.0),
        "history_lifetime_avg_mae_pct": float(history_rollup.get("avg_mae_pct") or 0.0),
        "history_lifetime_source": history_rollup.get("source"),
        "history_session_source": session_history_rollup.get("source"),
        "lifetime_taxonomy": dict(history_rollup.get("taxonomy") or {}),
        "headline_reconciliation": headline_reconciliation,
        "operator_anomalies": operator_anomalies,
        "taxonomy_visibility": taxonomy_visibility,
    }
    st["headline_reconciliation"] = headline_reconciliation
    st["operator_anomalies"] = operator_anomalies
    st["position_taxonomy_visibility"] = taxonomy_visibility
    st["session_window_budget_usd"] = runtime_session_window_budget
    st["sim_budget_usd"] = runtime_canonical_capital if st.get("execution_mode") != "live" else None
    st["pnl_usd"] = round(headline_pnl, 6)
    st["roi_pct"] = round(headline_roi, 6)
    st["headline"] = {
        "budget_usd": round(headline_budget, 6),
        "pnl_usd": round(headline_pnl, 6),
        "roi_pct": round(headline_roi, 6),
        "open_positions": len(canonical_positions),
        "source": "history_trade_close_plus_canonical_unrealized",
        "canonical_session_realized_trade_close_usd": round(session_realized, 6),
        "runtime_internal_session_realized_usd": round(runtime_session_realized, 6),
        "current_session_unrealized_usd": round(canonical_unrealized, 6),
        "contamination_gap_usd": round(runtime_session_realized - session_realized, 6),
        "session_contaminated_mint_count": len(session_contaminated_mints),
        "session_contaminated_mints": session_contaminated_mints,
        "trust_explanation": "Canonical headline excludes contaminated runtime realized and uses trade_close ledger realized plus canonical unrealized only.",
    }
    pending_other = max(
        0,
        int(daily.get("total_scans", 0) or 0)
        - int(daily.get("vetoed_liquidity", 0) or 0)
        - int(daily.get("vetoed_price", 0) or 0)
        - int(daily.get("successful_entries", 0) or 0)
        - int(daily.get("parse_failures", 0) or 0),
    )
    st["decision_breakdown"] = {
        "total_scans": int(daily.get("total_scans", 0) or 0),
        "unique_mints": int(daily.get("unique_mints", 0) or 0),
        "duplicates_skipped": int(daily.get("duplicates_skipped", 0) or 0),
        "vetoed_liquidity": int(daily.get("vetoed_liquidity", 0) or 0),
        "vetoed_price": int(daily.get("vetoed_price", 0) or 0),
        "parse_failures": int(daily.get("parse_failures", 0) or 0),
        "successful_entries": int(daily.get("successful_entries", 0) or 0),
        "pending_other": pending_other,
        "qualified_candidates": int(daily.get("qualified_candidates", 0) or 0),
        "raw_candidates": int(daily.get("raw_candidates", 0) or 0),
        "reconciles_total_scans": True,
    }
    daily_metrics = st.get("daily_metrics") or {}
    decision_avg = float(daily_metrics.get("avg_decision_latency_ms") or 0.0)
    decision_count = int(daily_metrics.get("decision_count") or 0)
    mint_parse_successes = max(1, int(daily_metrics.get("mint_parse_successes", 0) or 0))
    rpc_timeouts_1h = int(st.get("rpc_timeouts_1h", 0) or 0)
    process_start_ts = int(st.get("process_start_ts") or 0)
    session_start_ts = int(daily_metrics.get("session_start_ts") or process_start_ts or 0)
    last_cmd = dict(st.get("operator_last_command") or {})
    last_cmd_ts = int(last_cmd.get("ts") or 0)
    reset_cause = "unknown"
    reset_source = "unknown"
    reset_scope = "session_only"
    if last_cmd.get("action") == "reset_session" and last_cmd_ts >= session_start_ts > 0:
        reset_cause = f"operator_reset:{last_cmd.get('mode','unknown')}"
        reset_source = "operator"
    elif session_start_ts and process_start_ts and session_start_ts == process_start_ts:
        reset_cause = "process_restart_or_auto_daily_session_init"
        reset_source = "startup_or_auto_boundary"
    elif session_start_ts:
        reset_cause = "session_boundary_or_metrics_reset"
        reset_source = "auto"
    st["session_reset_info"] = {
        "current_session_start_ts": session_start_ts,
        "current_session_start_iso": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(session_start_ts)) if session_start_ts else None,
        "process_start_ts": process_start_ts,
        "process_start_iso": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(process_start_ts)) if process_start_ts else None,
        "reset_cause": reset_cause,
        "reset_source": reset_source,
        "reset_scope": reset_scope,
        "last_command_ts": last_cmd_ts or None,
        "last_command_is_stale": bool(last_cmd_ts and session_start_ts and last_cmd_ts < session_start_ts),
        "last_command_summary": last_cmd,
    }
    rpc_wait_avg_ms = float(latency_rollup.get("avg_rpc_wait_ms") or 0.0) or round(float(daily_metrics.get("mint_parse_gettx_wait_sum_ms", 0.0) or 0.0) / mint_parse_successes, 6)
    metadata_lookup_avg_ms = float(latency_rollup.get("avg_metadata_lookup_ms") or 0.0)
    price_lookup_avg_ms = float(latency_rollup.get("avg_price_lookup_ms") or 0.0)
    rugcheck_primary_avg_ms = float(latency_rollup.get("avg_rugcheck_primary_ms") or 0.0)
    fallback_avg_ms = float(latency_rollup.get("avg_fallback_ms") or 0.0)
    fast_veto_checks_avg_ms = float(latency_rollup.get("avg_fast_veto_checks_ms") or 0.0)
    serial_gap_avg_ms = float(latency_rollup.get("avg_serial_gap_ms") or 0.0)
    latency_components_ranked = sorted([
        {"name": "decision_total", "avg_ms": round(decision_avg, 6)},
        {"name": "dependency_delay", "avg_ms": round(float(latency_rollup.get("avg_dependency_delay_ms") or 0.0), 6)},
        {"name": "mint_parse", "avg_ms": round(float(daily_metrics.get("mint_parse_latency_sum_ms", 0.0) or 0.0) / mint_parse_successes, 6)},
        {"name": "rpc_wait", "avg_ms": round(rpc_wait_avg_ms, 6)},
        {"name": "decision_finalize", "avg_ms": round(float(latency_rollup.get("avg_decision_finalize_ms") or 0.0), 6)},
        {"name": "price_lookup", "avg_ms": round(price_lookup_avg_ms, 6)},
        {"name": "rugcheck_primary", "avg_ms": round(rugcheck_primary_avg_ms, 6)},
        {"name": "metadata_lookup", "avg_ms": round(metadata_lookup_avg_ms, 6)},
        {"name": "execution_submit", "avg_ms": round(float(latency_rollup.get("avg_execution_latency_ms") or 0.0), 6)},
        {"name": "fast_veto_checks", "avg_ms": round(fast_veto_checks_avg_ms, 6)},
        {"name": "serial_gap", "avg_ms": round(serial_gap_avg_ms, 6)},
        {"name": "fallback", "avg_ms": round(fallback_avg_ms, 6)},
    ], key=lambda x: float(x.get("avg_ms") or 0.0), reverse=True)
    st["latency_breakdown"] = {
        "headline_avg_latency_ms": float(st.get("avg_latency_ms") or 0.0),
        "decision_avg_latency_ms": decision_avg,
        "mint_parse_avg_latency_ms": round(float(daily_metrics.get("mint_parse_latency_sum_ms", 0.0) or 0.0) / mint_parse_successes, 6),
        "get_transaction_avg_wait_ms": round(float(daily_metrics.get("mint_parse_gettx_wait_sum_ms", 0.0) or 0.0) / mint_parse_successes, 6),
        "rpc_wait_avg_ms": round(rpc_wait_avg_ms, 6),
        "metadata_lookup_avg_ms": round(metadata_lookup_avg_ms, 6),
        "price_lookup_avg_ms": round(price_lookup_avg_ms, 6),
        "rugcheck_primary_avg_ms": round(rugcheck_primary_avg_ms, 6),
        "fallback_avg_ms": round(fallback_avg_ms, 6),
        "fast_veto_checks_avg_ms": round(fast_veto_checks_avg_ms, 6),
        "serial_gap_avg_ms": round(serial_gap_avg_ms, 6),
        "mint_parse_retry_avg": round(float(daily_metrics.get("mint_parse_retry_count", 0.0) or 0.0) / mint_parse_successes, 6),
        "mint_parse_cache_hits": int(daily_metrics.get("mint_parse_cache_hits", 0) or 0),
        "execution_avg_latency_ms": float(latency_rollup.get("avg_execution_latency_ms") or 0.0),
        "pre_execution_avg_latency_ms": float(latency_rollup.get("avg_pre_execution_latency_ms") or 0.0),
        "dependency_delay_avg_ms": float(latency_rollup.get("avg_dependency_delay_ms") or 0.0),
        "rug_check_avg_ms": float(latency_rollup.get("avg_rug_check_ms") or 0.0),
        "decision_finalize_avg_ms": float(latency_rollup.get("avg_decision_finalize_ms") or 0.0),
        "history_execution_total_avg_ms": float(latency_rollup.get("avg_total_latency_ms") or 0.0),
        "bottleneck_ranking": latency_components_ranked,
        "decision_minus_execution_gap_ms": round(max(0.0, decision_avg - float(latency_rollup.get("avg_execution_latency_ms") or 0.0)), 6),
        "rpc_timeouts_1h": rpc_timeouts_1h,
        "parse_fail_rpc_timeout_today": int((st.get("daily_metrics") or {}).get("parse_fail_rpc_timeout", 0) or 0),
        "dependency_retry_queue": len(st.get("dependency_retry_queue") or []),
        "decision_count_today": decision_count,
        "avg_detect_to_exec_ms": latency_rollup.get("avg_detect_to_exec_ms"),
        "execution_attempts_history": int(latency_rollup.get("execution_attempts") or 0),
        "p50_total_execution_attempt_latency_ms": float(latency_rollup.get("p50_total_latency_ms") or 0.0),
        "p95_execution_latency_ms": float(latency_rollup.get("p95_execution_latency_ms") or 0.0),
        "p95_total_execution_attempt_latency_ms": float(latency_rollup.get("p95_total_latency_ms") or 0.0),
        "dominant_component": (
            "decision/rpc_wait" if decision_avg >= max(float(latency_rollup.get("avg_execution_latency_ms") or 0.0), float(st.get("avg_latency_ms") or 0.0)) else
            "execution_submit" if float(latency_rollup.get("avg_execution_latency_ms") or 0.0) >= float(st.get("avg_latency_ms") or 0.0) else
            "mixed"
        ),
        "source": {
            "headline": "runtime_state.avg_latency_ms",
            "decision": "daily_metrics.avg_decision_latency_ms",
            "execution": "history.json:execution_attempt.execution_latency_ms",
            "pre_execution": "history.json:execution_attempt.latency_ms - execution_latency_ms",
        },
    }
    current_policy_usd = _latest_runtime_policy_threshold(st)
    st["min_liquidity_usd_active"] = current_policy_usd
    normalized_graveyard = []
    for row in list(st.get("graveyard") or []):
        row = dict(row or {})
        if row.get("historical_threshold_usd") is not None and row.get("current_policy_threshold_usd") is not None:
            row["historical_threshold_context_only"] = bool(row.get("historical_threshold_context_only", False))
            row["historical_threshold_note"] = "first-class persisted threshold-at-event"
        else:
            threshold_ctx = _threshold_context_from_reason(str(row.get("reason") or ""), current_policy_usd)
            row["historical_threshold_usd"] = threshold_ctx["historical_threshold_usd"]
            row["current_policy_threshold_usd"] = threshold_ctx["current_policy_threshold_usd"]
            row["historical_threshold_context_only"] = threshold_ctx["historical_context_only"]
            row["historical_threshold_note"] = threshold_ctx["historical_note"]
        normalized_graveyard.append(row)
    st["graveyard"] = normalized_graveyard

    st["source_of_truth"] = {
        "runtime": os.getenv("HUD_RUNTIME_LABEL", "local-127.0.0.1:8000"),
        "public_path": os.getenv("HUD_PUBLIC_PATH", "local-only"),
        "state_file": str(STATE_FILE),
        "canonical_session_equity": "analytics_phase5c.session + canonical open positions",
    }

    # P0-4 / P3-1 / P4-1: telemetry health + trust badge (runs every enrich pass)
    telemetry_health = _compute_telemetry_health(st, history_rollup, session_history_rollup)
    st["telemetry_health"] = telemetry_health
    st["trust_level"] = telemetry_health["trust_level"]
    st["trust_reasons"] = telemetry_health["trust_reasons"]
    st["hud_last_reconciled_at"] = telemetry_health["hud_last_reconciled_at"]

    return st

def _compute_telemetry_health(st: dict, history_rollup: dict, session_rollup: dict) -> dict:
    """
    P0-4 / P3-1 / P4-1 — Single reconciliation pass that produces:
      - ledger vs runtime trade-count comparison
      - orphan position detection (open in runtime but closed in ledger)
      - funnel staleness check
      - trust_level: "green" | "yellow" | "red"
    Called once per enrich_hud_state pass; result is cheap (history already loaded).
    """
    now_ts = int(time.time())

    # ── Ledger truth (from history.json) ────────────────────────────────────
    # Use LIFETIME rollup for win/rate reporting only.
    ledger_wins = int(history_rollup.get("wins") or 0)
    ledger_losses = int(history_rollup.get("losses") or 0)
    ledger_win_rate = float(history_rollup.get("win_rate_pct") or 0.0)
    ledger_realized = float(history_rollup.get("realized_pnl_usd") or 0.0)

    # ── Session-scoped comparison: ledger vs runtime (same time window) ──────
    # session_rollup is already filtered to session_start_ts — this is the
    # correct ledger-side count to compare against the runtime session counter.
    # Using lifetime ledger vs in-memory all_time (which resets on restart)
    # produces a guaranteed false mismatch after every restart.
    ledger_trade_count = int(session_rollup.get("closed_trades") or 0)

    # Runtime session counter: daily_metrics is persisted in runtime_state.json
    # and scoped to the current day/session — comparable to session_rollup.
    dm = dict(st.get("daily_metrics") or {})
    runtime_wins = int(dm.get("closed_wins") or 0)
    runtime_losses = int(dm.get("closed_losses") or 0)
    runtime_trade_count = runtime_wins + runtime_losses

    # Mismatch: allow a tolerance of ±2 for in-flight closes
    count_delta = abs(ledger_trade_count - runtime_trade_count)
    count_mismatch = count_delta > 2

    # ── Orphan detection: open positions whose mints appear in ledger closes ──
    hist = _read_history()
    closed_mints = {
        str(x.get("mint") or "")
        for x in hist
        if str(x.get("type") or "") == "trade_close" and x.get("mint")
    }
    all_open = (
        [p for p in list(st.get("paper_positions") or []) if str(p.get("status") or "") not in ("closed",)]
        + [p for p in list(st.get("active_positions") or []) if str(p.get("status") or "") not in ("closed",)]
    )
    orphan_mints = [
        str(p.get("mint") or "")
        for p in all_open
        if float(p.get("qty_remaining") or 0) > 1e-9
        and str(p.get("mint") or "") in closed_mints
    ]
    orphan_mints = list(dict.fromkeys(orphan_mints))  # deduplicate, preserve order

    # ── Funnel staleness ─────────────────────────────────────────────────────
    funnel = dict(st.get("entry_funnel") or {})
    funnel_attempted = int(funnel.get("execution_attempted") or 0)
    funnel_session_start_ts = int(funnel.get("funnel_session_start_ts") or 0)
    process_start_ts = int(st.get("process_start_ts") or 0)
    runtime_age_s = max(0, now_ts - (funnel_session_start_ts or process_start_ts)) if (funnel_session_start_ts or process_start_ts) else 0
    funnel_stale = runtime_age_s > 600 and funnel_attempted == 0  # 10+ min running, zero attempts

    # ── Integrity violations ─────────────────────────────────────────────────
    hist_integrity_errors = sum(
        1 for x in hist
        if str(x.get("type") or "") == "trade_close" and bool(x.get("close_integrity_error"))
    )

    # ── Price suspect share (recent 50 execution_attempts) ───────────────────
    # Degrade trust to YELLOW when most entries still use the graduation curve
    # sentinel price (no fill_price_usd). > 50% suspect = data is not trustworthy.
    recent_execs = [
        x for x in hist
        if str(x.get("type") or "") == "execution_attempt" and x.get("ok")
    ][-50:]
    suspect_count = sum(1 for x in recent_execs if not x.get("fill_price_usd"))
    total_execs = len(recent_execs)
    suspect_share_pct = round(suspect_count / total_execs * 100) if total_execs > 0 else 0
    price_data_suspect = total_execs >= 5 and suspect_share_pct > 50

    # ── Trust level ──────────────────────────────────────────────────────────
    trust_reasons: list[str] = []
    if count_mismatch:
        trust_reasons.append(f"history_runtime_mismatch:ledger={ledger_trade_count}_runtime={runtime_trade_count}_delta={count_delta}")
    if orphan_mints:
        trust_reasons.append(f"orphan_positions:{len(orphan_mints)}")
    if hist_integrity_errors:
        trust_reasons.append(f"close_integrity_errors:{hist_integrity_errors}")
    if funnel_stale:
        trust_reasons.append("funnel_stale:no_execution_attempts_10min")
    if price_data_suspect:
        trust_reasons.append(f"price_suspect_dominant:{suspect_share_pct}%_of_{total_execs}_execs_lack_fill_price")

    if count_mismatch or orphan_mints or hist_integrity_errors:
        trust_level = "red"
    elif funnel_stale or price_data_suspect:
        trust_level = "yellow"
    else:
        trust_level = "green"

    return {
        "hud_last_reconciled_at": now_ts,
        "ledger_trade_count": ledger_trade_count,
        "ledger_wins": ledger_wins,
        "ledger_losses": ledger_losses,
        "ledger_win_rate_pct": ledger_win_rate,
        "ledger_realized_pnl_usd": ledger_realized,
        "runtime_trade_count": runtime_trade_count,
        "history_runtime_mismatch": count_mismatch,
        "history_runtime_delta": count_delta,
        "orphan_position_count": len(orphan_mints),
        "orphan_position_mints": orphan_mints[:5],
        "funnel_stale": funnel_stale,
        "close_integrity_errors": hist_integrity_errors,
        "trust_level": trust_level,
        "trust_reasons": trust_reasons,
        "source": "history.json+runtime_state reconciliation",
    }


def compute_health_score(st: dict) -> dict:
    dm = st.get("daily_metrics") or {}
    scans = int(dm.get("total_scans", 0) or 0)
    raw_candidates = int(dm.get("raw_candidates", 0) or 0)
    qualified_candidates = int(dm.get("qualified_candidates", 0) or 0)
    parse_fail = int(dm.get("parse_failures", 0) or 0)
    rpc429 = int(dm.get("rpc_429_count", 0) or 0)
    ws_disc = int(dm.get("ws_disconnects", 0) or 0)
    ws_disc_1h = int(st.get("ws_disconnects_1h", 0) or 0)
    decision_count = int(dm.get("decision_count", 0) or 0)
    jito_rate = float(st.get("jito_success_rate", 100.0) or 100.0)

    score = 100.0
    parse_fail_rate = min(100.0, (parse_fail / max(1, scans)) * 100.0) / 100.0
    rpc429_rate = min(100.0, (rpc429 / max(1, scans)) * 100.0) / 100.0
    decision_coverage = min(100.0, (decision_count / max(1, scans)) * 100.0) / 100.0
    qualified_vs_raw_ratio = min(100.0, (qualified_candidates / max(1, raw_candidates)) * 100.0) / 100.0

    score -= min(35.0, parse_fail_rate * 50.0)
    score -= min(20.0, rpc429_rate * 40.0)
    score -= min(15.0, ws_disc * 0.8)
    score -= min(8.0, ws_disc_1h * 1.0)
    if jito_rate < 95.0:
        score -= min(20.0, (95.0 - jito_rate) * 0.7)
    # Penalize blind ingestion (lots of scans, little classification progress)
    if scans >= 100 and decision_coverage < 0.05:
        score -= 10.0
    if raw_candidates >= 50 and qualified_vs_raw_ratio < 0.10:
        score -= 8.0

    score = max(0.0, min(100.0, score))
    if score >= 85:
        tier = "ELITE"
    elif score >= 70:
        tier = "GOOD"
    elif score >= 50:
        tier = "DEGRADED"
    else:
        tier = "CRITICAL"

    return {
        "score": round(score, 1),
        "tier": tier,
        "factors": {
            "parse_fail_rate": round(parse_fail_rate * 100.0, 2),
            "rpc429_rate": round(rpc429_rate * 100.0, 2),
            "ws_disconnects": ws_disc,
            "ws_disconnects_1h": ws_disc_1h,
            "qualified_vs_raw_ratio": round(qualified_vs_raw_ratio * 100.0, 2),
            "parse_fail_rpc_429": int(dm.get("parse_fail_rpc_429", 0) or 0),
            "parse_fail_no_balances": int(dm.get("parse_fail_no_balances", 0) or 0),
            "parse_fail_excluded_mint": int(dm.get("parse_fail_excluded_mint", 0) or 0),
            "jito_success_rate": round(jito_rate, 2),
            "decision_coverage": round(decision_coverage * 100.0, 2),
        },
    }


def enrich_execution_fields(st: dict) -> dict:
    st = dict(st or {})
    ex = st.get("last_execution") or {}

    env_exec_mode = os.getenv("EXECUTION_MODE", "").strip().lower()
    env_cu_price_str = os.getenv("CU_PRICE_MIN", "").strip()
    env_jito_tip_str = os.getenv("JITO_TIP_LAMPORTS", "").strip()

    # Trust runtime state first: SimEngine writes the actual (possibly downgraded) mode at startup.
    # Env var is displayed separately as execution_mode_env for transparency.
    st["execution_mode"] = st.get("execution_mode") or env_exec_mode or "paper"
    st["execution_mode_env"] = env_exec_mode or "paper"

    # cu_price: prefer last live execution CU price (most truthful); fall back to env; then default.
    # Do NOT let env always win — the runtime may choose a different CU dynamically.
    runtime_cu = ex.get("cu_price") or st.get("cu_price")
    if runtime_cu:
        st["cu_price"] = int(runtime_cu)
    elif env_cu_price_str.isdigit():
        st["cu_price"] = int(env_cu_price_str)
    else:
        st["cu_price"] = 10000

    st["cu_limit"] = st.get("cu_limit", ex.get("cu_limit"))

    # jito_tip: prefer live canary last-computed tip; fall back to env; then default.
    # This surfaces the actual tip used in the last execution, not the static env config.
    canary_tip = None
    try:
        if _live_canary is not None and getattr(_live_canary, "_last_jito_tip_ts", 0) > 0:
            canary_tip = _live_canary._last_jito_tip_lamports
            st["jito_tip_mode"] = _live_canary._last_jito_tip_mode
    except Exception:
        pass
    runtime_jito = canary_tip or ex.get("jito_tip_lamports") or st.get("jito_tip")
    if runtime_jito:
        st["jito_tip"] = int(runtime_jito)
    elif env_jito_tip_str.isdigit():
        st["jito_tip"] = int(env_jito_tip_str)
    else:
        st["jito_tip"] = 20000

    st["priority_tier"] = st.get("priority_tier", ex.get("priority_tier"))
    st["cost_to_trade"] = st.get("cost_to_trade", ex.get("cost_to_trade"))
    st["live_trade_attempts"] = int(st.get("live_trade_attempts", 0) or 0)

    # Normalize operator_controls stop/failsafe to current env values.
    # State file may contain stale values from a prior session with different defaults.
    # The env is always authoritative for risk parameters.
    controls = st.get("operator_controls") or {}
    controls["default_downside_stop_pct"] = float(os.getenv("DEFAULT_DOWNSIDE_STOP_PCT", "0.12"))
    controls["absolute_failsafe_stop_pct"] = float(os.getenv("ABSOLUTE_FAILSAFE_STOP_PCT", "0.20"))
    st["operator_controls"] = controls

    return st


def hydrate_hud_derived_from_state() -> None:
    global HUD_HEALTH_HISTORY, HUD_JITO_BUNDLE_TOTAL, HUD_JITO_BUNDLE_SUCCESS
    st = read_state()
    hh = st.get("health_history") or []
    HUD_HEALTH_HISTORY = list(hh[-360:]) if isinstance(hh, list) else []
    HUD_JITO_BUNDLE_TOTAL = int(st.get("jito_bundle_total", 0) or 0)
    HUD_JITO_BUNDLE_SUCCESS = int(st.get("jito_bundle_success", 0) or 0)


def _hhmmss(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds or 0))
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def enrich_hud_state(st: dict) -> dict:
    # Pin history snapshot for this call so _read_history() never re-reads mid-call
    # even if the 10s TTL expires while we're computing (function can take 0.5-2s).
    _history_local.pinned = _read_history()
    try:
        return _enrich_hud_state_inner(st)
    finally:
        _history_local.pinned = None


def _enrich_hud_state_inner(st: dict) -> dict:
    st = enrich_execution_fields(st)
    now_ts = int(time.time())
    raw_updated_at = int(st.get("updated_at", 0) or 0)
    start_ts = int(st.get("process_start_ts", 0) or (st.get("daily_metrics") or {}).get("session_start_ts", 0) or 0)
    uptime_seconds = max(0, now_ts - start_ts) if start_ts else int(st.get("process_uptime_seconds", 0) or 0)

    st["state_updated_at"] = raw_updated_at
    st["state_updated_at_iso"] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(raw_updated_at)) if raw_updated_at else None
    st["state_age_seconds"] = max(0, now_ts - raw_updated_at) if raw_updated_at else None
    st["updated_at"] = now_ts
    st["updated_at_iso"] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(now_ts))
    st["ts"] = now_ts
    st["process_uptime_seconds"] = uptime_seconds
    st["process_uptime_human"] = _hhmmss(uptime_seconds)
    # paper_mode reflects actual runtime execution_mode, not just wallet env var presence
    st["paper_mode"] = (st.get("execution_mode", "paper") == "paper")
    # real_wallet_sol: use balance fetched by broadcaster_loop (cross-process safe RPC fetch)
    st["real_wallet_sol"] = _last_balance_sol
    # real_wallet_usd: SOL × cached price; None if either is unavailable
    if _last_balance_sol is not None and _last_sol_usd is not None:
        st["real_wallet_usd"] = round(_last_balance_sol * _last_sol_usd, 2)
    else:
        st["real_wallet_usd"] = None

    # Add live canary data if available — use module-level _live_canary ref (not per-call import)
    try:
        canary_data = _live_canary.get_canary_hud_data() if _live_canary is not None else None
        if canary_data is not None:
            st["live_canary"] = canary_data
            st["canary_mode"] = canary_data.get("canary_mode", False)
            # trading_enabled: prefer operator_controls.trading_enabled written by sim engine via queue
            # (cross-process safe); fall back to canary module value (same-process informational only)
            controls = st.get("operator_controls") or {}
            if "trading_enabled" in controls:
                st["trading_enabled"] = bool(controls["trading_enabled"])
            else:
                st["trading_enabled"] = canary_data.get("trading_enabled", False)
            # Wallet balance: prefer broadcaster_loop fetch; only fall back if still None
            if _last_balance_sol is None and canary_data.get("wallet_pubkey"):
                st["real_wallet_sol"] = canary_data.get("wallet_balance_sol") or None
        else:
            st.setdefault("live_canary", None)
            st.setdefault("canary_mode", False)
            st.setdefault("trading_enabled", False)
    except Exception:
        st.setdefault("live_canary", None)
        st.setdefault("canary_mode", False)
        st.setdefault("trading_enabled", False)
    
    st = _canonical_session_view(st)
    st["jito_bundle_total"] = int(HUD_JITO_BUNDLE_TOTAL or 0)
    st["jito_bundle_success"] = int(HUD_JITO_BUNDLE_SUCCESS or 0)
    total_probe = int(st.get("jito_bundle_total", 0) or 0)
    succ_probe = int(st.get("jito_bundle_success", 0) or 0)
    st["jito_success_rate"] = (succ_probe / total_probe) * 100.0 if total_probe > 0 else 100.0
    st["health_score"] = compute_health_score(st)
    st["health_history"] = list(HUD_HEALTH_HISTORY)
    st["recent_activity"] = list(st.get("recent_activity") or [])[-80:]
    st["telemetry_events"] = list(st.get("telemetry_events") or [])[-120:]
    st["graveyard"] = list(st.get("graveyard") or [])[-50:]
    st["health_history"] = list(st.get("health_history") or [])[-120:]
    st["provider_health"] = {
        "ws": {
            "disconnects_1h": int(st.get("ws_disconnects_1h", 0) or 0),
            "uptime_ratio": float(st.get("ws_uptime_ratio", 0.0) or 0.0),
            "last_message_ts": st.get("last_ws_message_ts"),
            "last_heartbeat_ts": st.get("last_ws_heartbeat_ts"),
            "last_successful_subscribe_ts": st.get("last_successful_subscribe_ts"),
            "handshake_timeouts_1h": int(st.get("ws_handshake_timeouts_1h", 0) or 0),
            "redundancy_ok": bool(st.get("ws_redundancy_ok", False)),
            "derived_status": ("active_messages_seen" if int(st.get("last_ws_message_ts") or 0) >= now_ts - 30 else "idle_or_stale"),
            "uptime_note": ("Metric is producer-supplied; can stay 0.0 when messages are flowing if the upstream ratio field is not being updated." if float(st.get("ws_uptime_ratio", 0.0) or 0.0) == 0.0 and int(st.get("last_ws_message_ts") or 0) >= now_ts - 30 else "reported"),
        },
        "rpc": {
            "success_rate": float(st.get("rpc_success_rate", 0.0) or 0.0),
            "timeouts_1h": int(st.get("rpc_timeouts_1h", 0) or 0),
            "endpoint_count": int(st.get("rpc_endpoint_count", 0) or 0),
            "rpc_429_count_today": int((st.get("daily_metrics") or {}).get("rpc_429_count", 0) or 0),
        },
        "dependencies": {
            "retry_queue": len(st.get("dependency_retry_queue") or []),
            "event_workers": int(st.get("event_workers", 0) or 0),
            "event_workers_active": int(st.get("event_workers_active", 0) or 0),
            "event_queue_max": int(st.get("event_queue_max", 0) or 0),
        },
    }
    st["hud_derived"] = {
        "paper_mode": st["paper_mode"],
        "real_wallet_sol": st["real_wallet_sol"],
        "real_wallet_usd": st.get("real_wallet_usd"),
        "trading_enabled": st.get("trading_enabled"),
        "execution_mode_env": st.get("execution_mode_env"),
        "pending_execution_mode": st.get("pending_execution_mode"),
        "sim_budget_usd": st["sim_budget_usd"],
        "jito_bundle_total": st["jito_bundle_total"],
        "jito_bundle_success": st["jito_bundle_success"],
        "jito_success_rate": st["jito_success_rate"],
        "health_score": st["health_score"],
        "health_history": st["health_history"],
        "state_updated_at": st["state_updated_at"],
        "state_updated_at_iso": st["state_updated_at_iso"],
        "state_age_seconds": st["state_age_seconds"],
        "process_uptime_human": st["process_uptime_human"],
        "source_of_truth": st.get("source_of_truth"),
        "provider_health": st.get("provider_health"),
    }
    # Diagnostic instrumentation fields
    st["entry_funnel"] = st.get("entry_funnel", {})
    st["terminal_blockers_summary"] = st.get("terminal_blockers_summary", {})
    st["execution_audit"] = st.get("execution_audit", {})
    st["candidate_timing"] = st.get("candidate_timing", {})
    st["rpc_health"] = st.get("rpc_health", {})
    last_trade = dict(st.get("last_trade") or {})
    if last_trade:
        last_mint = str(last_trade.get("mint") or "")
        last_entry_ts = int(last_trade.get("entry_ts") or last_trade.get("ts") or 0)
        closed_positions = list(st.get("closed_positions") or [])
        closed_match = next((row for row in reversed(closed_positions) if str(row.get("mint") or "") == last_mint and int(row.get("entry_ts") or 0) == last_entry_ts), None)
        if closed_match:
            last_trade["closed"] = True
            last_trade["close_reason"] = str(closed_match.get("close_reason") or "") or None
            last_trade["exit_ts"] = int(closed_match.get("exit_ts") or 0) or None
            last_trade["realized_roi_pct"] = closed_match.get("realized_roi_pct")
            last_trade["realized_pnl_usd"] = closed_match.get("realized_pnl_usd")
            last_trade["note"] = "Resolved via canonical closed position truth."
            st["last_trade"] = last_trade
        else:
            terminal_positions = list(st.get("terminal_positions") or [])
            terminal_match = next((row for row in reversed(terminal_positions) if str(row.get("mint") or "") == last_mint and int(row.get("entry_ts") or 0) == last_entry_ts), None)
            if terminal_match:
                last_trade["closed"] = True
                last_trade["terminalized"] = True
                last_trade["terminalized_ts"] = int(terminal_match.get("terminalized_ts") or 0)
                last_trade["terminalized_reason"] = str(terminal_match.get("reason") or terminal_match.get("type") or "terminalized")
                last_trade["note"] = "Resolved via terminal position reconciliation; no canonical trade_close was fabricated."
                st["last_trade"] = last_trade
    st["filter_pressure"] = _compute_filter_pressure(st)
    st["next_experiment_readiness"] = _compute_next_experiment(st)
    st["mode_safety"] = _compute_mode_safety(st)
    st["wallet_reconciliation"] = _compute_wallet_reconciliation(st)

    # ── CANONICAL EXECUTION CONFIG SURFACE ────────────────────────────────────
    # Single authoritative object for all risk/execution display fields.
    # Frontend should read ONLY from this object — never from top-level stale fields.
    controls = st.get("operator_controls") or {}
    canary_data = st.get("live_canary") or {}
    ms = st.get("mode_safety") or {}
    st["execution_config"] = {
        # Risk thresholds — always current env values (normalized in enrich_execution_fields)
        "default_downside_stop_pct": float(controls.get("default_downside_stop_pct", float(os.getenv("DEFAULT_DOWNSIDE_STOP_PCT", "0.12")))),
        "absolute_failsafe_stop_pct": float(controls.get("absolute_failsafe_stop_pct", float(os.getenv("ABSOLUTE_FAILSAFE_STOP_PCT", "0.20")))),
        "displayed_failsafe_stop_pct": float(canary_data.get("failsafe_stop_pct") or controls.get("absolute_failsafe_stop_pct") or float(os.getenv("ABSOLUTE_FAILSAFE_STOP_PCT", "0.20"))),
        # Trade sizing
        "trade_size_usd": ms.get("trade_size_usd") or canary_data.get("trade_size_usd"),
        "max_concurrent": ms.get("max_concurrent") or canary_data.get("max_concurrent"),
        "daily_loss_cap_usd": ms.get("daily_loss_cap_usd") or canary_data.get("max_daily_loss_usd"),
        # CU / Jito execution
        "cu_price": int(st.get("cu_price") or 10000),
        "cu_limit": st.get("cu_limit"),
        "jito_tip": int(st.get("jito_tip") or 20000),
        "jito_tip_mode": st.get("jito_tip_mode") or canary_data.get("jito_tip_mode") or "flat",
        "jito_tip_base": int(canary_data.get("jito_tip_base") or int(os.getenv("JITO_TIP_LAMPORTS", "20000"))),
        "jito_tip_high": int(canary_data.get("jito_tip_high") or int(os.getenv("JITO_TIP_HIGH_LAMPORTS", "50000"))),
        "jito_dynamic_enabled": bool(canary_data.get("jito_dynamic_enabled", os.getenv("JITO_DYNAMIC_TIP_ENABLED", "true").strip().lower() == "true")),
        "jito_early_entry_age_ms": int(canary_data.get("jito_early_entry_age_ms") or int(os.getenv("JITO_EARLY_ENTRY_TIP_AGE_MS", "5000"))),
        # Profile / mode
        "active_config_source": ms.get("active_env_source") or ".env",
        "active_profile": ms.get("current_profile") or "UNKNOWN",
        "execution_mode": st.get("execution_mode") or "paper",
        # Task 4/6: Price pipeline + retry telemetry for debugging entry veto issues
        "price_pipeline_telemetry": {
            "qualified_candidates_processed": st.get("entry_funnel", {}).get("qualified_candidates", 0),
            "total_price_veto_count": st.get("entry_funnel", {}).get("veto_price", 0),
            "veto_rate_pct": round(int(st.get("entry_funnel", {}).get("veto_price", 0)) / max(1, int(st.get("entry_funnel", {}).get("qualified_candidates", 1))) * 100, 1),
            "entry_eligible_candidates": st.get("entry_funnel", {}).get("entry_eligible", 0),
            "execution_attempted_count": st.get("entry_funnel", {}).get("execution_attempted", 0),
            # Migration retry breakdown
            "migration_retry_enqueued": st.get("entry_funnel", {}).get("migration_retry_enqueued", 0),
            "migration_retry_attempts_total": st.get("entry_funnel", {}).get("migration_retry_attempts_total", 0),
            "migration_retry_price_found": st.get("entry_funnel", {}).get("migration_retry_price_found", 0),
            "migration_retry_exhausted_count": st.get("entry_funnel", {}).get("migration_retry_exhausted_count", 0),
            "migration_retry_missing_ref_price_count": st.get("entry_funnel", {}).get("migration_retry_missing_ref_price_count", 0),
            # Delayed entry breakdown
            "delayed_entry_enqueued": st.get("entry_funnel", {}).get("delayed_entry_enqueued", 0),
            "delayed_entry_price_alive": st.get("entry_funnel", {}).get("delayed_entry_price_alive", 0),
            "delayed_entry_dump_discarded": st.get("entry_funnel", {}).get("delayed_entry_dump_discarded", 0),
        },
        # P2-1 / P2-3: blocked-entry breakdown — these do NOT inflate execution_attempted
        "entry_blocked_breakdown": {
            "trading_disabled": st.get("entry_funnel", {}).get("entry_blocked_trading_disabled", 0),
            "duplicate_mint": st.get("entry_funnel", {}).get("entry_blocked_duplicate", 0),
            "budget_exhausted": st.get("entry_funnel", {}).get("entry_blocked_budget", 0),
            "note": "These are pre-execution blocks and are excluded from execution_attempted counter",
        },
    }
    # P4.4: survivorship — the win-rate denominator should be entered trades, but
    # analytical questions like "is our entry filter selecting good candidates?"
    # need the qualified-but-not-entered cohort for contrast. Expose it explicitly.
    _fu = st.get("entry_funnel", {}) or {}
    _qualified   = int(_fu.get("qualified_candidates", 0) or 0)
    _entered_ok  = int(_fu.get("execution_succeeded", 0) or 0)
    _entered_try = int(_fu.get("execution_attempted", 0) or 0)
    _blocked = (
        int(_fu.get("entry_blocked_trading_disabled", 0) or 0)
        + int(_fu.get("entry_blocked_duplicate",        0) or 0)
        + int(_fu.get("entry_blocked_budget",           0) or 0)
    )
    st["survivorship"] = {
        "qualified_candidates": _qualified,
        "entered_attempted":    _entered_try,
        "entered_filled":       _entered_ok,
        "blocked_pre_execution": _blocked,
        "qualified_but_not_entered": max(0, _qualified - _entered_try),
        "entry_conversion_pct": round((_entered_ok / _qualified) * 100.0, 2) if _qualified else 0.0,
        "note": ("qualified_but_not_entered is the cohort to compare against entered "
                 "trades — if both cohorts have similar outcome distributions on "
                 "observable features, scoring has no edge."),
    }
    # ── END CANONICAL EXECUTION CONFIG ────────────────────────────────────────

    return st


async def tg_send(text: str):
    if not SNIPER_TG_TOKEN or not SNIPER_TG_CHAT_ID:
        return
    import aiohttp
    url = f"https://api.telegram.org/bot{SNIPER_TG_TOKEN}/sendMessage"
    payload = {"chat_id": SNIPER_TG_CHAT_ID, "text": text}
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json=payload, timeout=6)
    except Exception:
        pass


_last_balance_fetch = 0
_last_balance_sol = None
_last_sol_usd = None
_last_jito_check = 0
_last_wallet_spl_tokens: list[dict] = []
_last_wallet_spl_ts: float = 0.0
FAST_WALLET_REFRESH_SECONDS = float(os.getenv("FAST_WALLET_REFRESH_SECONDS", "5"))
SLOW_WALLET_REFRESH_SECONDS = float(os.getenv("SLOW_WALLET_REFRESH_SECONDS", "60"))
POST_CLOSE_FAST_REFRESH_WINDOW_SECONDS = float(os.getenv("POST_CLOSE_FAST_REFRESH_WINDOW_SECONDS", "90"))

_WSOL_MINT = "So11111111111111111111111111111111111111112"
_SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_TOKEN22_PROGRAM   = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


async def fetch_sol_price_usd() -> float | None:
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://price.jup.ag/v4/price?ids=SOL", timeout=5) as r:
                if r.status == 200:
                    d = await r.json()
                    return float(d["data"]["SOL"]["price"])
    except Exception:
        pass
    return None


async def fetch_wallet_balance_sol() -> float | None:
    # Paper mode: if no wallet pubkey configured, skip remote balance fetch entirely.
    if not WALLET_PUBKEY or len(WALLET_PUBKEY.strip()) < 32:
        return None
    import aiohttp
    body = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [WALLET_PUBKEY]}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(RPC_URL, json=body, timeout=6) as r:
                if r.status != 200:
                    return None
                d = await r.json()
                lamports = (((d.get("result") or {}).get("value")) or 0)
                return round(lamports / 1_000_000_000, 6)
    except Exception:
        return None


async def fetch_wallet_spl_tokens() -> list[dict]:
    """
    Fetch all non-zero, non-WSOL token balances held by the wallet.
    Queries both classic SPL Token Program and Token-2022 Program, deduplicates by mint.
    Each record includes a 'token_program' field ('spl' or 'token22') for auditability.
    """
    if not WALLET_PUBKEY or len(WALLET_PUBKEY.strip()) < 32:
        return []
    import aiohttp

    async def _query_program(session: aiohttp.ClientSession, program_id: str, label: str) -> list[dict]:
        body = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                WALLET_PUBKEY,
                {"programId": program_id},
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        }
        try:
            async with session.post(RPC_URL, json=body, timeout=10) as r:
                if r.status != 200:
                    return []
                d = await r.json()
                accounts = ((d.get("result") or {}).get("value") or [])
                tokens = []
                for acct in accounts:
                    info = ((((acct.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
                    mint = str(info.get("mint") or "")
                    token_amount = info.get("tokenAmount") or {}
                    ui_amount = float(token_amount.get("uiAmount") or 0.0)
                    decimals = int(token_amount.get("decimals") or 0)
                    if not mint or mint == _WSOL_MINT or ui_amount <= 0:
                        continue
                    tokens.append({
                        "mint": mint,
                        "ui_amount": round(ui_amount, decimals),
                        "decimals": decimals,
                        "token_program": label,
                    })
                return tokens
        except Exception:
            return []

    try:
        async with aiohttp.ClientSession() as s:
            spl_tokens    = await _query_program(s, _SPL_TOKEN_PROGRAM, "spl")
            token22_tokens = await _query_program(s, _TOKEN22_PROGRAM,  "token22")

        # Merge, deduplicate by mint (SPL takes precedence if both somehow return same mint)
        seen: dict[str, dict] = {}
        for tok in token22_tokens:
            seen[tok["mint"]] = tok
        for tok in spl_tokens:
            seen[tok["mint"]] = tok  # overwrite token22 if duplicate

        return list(seen.values())
    except Exception:
        return []


async def probe_jito_health() -> bool:
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(JITO_BLOCK_ENGINE_URL, timeout=6) as r:
                return r.status < 500
    except Exception:
        return False


async def broadcaster_loop():
    global LAST_WARN_TS, _last_balance_fetch, _last_balance_sol, _last_sol_usd, _last_jito_check
    global HUD_HEALTH_HISTORY, HUD_JITO_BUNDLE_TOTAL, HUD_JITO_BUNDLE_SUCCESS
    global _last_wallet_spl_tokens, _last_wallet_spl_ts
    while True:
        raw_state = await asyncio.get_running_loop().run_in_executor(None, read_state)

        last_trade = dict(raw_state.get("last_trade") or {})
        if last_trade:
            last_mint = str(last_trade.get("mint") or "")
            last_entry_ts = int(last_trade.get("entry_ts") or last_trade.get("ts") or 0)
            closed_positions = list(raw_state.get("closed_positions") or [])
            closed_match = next((row for row in reversed(closed_positions) if str(row.get("mint") or "") == last_mint and int(row.get("entry_ts") or 0) == last_entry_ts), None)
            if closed_match:
                last_trade["closed"] = True
                last_trade["close_reason"] = str(closed_match.get("close_reason") or "") or None
                last_trade["exit_ts"] = int(closed_match.get("exit_ts") or 0) or None
                last_trade["realized_roi_pct"] = closed_match.get("realized_roi_pct")
                last_trade["realized_pnl_usd"] = closed_match.get("realized_pnl_usd")
                last_trade["note"] = "Resolved via canonical closed position truth."
            else:
                terminal_positions = list(raw_state.get("terminal_positions") or [])
                terminal_match = next((row for row in reversed(terminal_positions) if str(row.get("mint") or "") == last_mint and int(row.get("entry_ts") or 0) == last_entry_ts), None)
                if terminal_match:
                    last_trade["closed"] = True
                    last_trade["terminalized"] = True
                    last_trade["terminalized_ts"] = int(terminal_match.get("terminalized_ts") or 0)
                    last_trade["terminalized_reason"] = str(terminal_match.get("reason") or terminal_match.get("type") or "terminalized")
                    last_trade["note"] = "Resolved via terminal position reconciliation; no canonical trade_close was fabricated."
        # For closed trades, measure from exit_ts so POST_CLOSE_FAST_REFRESH_WINDOW_SECONDS
        # is counted from the close, not from entry. Without this, any trade held > 90s
        # immediately drops to slow-scan on close, leaving a stale "unmanaged" wallet gap.
        _close_ts = float(last_trade.get("exit_ts") or 0.0) if last_trade.get("closed") else 0.0
        _age_ref_ts = _close_ts or float(last_trade.get("ts") or 0.0)
        last_trade_age = max(0.0, time.time() - _age_ref_ts) if _age_ref_ts else float("inf")
        open_rows = list(raw_state.get("active_positions") or raw_state.get("paper_positions") or [])
        has_open_managed = any(str(r.get("status") or "") != "closed" and float(r.get("qty_remaining") or 0.0) > 1e-9 for r in open_rows)
        recent_close_window = bool(last_trade.get("closed") and last_trade_age <= POST_CLOSE_FAST_REFRESH_WINDOW_SECONDS)
        wallet_refresh_seconds = FAST_WALLET_REFRESH_SECONDS if (has_open_managed or recent_close_window) else SLOW_WALLET_REFRESH_SECONDS

        # Wallet balance + SOL price monitor + SPL token inventory.
        # Run fast while a position is open or immediately after a close so reconciliation converges promptly.
        if time.time() - _last_balance_fetch > wallet_refresh_seconds:
            _last_balance_fetch = time.time()
            bal = await fetch_wallet_balance_sol()
            if bal is not None:
                _last_balance_sol = bal
            sol_p = await fetch_sol_price_usd()
            if sol_p is not None:
                _last_sol_usd = sol_p
            spl = await fetch_wallet_spl_tokens()
            _last_wallet_spl_tokens = spl
            _last_wallet_spl_ts = time.time()

        # Run enrich_hud_state in a thread pool so the event loop isn't blocked
        # by the 300-500ms of synchronous JSON processing / history reads.
        st = await asyncio.get_running_loop().run_in_executor(None, enrich_hud_state, raw_state)

        # Rolling health history for sparkline (1 point/min, 6h window) — in memory only.
        now_ts = int(time.time())
        if (not HUD_HEALTH_HISTORY) or (now_ts - int(HUD_HEALTH_HISTORY[-1].get("ts", 0) or 0) >= 60):
            HUD_HEALTH_HISTORY.append({"ts": now_ts, "score": float(st["health_score"].get("score", 0.0) or 0.0)})
            HUD_HEALTH_HISTORY = HUD_HEALTH_HISTORY[-360:]
            st["health_history"] = list(HUD_HEALTH_HISTORY)
            st["hud_derived"]["health_history"] = st["health_history"]

        # Active Jito block engine health probe (every 60s) — in memory only.
        if time.time() - _last_jito_check > 60:
            _last_jito_check = time.time()
            ok = await probe_jito_health()
            HUD_JITO_BUNDLE_TOTAL += 1
            if ok:
                HUD_JITO_BUNDLE_SUCCESS += 1
            st["jito_bundle_total"] = int(HUD_JITO_BUNDLE_TOTAL)
            st["jito_bundle_success"] = int(HUD_JITO_BUNDLE_SUCCESS)
            total_probe = int(st.get("jito_bundle_total", 0) or 0)
            succ_probe = int(st.get("jito_bundle_success", 0) or 0)
            st["jito_success_rate"] = (succ_probe / total_probe) * 100.0 if total_probe > 0 else 100.0
            st["hud_derived"]["jito_bundle_total"] = st["jito_bundle_total"]
            st["hud_derived"]["jito_bundle_success"] = st["jito_bundle_success"]
            st["hud_derived"]["jito_success_rate"] = st["jito_success_rate"]
            st["health_score"] = compute_health_score(st)
            st["hud_derived"]["health_score"] = st["health_score"]

        # Jito health warning
        rate = float(st.get("jito_success_rate", 100.0) or 100.0)
        total = int(st.get("jito_bundle_total", 0) or 0)
        if total >= 5 and rate < 90.0 and (time.time() - LAST_WARN_TS) > 300:
            LAST_WARN_TS = int(time.time())
            await tg_send(f"⚠️ Jito status degraded: success rate {rate:.1f}% ({int(st.get('jito_bundle_success',0))}/{total})")

        dead = []
        payload = json.dumps(st)
        now_ts = time.time()
        for ws in CLIENTS:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
                continue
            last_pong = CLIENT_LAST_PONG.get(ws, now_ts)
            if now_ts - last_pong > WS_STALE_TIMEOUT_SEC:
                dead.append(ws)
        for ws in dead:
            CLIENTS.discard(ws)
            CLIENT_LAST_PONG.pop(ws, None)
            try:
                await ws.close()
            except Exception:
                pass
        await asyncio.sleep(1)


@app.on_event("startup")
async def startup_event():
    hydrate_hud_derived_from_state()
    asyncio.create_task(broadcaster_loop())


@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}


@app.get("/hud")
def hud():
    return enrich_hud_state(read_state())


def _compute_wallet_reconciliation(st: dict) -> dict:
    """
    Cross-reference wallet-held SPL tokens against canonical managed positions.

    unmanaged_wallet_mints  = wallet holds tokens that have no open managed position
                              (orphaned real holdings — not tracked by stop-loss/MTM)
    managed_missing_from_wallet = bot has an open position record but no wallet balance
                              (may have exited on-chain without a close event, or sentinel entry)
    """
    is_live = str(st.get("execution_mode") or "paper").lower() == "live"

    active_positions = list(st.get("active_positions") or [])
    paper_positions = list(st.get("paper_positions") or [])
    canonical_open_positions = active_positions if active_positions else [
        row for row in paper_positions
        if str(row.get("status") or "") != "closed" and float(row.get("qty_remaining") or 0.0) > 1e-9
    ]
    all_open_mints = {
        str(row.get("mint") or "")
        for row in canonical_open_positions
        if str(row.get("mint") or "")
    }

    wallet_tokens = list(_last_wallet_spl_tokens)
    wallet_mints = {str(t.get("mint") or "") for t in wallet_tokens if t.get("mint")}

    unmanaged_mints = sorted(wallet_mints - all_open_mints)
    managed_missing = sorted(all_open_mints - wallet_mints) if is_live else []

    scan_age = round(time.time() - _last_wallet_spl_ts, 1) if _last_wallet_spl_ts else None

    # Classify unmanaged tokens: real holdings with no managed position
    unmanaged_detail = []
    for t in wallet_tokens:
        m = str(t.get("mint") or "")
        if m in unmanaged_mints:
            unmanaged_detail.append({
                "mint": m,
                "ui_amount": t.get("ui_amount"),
                "decimals": t.get("decimals"),
                "classification": "unmanaged_real_holding",
                "risk": "NOT covered by stop-loss or MTM — paper-only exits do not sell on-chain",
            })

    # Classify ghost positions: managed record with no wallet backing
    ghost_detail = []
    for m in managed_missing:
        pp_row = next((r for r in canonical_open_positions if str(r.get("mint") or "") == m), {}) or next((r for r in paper_positions if str(r.get("mint") or "") == m), {})
        ghost_detail.append({
            "mint": m,
            "status": str(pp_row.get("status") or "open"),
            "size_usd": pp_row.get("size_usd"),
            "entry_ts": pp_row.get("entry_ts"),
            "classification": "ghost_position",
            "risk": "Open managed record but no wallet tokens — TX may have failed on-chain or tokens already exited",
        })

    return {
        "enabled": is_live,
        "last_wallet_scan_ts": _last_wallet_spl_ts or None,
        "last_wallet_scan_age_seconds": scan_age,
        "wallet_spl_token_count": len(wallet_tokens),
        "wallet_spl_tokens": wallet_tokens,
        "all_open_position_mint_count": len(all_open_mints),
        "all_open_position_mints": sorted(all_open_mints),
        "unmanaged_wallet_mint_count": len(unmanaged_mints),
        "unmanaged_wallet_mints": unmanaged_mints,
        "unmanaged_wallet_detail": unmanaged_detail,
        "managed_missing_from_wallet_count": len(managed_missing),
        "managed_missing_from_wallet_mints": managed_missing,
        "ghost_position_detail": ghost_detail,
        # Only report CLEAN if the wallet scan is fresh (< 60s old).
        # A stale scan can miss newly-received tokens or recently-exited positions.
        "reconciliation_clean": (
            len(unmanaged_mints) == 0
            and len(managed_missing) == 0
            and scan_age is not None
            and scan_age < 60.0
        ),
        "reconciliation_stale": scan_age is None or scan_age >= 60.0,
        "note": (
            "unmanaged_real_holding = wallet holds token, no managed position, no stop-loss coverage "
            "(paper exits do not sell on-chain — LIVE SELL GAP); "
            "ghost_position = open managed record but no wallet tokens "
            "(TX failed on-chain, or already paper-exited)"
        ),
    }


def _compute_filter_pressure(st: dict) -> dict:
    ef = st.get("entry_funnel") or {}
    qc = max(1, int(ef.get("qualified_candidates", 0) or 0))
    return {
        "liquidity_veto_share_pct": round(int(ef.get("veto_liquidity", 0) or 0) / qc * 100, 1),
        "dependency_veto_share_pct": round(int(ef.get("veto_dependency", 0) or 0) / qc * 100, 1),
        "price_veto_share_pct": round(int(ef.get("veto_price", 0) or 0) / qc * 100, 1),
        "entry_progression_rate_pct": round(int(ef.get("entry_eligible", 0) or 0) / qc * 100, 1),
        "execution_reach_rate_pct": round(int(ef.get("execution_attempted", 0) or 0) / qc * 100, 1),
    }


def _compute_next_experiment(st: dict) -> dict:
    ef = st.get("entry_funnel") or {}
    hs = (st.get("health_score") or {}).get("score", 0)
    real_qc = int(ef.get("qualified_candidates", 0) or 0)
    qc = max(1, real_qc)
    liq_pct = int(ef.get("veto_liquidity", 0) or 0) / qc * 100
    dep_pct = int(ef.get("veto_dependency", 0) or 0) / qc * 100
    exec_attempted = int(ef.get("execution_attempted", 0) or 0)
    # When no candidates qualified at all, both pcts are 0/1=0.
    # "0 >= 0" is vacuously True and would falsely attribute liquidity_lock.
    # Return "no_candidates" so the panel accurately reflects an empty pipeline.
    if real_qc == 0:
        dominant = "no_candidates"
    elif liq_pct >= dep_pct:
        dominant = "liquidity_lock"
    else:
        dominant = "dependency_friction"
    return {
        "runtime_healthy": bool(float(hs) >= 80.0),
        "execution_reached_this_session": exec_attempted > 0,
        "dominant_blocker": dominant,
        "dependency_material": dep_pct > 5.0,
        "liquidity_veto_dominant": liq_pct > 70.0,
        "health_score": float(hs),
    }


def _compute_mode_safety(st: dict) -> dict:
    mode = st.get("execution_mode", "paper")
    is_live = mode in ("live", "live_canary")
    controls = st.get("operator_controls") or {}
    entries_disabled = bool(controls.get("disable_new_entries", False))
    # Two independent flags — read from operator_controls (set by sim engine on command processing)
    live_tx_submission_enabled = bool(controls.get("live_tx_submission_enabled", False))
    # Prefer top-level st["trading_enabled"] which enrich_hud_state already resolved from
    # canary_data when operator_controls lacks the key — avoids Mode Safety showing DISABLED
    # while Critical Status shows ENABLED (split-brain from two different read paths).
    trading_enabled = bool(st["trading_enabled"] if "trading_enabled" in st else controls.get("trading_enabled", False))
    sim_budget = st.get("sim_budget_usd")
    real_wallet_sol = st.get("real_wallet_sol")
    sp = st.get("startup_profile") or {}
    profile = sp.get("current_profile") or (os.getenv("ACTIVE_PROFILE", "").strip().upper() or ("LIVE_CANARY_SAFE" if is_live else "PAPER"))
    env_source = sp.get("active_env_source") or ".env"
    verified = bool(sp.get("startup_ts"))
    lock_policy = sp.get("lock_policy") or (os.getenv("LOCK_POLICY", "standard").strip().lower())
    lp_pct_min = sp.get("research_lp_lock_pct_min")
    is_research = profile == "LIVE_CANARY_RESEARCH"
    # Canary risk caps from live_canary module if available
    canary_info = {}
    try:
        from live_canary import CANARY_TRADE_SIZE_USD, CANARY_MAX_CONCURRENT, CANARY_MAX_DAILY_LOSS_USD
        canary_info = {
            "trade_size_usd": CANARY_TRADE_SIZE_USD,
            "max_concurrent": CANARY_MAX_CONCURRENT,
            "daily_loss_cap_usd": CANARY_MAX_DAILY_LOSS_USD,
        }
    except Exception:
        pass
    # Jupiter telemetry from live_canary rate limiter
    jupiter_info: dict = {}
    try:
        from live_canary import get_jupiter_telemetry
        jupiter_info = get_jupiter_telemetry()
    except Exception:
        pass
    research_warning = (
        "!! RESEARCH PROFILE ACTIVE — Modified lock policy. Real funds. Supervised run only."
        if is_research else None
    )
    lock_policy_label = {
        "standard": "STANDARD (locked=true OR burned=true)",
        "lp_pct_threshold": f"LP_PCT_THRESHOLD (pump_fun lpLockedPct>={lp_pct_min or 95}% AND lpUnlocked=0)",
    }.get(lock_policy, lock_policy)
    # Canonical operator-visible sell execution truth.
    # Single source of truth for both the top banner and Mode Safety table.
    if is_live:
        sell_execution_status = {
            "code": "onchain_sell_active",
            "headline": "CLOSED — on-chain sell TX active",
            "detail": "Live exits submit real on-chain sell transactions.",
            "banner_enabled": False,
            "severity": "ok",
        }
    else:
        sell_execution_status = {
            "code": "paper_mode",
            "headline": "N/A (Paper Only)",
            "detail": "Paper mode does not submit on-chain sell transactions.",
            "banner_enabled": False,
            "severity": "dim",
        }

    return {
        "current_profile": profile,
        "current_mode": mode,
        "active_env_source": env_source,
        "profile_startup_verified": verified,
        "lock_policy": lock_policy,
        "lock_policy_label": lock_policy_label,
        "research_warning": research_warning,
        "paper_entries_allowed": not entries_disabled and not is_live,
        "live_entries_allowed": not entries_disabled and is_live,
        # TX submission and trading are independent — tracked and displayed separately
        "live_tx_submission_enabled": live_tx_submission_enabled,
        "live_tx_label": ("ARMED" if live_tx_submission_enabled else "DISARMED") if is_live else "N/A (Paper Only)",
        "trading_enabled": trading_enabled,
        "trading_label": ("ENABLED" if (trading_enabled and live_tx_submission_enabled) else "DISABLED") if is_live else "N/A (Paper Only)",
        # Legacy field kept for backwards-compat: buys are live only when both flags are true
        "live_tx_submission_allowed": is_live and live_tx_submission_enabled and trading_enabled,
        "real_wallet_in_use": is_live and real_wallet_sol is not None,
        "sim_budget_authoritative": not is_live,
        "sim_budget_analytics_only": is_live,
        "sim_budget_usd": None if is_live else sim_budget,
        **canary_info,
        "jupiter": jupiter_info,
        "sell_execution_status": sell_execution_status,
        # Back-compat legacy fields now derive from canonical sell_execution_status only.
        "live_sell_gap": bool(sell_execution_status.get("code") == "paper_only_exits"),
        "live_sell_gap_warning": sell_execution_status.get("detail") if sell_execution_status.get("code") == "paper_only_exits" else None,
    }


@app.get("/hud_digest")
def hud_digest():
    global _digest_cache, _digest_cache_ts
    now = time.monotonic()
    if _digest_cache is not None and (now - _digest_cache_ts) < _DIGEST_CACHE_TTL_S:
        return _digest_cache
    st = enrich_hud_state(read_state())
    result = {
        "state_updated_at": st.get("state_updated_at"),
        "pnl_usd": st.get("pnl_usd"),
        "sim_budget_usd": None if st.get("execution_mode") == "live" else st.get("sim_budget_usd"),
        "closed_positions_count": st.get("closed_positions_count"),
        "open_positions": (st.get("headline") or {}).get("open_positions"),
        "anomaly_open_count": (((st.get("analytics_phase5c") or {}).get("current_open") or {}).get("anomaly_count")),
        "entry_funnel": st.get("entry_funnel", {}),
        "terminal_blockers_summary": st.get("terminal_blockers_summary", {}),
        "execution_audit": st.get("execution_audit", {}),
        "candidate_timing": st.get("candidate_timing", {}),
        "rpc_health": st.get("rpc_health", {}),
        "filter_pressure": _compute_filter_pressure(st),
        "next_experiment_readiness": _compute_next_experiment(st),
        "mode_safety": _compute_mode_safety(st),
        "telemetry_health": st.get("telemetry_health", {}),
        "trust_level": st.get("trust_level", "unknown"),
        "trust_reasons": st.get("trust_reasons", []),
        "hud_last_reconciled_at": st.get("hud_last_reconciled_at"),
        # P1-2: clean latency profile surface for operator dashboards
        "latency_profile": {
            "avg_total_ms": (st.get("latency_breakdown") or {}).get("history_execution_total_avg_ms", 0.0),
            "p50_ms": (st.get("latency_breakdown") or {}).get("p50_total_execution_attempt_latency_ms", 0.0),
            "p95_ms": (st.get("latency_breakdown") or {}).get("p95_total_execution_attempt_latency_ms", 0.0),
            "avg_detect_to_exec_ms": (st.get("latency_breakdown") or {}).get("avg_detect_to_exec_ms"),
            "avg_decision_ms": (st.get("latency_breakdown") or {}).get("decision_avg_latency_ms", 0.0),
            "avg_exec_submit_ms": (st.get("latency_breakdown") or {}).get("execution_avg_latency_ms", 0.0),
            "bottleneck": (st.get("latency_breakdown") or {}).get("dominant_component", "unknown"),
            "source": "history.json:execution_attempt",
        },
        # P2-1 / P2-3: blocked-entry breakdown (separate from execution failures)
        "entry_blocked_breakdown": {
            "trading_disabled": (st.get("entry_funnel") or {}).get("entry_blocked_trading_disabled", 0),
            "duplicate_mint": (st.get("entry_funnel") or {}).get("entry_blocked_duplicate", 0),
            "budget_exhausted": (st.get("entry_funnel") or {}).get("entry_blocked_budget", 0),
        },
    }
    _digest_cache = result
    _digest_cache_ts = now
    return result


@app.get("/", response_class=HTMLResponse)
def index():
    html = (BASE / "index.html").read_text() if (BASE / "index.html").exists() else "<h1>No index.html</h1>"
    return HTMLResponse(html, headers=NO_STORE_HEADERS)


@app.get("/style.css")
def style_css():
    p = BASE / "style.css"
    if p.exists():
        return FileResponse(p, media_type="text/css", headers=NO_STORE_HEADERS)
    return HTMLResponse("", status_code=404, headers=NO_STORE_HEADERS)


@app.get("/app.js")
def app_js():
    p = BASE / "app.js"
    if p.exists():
        return FileResponse(p, media_type="application/javascript", headers=NO_STORE_HEADERS)
    return HTMLResponse("", status_code=404, headers=NO_STORE_HEADERS)



@app.post("/control")
def control(payload: dict = Body(...)):
    action = str((payload or {}).get("action") or "").strip().lower()
    mint = str((payload or {}).get("mint") or "").strip()
    fraction = float((payload or {}).get("fraction") or 0.0)
    value = bool((payload or {}).get("value", True))

    if action == "disable_new_entries":
        cmd = enqueue_operator_command("set_disable_new_entries", value=value)
        return {"ok": True, "queued": cmd}
    if action == "flatten_all":
        cmd = enqueue_operator_command("flatten_all")
        return {"ok": True, "queued": cmd}
    if action == "sell_fraction":
        _validate_mint(mint)
        if fraction <= 0 or fraction > 1:
            raise HTTPException(status_code=400, detail="fraction must be in (0,1]")
        cmd = enqueue_operator_command("sell_fraction", mint=mint, fraction=fraction)
        return {"ok": True, "queued": cmd}
    
    # Live canary controls — routed through operator_commands queue so sim engine process acts on them

    if action == "enable_live_tx":
        # Step 1 of two-step arm: enable TX infrastructure only — does NOT enable trading/buys
        cmd = enqueue_operator_command("enable_live_tx")
        return {
            "ok": True, "action": "enable_live_tx", "queued": cmd,
            "note": "Live TX submission armed. Trading (buys) remains DISABLED. Call enable_trading to allow new entries.",
        }

    if action == "disable_live_tx":
        # Disarms TX infrastructure. Safety cascade also disables trading.
        cmd = enqueue_operator_command("disable_live_tx")
        return {
            "ok": True, "action": "disable_live_tx", "queued": cmd,
            "note": "Live TX submission disarmed. Trading also disabled (safety cascade).",
        }

    if action == "enable_trading":
        # Step 2 of two-step arm: requires live_tx_submission_enabled first
        st = read_state()
        controls = dict(st.get("operator_controls") or {})
        if str(st.get("execution_mode") or "paper").lower() == "live":
            if not controls.get("live_tx_submission_enabled", False):
                raise HTTPException(
                    status_code=400,
                    detail="live_tx_submission_not_enabled: arm Live TX Submission first (action=enable_live_tx)",
                )
        cmd = enqueue_operator_command("enable_trading")
        return {"ok": True, "action": "enable_trading", "queued": cmd,
                "note": "Queued for sim engine. trading_enabled will update in HUD within seconds."}

    if action == "disable_trading":
        cmd = enqueue_operator_command("disable_trading")
        return {"ok": True, "action": "disable_trading", "queued": cmd,
                "note": "Queued for sim engine. trading_enabled will update in HUD within seconds."}

    # Mode switching — requires restart; records pending_execution_mode in state for HUD display
    if action in ("set_mode_paper", "set_mode_live"):
        requested = "paper" if action == "set_mode_paper" else "live"
        active = read_state().get("execution_mode") or "paper"
        # P5.1: route through operator_commands.jsonl so sim owns the state write.
        enqueue_operator_command("set_pending_execution_mode", requested=requested)
        return {
            "ok": True,
            "action": action,
            "requested_mode": requested,
            "active_mode": active,
            "requires_restart": True,
            "note": (
                f"Mode switch to '{requested}' requires restart. "
                f"Set EXECUTION_MODE={requested} in .env"
                + (" and ALLOW_LIVE_TRADING=YES_I_UNDERSTAND_LIVE_RISK" if requested == "live" else "")
                + f", then restart sim engine. Currently active: '{active}'."
            ),
        }

    if action == "inject_position":
        _validate_mint(mint)
        qty = float((payload or {}).get("qty") or 0.0)
        entry_price_usd = float((payload or {}).get("entry_price_usd") or 0.0)
        if qty <= 0:
            raise HTTPException(status_code=400, detail="qty must be > 0")
        if entry_price_usd < 0:
            raise HTTPException(status_code=400, detail="entry_price_usd must be >= 0")
        size_usd = float((payload or {}).get("size_usd") or 0.0)
        if size_usd <= 0 and entry_price_usd > 0:
            size_usd = round(qty * entry_price_usd, 6)
        cmd = enqueue_operator_command(
            "inject_position",
            mint=mint,
            qty=qty,
            entry_price_usd=entry_price_usd,
            size_usd=size_usd,
        )
        return {
            "ok": True,
            "action": "inject_position",
            "queued": cmd,
            "note": (
                f"Injection queued for sim engine. mint={mint} qty={qty} "
                f"entry_price_usd={entry_price_usd} size_usd={size_usd}. "
                "Position will appear in HUD after next state sync (~2s)."
            ),
        }

    if action == "hard_reset":
        import subprocess, shutil
        profile = str((payload or {}).get("profile") or "").strip().upper()
        if profile and profile in _PROFILE_ENV_MAP:
            src = BASE / _PROFILE_ENV_MAP.get(profile, "")
            dst = BASE / ".env"
            if src.exists():
                shutil.copy2(src, dst)
        reset_script = BASE / "hard-reset.sh"
        if not reset_script.exists():
            raise HTTPException(status_code=500, detail="hard-reset.sh not found")
        # Derive hard-reset.sh $1 arg from profile env filename (strip ".env." prefix)
        _reset_profile_arg = _PROFILE_ENV_MAP.get(profile, "").removeprefix(".env.") if profile else ""
        _reset_cmd = ["bash", str(reset_script)]
        if _reset_profile_arg:
            _reset_cmd.append(_reset_profile_arg)
        proc = subprocess.Popen(
            _reset_cmd,
            cwd=str(BASE),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {
            "ok": True, "action": "hard_reset",
            "profile": profile or "current",
            "pid": proc.pid,
            "note": "hard-reset.sh started. Triad will restart in ~5s.",
        }

    if action == "restart":
        import subprocess
        # Always use hard-reset.sh to restart — it kills existing processes and relaunches.
        # There is no external process supervisor, so SIGTERM alone would leave the sim dead.
        reset_script = BASE / "hard-reset.sh"
        if not reset_script.exists():
            raise HTTPException(status_code=500, detail="hard-reset.sh not found")
        proc = subprocess.Popen(
            ["bash", str(reset_script)],
            cwd=str(BASE),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"ok": True, "action": "restart", "pid": proc.pid,
                "note": "hard-reset.sh started. Triad will restart in ~5s."}

    raise HTTPException(status_code=400, detail="unsupported action")


_PROFILE_ENV_MAP = {
    "LIVE_CANARY_SAFE":     ".env.live_canary",
    "LIVE_CANARY_RESEARCH": ".env.live_canary_research",
    "PAPER":                ".env.paper",
}

@app.post("/profile/switch")
async def profile_switch(payload: dict = Body(...)):
    """
    Switch the active profile and restart the triad.
    Profile must be one of: LIVE_CANARY_SAFE, LIVE_CANARY_RESEARCH, PAPER.
    Copies .env.<profile> → .env then runs hard_reset.sh in the background.
    """
    import subprocess, shutil
    profile = str((payload or {}).get("profile") or "").strip().upper()
    if profile not in _PROFILE_ENV_MAP:
        raise HTTPException(status_code=400, detail=f"profile must be one of {sorted(_PROFILE_ENV_MAP)}")
    src = BASE / _PROFILE_ENV_MAP[profile]
    dst = BASE / ".env"
    if not src.exists():
        raise HTTPException(status_code=404, detail=f"Profile file {src.name} not found")
    try:
        shutil.copy2(src, dst)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to copy profile file: {e}")
    # Run hard-reset.sh in background — this will kill and restart the entire triad including this HUD process
    reset_script = BASE / "hard-reset.sh"
    if not reset_script.exists():
        raise HTTPException(status_code=500, detail="hard_reset.sh not found")
    _switch_profile_arg = _PROFILE_ENV_MAP[profile].removeprefix(".env.")
    subprocess.Popen(
        ["bash", str(reset_script), _switch_profile_arg],
        cwd=str(BASE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {
        "ok": True,
        "switching_to": profile,
        "env_source": _PROFILE_ENV_MAP[profile],
        "restarting": True,
        "note": "Profile copied and hard_reset.sh started. Triad will restart in ~5s. Reconnect HUD after restart.",
    }


@app.post("/operator/reset_session")
def operator_reset_session(payload: dict = Body(...)):
    mode = str((payload or {}).get("mode") or "").strip().lower()
    if mode not in {"metrics_only", "baseline_100"}:
        raise HTTPException(status_code=400, detail="mode must be metrics_only or baseline_100")
    cmd = enqueue_operator_command("reset_session", mode=mode)
    return {"ok": True, "queued": cmd}


@app.get("/api/scorecard")
def api_scorecard(n: int = 10):
    """Mini scorecard: last N trade_close events from history.json."""
    hist = _read_history()
    closes = [x for x in hist if str(x.get("type") or "") == "trade_close"]
    closes.sort(key=lambda x: float(x.get("ts") or 0))
    closes = closes[-max(1, min(n, 200)):]
    if not closes:
        return {"win_rate": 0, "total_pnl": 0, "hard_cap_count": 0,
                "hard_cap_pct": 0, "close_reasons": [], "verdict": "NO_DATA",
                "trades": 0, "wins": 0, "losses": 0}
    wins = [c for c in closes if float(c.get("pnl_usd") or 0) > 0]
    losses = [c for c in closes if float(c.get("pnl_usd") or 0) <= 0]
    total_pnl = round(sum(float(c.get("pnl_usd") or 0) for c in closes), 4)
    win_rate = round(len(wins) / len(closes) * 100, 1)
    hc = [c for c in closes if "HARD_CAP" in str(c.get("close_reason") or "")]
    hc_pct = round(len(hc) / len(closes) * 100, 1)
    reason_counts: dict[str, dict] = {}
    for c in closes:
        r = str(c.get("close_reason") or "unknown")
        row = reason_counts.setdefault(r, {"reason": r, "count": 0, "pnl": 0.0})
        row["count"] += 1
        row["pnl"] = round(row["pnl"] + float(c.get("pnl_usd") or 0), 4)
    close_reasons = sorted(reason_counts.values(), key=lambda x: -x["count"])
    verdict = "PASS" if win_rate >= 35 and total_pnl > 0 else "FAIL"
    return {
        "trades": len(closes), "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate, "total_pnl": total_pnl,
        "hard_cap_count": len(hc), "hard_cap_pct": hc_pct,
        "close_reasons": close_reasons, "verdict": verdict,
    }


@app.get("/api/preentry_stats")
def api_preentry_stats():
    """Count PRE_ENTRY_PRICE rejections from today's log file."""
    import re as _re
    log_candidates = [
        BASE / "logs" / "logs_sim.err",
        BASE / "logs_sim.err", BASE / "sim.err",
        BASE / "bot.err", BASE / "nohup.out",
    ]
    log_file = next((p for p in log_candidates if p.exists()), None)
    ok_count = surge_count = dump_count = 0
    if log_file:
        try:
            text = log_file.read_text(errors="replace")
            ok_count = len(_re.findall(r"PRE_ENTRY_PRICE_OK", text))
            surge_count = len(_re.findall(r"PRE_ENTRY_PRICE_SURGE", text))
            dump_count = len(_re.findall(r"PRE_ENTRY_PRICE_DUMP", text))
        except Exception:
            pass
    total = ok_count + surge_count + dump_count
    return {
        "ok_count": ok_count, "surge_count": surge_count, "dump_count": dump_count,
        "total": total,
        "ok_ratio_pct": round(ok_count / total * 100, 1) if total > 0 else 0,
        "log_file": log_file.name if log_file else None,
    }


# ── Replay & Exit Lab caches (mtime-based) ─────────────────────
_replay_cache: dict = {"mtime": 0.0, "data": None}
_exitlab_cache: dict = {"mtime": 0.0, "n": 0, "data": None}


def _read_history_locked() -> list:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            data = f.read()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return json.loads(data)


def _compute_replay_summary() -> dict:
    """Heavy CPU work — always called via run_in_executor."""
    import replay_v2
    hist = _read_history_locked()
    trades_all = replay_v2.reconstruct_trades(hist)
    all_closed = [t for t in trades_all if t.roi_pct is not None]
    clean = [t for t in all_closed if not replay_v2._is_contaminated(t)]
    post_fix = [t for t in clean if (t.close_ts or 0) > replay_v2.POST_FIX_CUTOFF_TS]
    post_real = [t for t in post_fix if not t.price_suspect]

    def _stats(subset):
        if not subset:
            return {"trades": 0, "wins": 0, "win_rate": 0, "total_pnl": 0, "avg_roi": 0}
        wins = [t for t in subset if (t.pnl_usd or 0) > 0]
        rois = [t.roi_pct for t in subset if t.roi_pct is not None]
        pnls = [t.pnl_usd for t in subset if t.pnl_usd is not None]
        return {
            "trades": len(subset),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(subset) * 100, 1),
            "total_pnl": round(sum(pnls), 4),
            "avg_roi": round(sum(rois) / len(rois), 2) if rois else 0,
        }

    # Trail counterfactual: -6% and -8%
    cf_6 = replay_v2.cf_trailing_stop_from_mfe(post_fix, 0.06)
    cf_8 = replay_v2.cf_trailing_stop_from_mfe(post_fix, 0.08)
    cf_6_stats = replay_v2.scenario_stats(cf_6)
    cf_8_stats = replay_v2.scenario_stats(cf_8)

    # Close reason distribution
    reason_dist: dict[str, int] = {}
    for t in post_fix:
        r = t.close_reason or "unknown"
        reason_dist[r] = reason_dist.get(r, 0) + 1

    actual_stats = _stats(post_fix)
    return {
        "post_fix_trades": actual_stats["trades"],
        "win_rate": actual_stats["win_rate"],
        "total_pnl": actual_stats["total_pnl"],
        "avg_roi": actual_stats["avg_roi"],
        "real_price_trades": len(post_real),
        "real_price_win_rate": round(len([t for t in post_real if (t.pnl_usd or 0) > 0]) / len(post_real) * 100, 1) if post_real else 0,
        "trail_counterfactual": {
            "actual_pnl": actual_stats["total_pnl"],
            "trail_6pct_pnl": round(cf_6_stats.get("total_pnl", 0), 4),
            "trail_8pct_pnl": round(cf_8_stats.get("total_pnl", 0), 4),
            "trail_6pct_win_rate": round(cf_6_stats.get("win_rate", 0), 1),
            "trail_8pct_win_rate": round(cf_8_stats.get("win_rate", 0), 1),
        },
        "close_reason_distribution": dict(sorted(reason_dist.items(), key=lambda kv: -kv[1])),
        "last_updated_ts": time.time(),
    }


def _compute_exitlab_summary(n_trades: int) -> dict:
    """Heavy CPU work — always called via run_in_executor."""
    import exit_lab
    hist = _read_history_locked()
    closes = [ev for ev in hist if ev.get("type") == "trade_close"]
    closes.sort(key=lambda ev: float(ev.get("ts") or 0))
    closes = closes[-n_trades:]

    _MFE_CAP = 500.0
    envelopes = []
    for c in closes:
        mfe = min(abs(float(c.get("mfe_pct") or 0)), _MFE_CAP)
        mae = float(c.get("mae_pct") or 0)
        if mfe <= 0:
            continue
        basis = float(c.get("realized_basis_usd") or c.get("size_usd") or 0)
        qty = float(c.get("qty_closed") or 0)
        if qty <= 0 or basis <= 0:
            continue
        entry_px = basis / qty
        exit_px = float(c.get("exit_price") or 0) or entry_px
        high_px = entry_px * (1 + mfe / 100.0)
        low_px = entry_px * (1 + mae / 100.0)
        fees = float(c.get("fees_usd") or 0)
        size = float(c.get("size_usd") or basis)
        envelopes.append(exit_lab.TradeEnvelope(
            mint=c.get("mint", "?"), entry_px=entry_px, exit_px=exit_px,
            high_px=high_px, low_px=low_px, mfe_pct=mfe, mae_pct=mae,
            roi_pct=float(c.get("roi_pct") or 0), hold_s=float(c.get("hold_time_seconds") or 0),
            fees_usd=fees, size_usd=size, close_reason=c.get("close_reason", "unknown"),
            pnl_usd=float(c.get("pnl_usd") or 0),
        ))

    if not envelopes:
        return {"strategies": [], "best_strategy": None, "trade_count": 0, "last_updated_ts": time.time()}

    avg_size = sum(e.size_usd for e in envelopes) / len(envelopes)
    strategies = []
    for name, fn in exit_lab.STRATEGIES:
        sr = exit_lab.StrategyResult(name=name)
        sr._avg_size = avg_size
        for env in envelopes:
            sr.rois.append(fn(env))
        strategies.append({
            "name": sr.name,
            "trades": sr.n,
            "wins": sr.wins,
            "win_rate": round(sr.win_rate, 1),
            "avg_roi": round(sr.avg_roi, 2),
            "total_pnl": round(sr.total_pnl_approx, 4),
        })

    best = max(strategies, key=lambda s: s["total_pnl"])
    return {
        "strategies": strategies,
        "best_strategy": best["name"],
        "trade_count": len(envelopes),
        "last_updated_ts": time.time(),
    }


@app.get("/api/replay_summary")
async def api_replay_summary():
    """Replay analysis summary — heavy computation off event loop, mtime-cached."""
    if not HISTORY_FILE.exists():
        return {"error": "history.json not found", "post_fix_trades": 0}
    mtime = os.path.getmtime(HISTORY_FILE)
    if _replay_cache["data"] is not None and _replay_cache["mtime"] == mtime:
        return _replay_cache["data"]
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _compute_replay_summary)
    _replay_cache["mtime"] = mtime
    _replay_cache["data"] = result
    return result


@app.get("/api/exit_lab_summary")
async def api_exit_lab_summary(n: int = 30):
    """Exit strategy lab — heavy computation off event loop, mtime-cached."""
    n = max(5, min(n, 500))
    if not HISTORY_FILE.exists():
        return {"error": "history.json not found", "strategies": [], "trade_count": 0}
    mtime = os.path.getmtime(HISTORY_FILE)
    if _exitlab_cache["data"] is not None and _exitlab_cache["mtime"] == mtime and _exitlab_cache["n"] == n:
        return _exitlab_cache["data"]
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _compute_exitlab_summary, n)
    _exitlab_cache["mtime"] = mtime
    _exitlab_cache["n"] = n
    _exitlab_cache["data"] = result
    return result


@app.websocket("/ws")
async def ws_feed(ws: WebSocket):
    await ws.accept()
    CLIENTS.add(ws)
    CLIENT_LAST_PONG[ws] = time.time()

    async def _ping_loop():
        while True:
            await asyncio.sleep(WS_PING_INTERVAL_SEC)
            try:
                await ws.send_text(json.dumps({"type": "ping", "ts": int(time.time())}))
            except Exception:
                return

    ping_task = asyncio.create_task(_ping_loop())
    try:
        await ws.send_text(json.dumps(enrich_hud_state(read_state())))
        while True:
            msg = await ws.receive_text()
            CLIENT_LAST_PONG[ws] = time.time()
            # Silently absorb pongs; any other payload is treated as a liveness signal.
            try:
                obj = json.loads(msg) if msg else None
            except Exception:
                obj = None
            if isinstance(obj, dict) and obj.get("type") == "pong":
                continue
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        ping_task.cancel()
        CLIENTS.discard(ws)
        CLIENT_LAST_PONG.pop(ws, None)
