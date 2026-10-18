"""
models.py - Trade data models for sniperbot.

Contains the core dataclasses used throughout the pipeline:
  PaperTrade      - Represents an open or closed position (paper or live)
  ExecutionIntent - Describes a trade entry to be executed
  ExecutionResult - Result of an execution attempt
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class PaperTrade:
    mint: str
    ts: float
    size_usd: float
    reason: str
    entry_px: float = 1.0
    current_px: float = 1.0
    current_px_source: str = "entry"
    last_mark_ts: float = 0.0
    high_px: float = 1.0
    low_px: float = 1.0
    stop_px: float = 1.0
    closed: bool = False
    realized_pnl: float = 0.0
    qty_total: float = 1.0
    qty_remaining: float = 1.0
    fees_usd: float = 0.0
    stage1_done: bool = False
    stage2_done: bool = False
    stage3_done: bool = False
    status: str = "open"
    entry_ts: float = 0.0
    entry_price: float = 1.0
    last_price: float = 1.0
    last_price_update_at: float = 0.0
    mark_price_source: str = "fallback"
    mark_notional_usd_used: float = 0.0
    mark_notional_size_used: float = 0.0
    mark_confidence_level: str = "LOW"
    mark_vs_full_liq_diff_pct: Optional[float] = None
    mark_stale: bool = False
    mark_staleness_seconds: float = 0.0
    unrealized_roi_pct: float = 0.0
    realized_roi_pct: float = 0.0
    exit_ts: float = 0.0
    exit_price: float = 0.0
    close_reason: str = ""
    qty_closed: float = 0.0
    proceeds_usd: float = 0.0
    cost_basis_total_usd: float = 0.0
    remaining_basis_usd: float = 0.0
    realized_basis_usd: float = 0.0
    realized_fees_usd: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    close_recorded: bool = False
    restored_from_state_ts: float = 0.0
    restore_mark_age_seconds: float = 0.0
    fresh_mark_seen_since_restore: bool = False
    close_event_emitted_ts: float = 0.0
    canonical_close_emitted: bool = False
    stop_trigger_ts: float = 0.0
    sell_attempt_ts: float = 0.0
    sell_success_ts: float = 0.0
    first_executable_mark_ts: float = 0.0
    executable_mark_observations: int = 0
    total_mark_observations: int = 0         # ALL mark updates (jupiter + dexscreener + rpc)
    first_mark_ts: float = 0.0               # timestamp of first ANY-source mark update
    mark_source_sequence: str = ""            # comma-separated source trail e.g. "dexscreener,dexscreener,executable_notional_quote"
    prev_px: Optional[float] = None          # for rapid dump detection
    prev_px_ts: Optional[float] = None       # timestamp of prev_px sample
    # Forced exit tracking — prevents QUOTEABILITY_GUARD_EXIT / STALE_MARK exit loop
    forced_exit_unresolved: bool = False
    forced_exit_attempt_count: int = 0
    forced_exit_last_attempt_ts: float = 0.0
    # Set when a stop-exit sell TX times out verification; cleared by _sell_verify_poll.
    # While > 0, the main loop skips all exit logic for this position.
    sell_pending_verify_ts: float = 0.0
    # Position lifecycle state machine: OPEN | PARTIAL_EXIT | GHOST_PENDING | GHOST_CLOSE_PENDING | GHOST_UNRESOLVED | CLOSED | TERMINALIZED
    position_truth_state: str = "OPEN"
    # Set when buy verify sees wallet_raw=0 — wall-clock at which GHOST_PENDING began.
    # After GHOST_BUY_PENDING_RECHECK_SECONDS, _ghost_pending_recheck re-probes the wallet
    # and either clears back to OPEN (late-landed TX) or finalizes as GHOST_BUY_NO_TOKENS.
    ghost_pending_since: float = 0.0
    # P2-2 / P1-1: Wall-clock detection timestamp (seconds); 0 = unknown / pre-instrumentation
    detect_ts_wall: float = 0.0
    # 2-3: Causal identity threaded from signal → execution → close for replay matching
    decision_id: str = ""
    # HARD_CAP sell failure counter: ghost_close forced after 4 consecutive failures
    hcap_sell_fail_count: int = 0
    # True when entry_px was set to the 1e-12 sentinel (fill+dex+reference all unavailable).
    # All exit logic (TP/trail/HARD_CAP/RAPID_DUMP) must skip this trade until the first
    # executable mark arrives and rewrites entry_px to a real value.
    entry_px_sentinel: bool = False


@dataclass
class ExecutionIntent:
    decision_id: str
    mint: str
    size_usd: float
    max_slippage_pct: float
    max_cost_lamports: int
    ts_detected: float
    ts_decided: float
    priority_policy: str
    reference_price_usd: Optional[float] = None
    # True when reference_price_usd was confirmed by a real DEX source (DexScreener/Jupiter
    # pool price, drift guard fetch) rather than a theoretical estimate (graduation curve,
    # PUMP_CURVE constant).  Paper adapter uses it to skip the slippage inflation factor —
    # consistent with live mode which uses the Jupiter fill_px directly.
    reference_price_is_real: bool = False
    # Pre-entry quote cached here so live_adapter can reuse it instead of double-quoting.
    cached_quote: Optional[Dict[str, Any]] = None


@dataclass
class ExecutionResult:
    ok: bool
    mode: str
    tx_sig: Optional[str]
    cu_price: Optional[int]
    cu_limit: Optional[int]
    jito_tip_lamports: Optional[int]
    priority_tier: str
    cost_to_trade_usd: float
    error: Optional[str]
    latency_ms: float
    extra: Dict[str, Any]
