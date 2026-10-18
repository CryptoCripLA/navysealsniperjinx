"""
live_canary.py - Live Canary Mode Execution for Sniperbot

SAFETY CONTROLS:
- TRADING_ENABLED env must be "true" to allow any new entries
- SOLANA_PRIVATE_KEY required (base58)
- Fixed trade size (CANARY_TRADE_SIZE_USD)
- Max concurrent positions (CANARY_MAX_CONCURRENT)
- Max daily loss cap (CANARY_MAX_DAILY_LOSS_USD)
- Failsafe stop percentage

This module handles:
1. Wallet management (keypair from env)
2. Jupiter swap quotes and transaction building
3. Jito bundle submission
4. Safety limit enforcement
5. Real balance tracking
"""

import os
import time
import json
import base64
import asyncio
import threading
import logging
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass

import aiohttp

# Solana imports - only available in venv
try:
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    SOLANA_AVAILABLE = True
except ImportError:
    SOLANA_AVAILABLE = False
    Keypair = None
    Pubkey = None

log = logging.getLogger("live_canary")

# ==================== CANARY CONFIGURATION ====================

CANARY_TRADE_SIZE_USD = float(os.getenv("CANARY_TRADE_SIZE_USD", "5.0"))
CANARY_MAX_CONCURRENT = int(os.getenv("CANARY_MAX_CONCURRENT", "2"))
CANARY_MAX_DAILY_LOSS_USD = float(os.getenv("CANARY_MAX_DAILY_LOSS_USD", "25.0"))
CANARY_FAILSAFE_STOP_PCT = float(os.getenv("CANARY_FAILSAFE_STOP_PCT", "0.50"))
CANARY_MAX_PRICE_IMPACT_PCT = float(os.getenv("CANARY_MAX_PRICE_IMPACT_PCT", "0.15"))

# Live TX Submission flag — must be explicitly enabled before any on-chain TX can occur.
# This is a low-level arming step: enables the TX infrastructure.
# Trading (buys) requires BOTH this AND TRADING_ENABLED.
# Sell exits are NOT gated by this flag — positions can always be exited.
LIVE_TX_SUBMISSION_ENABLED: bool = False  # Off by default; set via operator control only

# Trading enabled flag - controls new entry buys only.
# MUST have LIVE_TX_SUBMISSION_ENABLED=True first. Set via operator control only.
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").strip().lower() == "true"

# Jupiter API — lite-api.jup.ag/swap/v1 is the public unauthenticated endpoint (no key required).
# For paid access to api.jup.ag/v6, set JUPITER_API_KEY and override JUPITER_QUOTE_API/JUPITER_SWAP_API.
JUPITER_QUOTE_API               = os.getenv("JUPITER_QUOTE_API", "https://lite-api.jup.ag/swap/v1/quote")
JUPITER_SWAP_API                = os.getenv("JUPITER_SWAP_API",  "https://lite-api.jup.ag/swap/v1/swap")
JUPITER_API_KEY                 = os.getenv("JUPITER_API_KEY", "").strip()
# Rate limiting — conservative defaults for free-tier key (1 RPS with 1.2 s gap)
JUPITER_MAX_RPS                 = float(os.getenv("JUPITER_MAX_RPS", "1"))
JUPITER_MIN_REQUEST_INTERVAL_MS = float(os.getenv("JUPITER_MIN_REQUEST_INTERVAL_MS", "1200"))
JUPITER_QUOTE_TIMEOUT_SECONDS   = float(os.getenv("JUPITER_QUOTE_TIMEOUT_SECONDS", "10"))
JUPITER_SWAP_TIMEOUT_SECONDS    = float(os.getenv("JUPITER_SWAP_TIMEOUT_SECONDS", "15"))
JUPITER_RETRY_ATTEMPTS          = int(os.getenv("JUPITER_RETRY_ATTEMPTS", "2"))
JUPITER_RETRY_DELAY_SECONDS     = float(os.getenv("JUPITER_RETRY_DELAY_SECONDS", "2.0"))

# ==================== JUPITER RATE LIMITER (PRIORITY-AWARE) ====================
#
# Priority queue: entry buys and stop-loss sells preempt mark-refresh quotes.
# _jupiter_priority_waiters are served before _jupiter_normal_waiters.

_jupiter_lock: Optional[asyncio.Lock] = None
_jupiter_last_request_ts: float = 0.0
_shared_http_session: Optional[aiohttp.ClientSession] = None
_jupiter_priority_waiters: list = []    # asyncio.Event list — urgent (buy/sell)
_jupiter_normal_waiters: list = []      # asyncio.Event list — mark refresh
# Count of priority callers currently waiting for the lock.
# Normal (mark-refresh) callers yield while this is > 0, ensuring entry buys
# always jump ahead of queued mark refreshes without a complex queue structure.
_jupiter_priority_waiting: int = 0
_jupiter_stats: Dict[str, Any] = {
    "requests_session": 0,
    "rate_limited_count": 0,
    "last_error": None,
    "last_quote_latency_ms": None,
    "throttle_active": False,
    "last_request_ts": 0.0,
    "priority_preemptions": 0,
}


def _get_jupiter_lock() -> asyncio.Lock:
    """Lazy-init the asyncio.Lock (must be created inside a running event loop)."""
    global _jupiter_lock
    if _jupiter_lock is None:
        _jupiter_lock = asyncio.Lock()
    return _jupiter_lock


async def _jupiter_acquire(priority: bool) -> None:
    """Acquire the Jupiter rate-limit gate with priority preemption.

    priority=True  (entry buys, stop-loss sells): registers as a priority waiter
                   so any concurrently queued normal callers yield first.
    priority=False (mark refreshes): spins on a short sleep while any priority
                   caller is waiting, then acquires the lock normally.

    Both paths still serialize through the single lock — the rate-limit throttle
    inside the lock correctly enforces 1 req / JUPITER_MIN_REQUEST_INTERVAL_MS.
    """
    global _jupiter_priority_waiting
    lock = _get_jupiter_lock()
    if priority:
        _jupiter_priority_waiting += 1
        try:
            await lock.acquire()
        finally:
            # We're no longer waiting (now holding or cancelled).
            _jupiter_priority_waiting -= 1
            if _jupiter_priority_waiting < 0:
                _jupiter_priority_waiting = 0
    else:
        # Yield to any priority callers waiting in the queue.
        while _jupiter_priority_waiting > 0:
            await asyncio.sleep(0.02)
        await lock.acquire()


def _jupiter_release() -> None:
    """Release the Jupiter gate lock."""
    _get_jupiter_lock().release()


async def _jupiter_throttle() -> None:
    """Enforce minimum inter-request gap between Jupiter API calls."""
    global _jupiter_last_request_ts
    min_gap = JUPITER_MIN_REQUEST_INTERVAL_MS / 1000.0
    now = time.monotonic()
    gap = now - _jupiter_last_request_ts
    if gap < min_gap:
        wait = min_gap - gap
        _jupiter_stats["throttle_active"] = True
        log.debug("[JUPITER] throttling %.0f ms", wait * 1000)
        await asyncio.sleep(wait)
    _jupiter_stats["throttle_active"] = False
    _jupiter_last_request_ts = time.monotonic()
    _jupiter_stats["requests_session"] += 1
    _jupiter_stats["last_request_ts"] = time.time()


def get_jupiter_telemetry() -> Dict[str, Any]:
    """Return Jupiter rate-limiter telemetry for the HUD."""
    base = JUPITER_QUOTE_API
    if base.endswith("/quote"):
        base = base[:-6]
    return {
        "api_base": base,
        "api_key_present": bool(JUPITER_API_KEY),
        "max_rps": JUPITER_MAX_RPS,
        "min_interval_ms": JUPITER_MIN_REQUEST_INTERVAL_MS,
        "retry_attempts": JUPITER_RETRY_ATTEMPTS,
        "requests_session": _jupiter_stats["requests_session"],
        "rate_limited_count": _jupiter_stats["rate_limited_count"],
        "last_error": _jupiter_stats["last_error"],
        "last_quote_latency_ms": _jupiter_stats["last_quote_latency_ms"],
        "throttle_active": _jupiter_stats["throttle_active"],
    }


# Jito Block Engine
JITO_BLOCK_ENGINE_URL = os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf/api/v1/bundles")
# Dynamic Jito tip configuration
JITO_TIP_LAMPORTS         = int(os.getenv("JITO_TIP_LAMPORTS", "20000"))
# JITO_DYNAMIC_TIP_ENABLED: canonical name. Also checks legacy JITO_TIP_DYNAMIC for backward compat.
JITO_DYNAMIC_TIP_ENABLED  = (
    os.getenv("JITO_DYNAMIC_TIP_ENABLED", os.getenv("JITO_TIP_DYNAMIC", "true")).strip().lower() == "true"
)
JITO_TIP_DYNAMIC          = JITO_DYNAMIC_TIP_ENABLED  # backward-compat alias
JITO_TIP_MIN_LAMPORTS     = int(os.getenv("JITO_TIP_MIN_LAMPORTS", "10000"))
JITO_TIP_MAX_LAMPORTS     = int(os.getenv("JITO_TIP_MAX_LAMPORTS", "150000"))
JITO_TIP_HIGH_LAMPORTS    = int(os.getenv("JITO_TIP_HIGH_LAMPORTS", "50000"))   # used for early-entry window
JITO_EARLY_ENTRY_TIP_AGE_MS = int(os.getenv("JITO_EARLY_ENTRY_TIP_AGE_MS", "5000"))  # entries < 5s old use high tip
JITO_TIP_FAIL_MULTIPLIER  = float(os.getenv("JITO_TIP_FAIL_MULTIPLIER", "1.5"))
JITO_ENABLED              = os.getenv("JITO_ENABLED", "true").strip().lower() == "true"

# Rolling window for tracking bundle/tx failure rate (used to boost tip on congestion)
_jito_recent_outcomes: list[tuple[float, bool]] = []  # (timestamp, success)
_JITO_OUTCOME_WINDOW_S = 120  # 2-minute rolling window

# Last tip telemetry — exposed via get_canary_hud_data() for HUD display
_last_jito_tip_lamports: int = JITO_TIP_LAMPORTS
_last_jito_tip_mode: str = "base"       # "base" | "high_early_entry" | "congestion_boost" | "flat"
_last_jito_tip_ts: float = 0.0


def _record_jito_outcome(success: bool) -> None:
    """Record a transaction submission outcome for congestion-aware tip scaling."""
    now = time.time()
    _jito_recent_outcomes.append((now, success))
    # Prune old entries outside the window
    cutoff = now - _JITO_OUTCOME_WINDOW_S
    while _jito_recent_outcomes and _jito_recent_outcomes[0][0] < cutoff:
        _jito_recent_outcomes.pop(0)


def _jito_recent_fail_rate() -> float:
    """Return fraction of recent transactions that failed (0.0–1.0)."""
    now = time.time()
    cutoff = now - _JITO_OUTCOME_WINDOW_S
    recent = [s for ts, s in _jito_recent_outcomes if ts >= cutoff]
    if not recent:
        return 0.0
    return sum(1 for s in recent if not s) / len(recent)


def compute_jito_tip(trade_size_usd: float = 0.0, candidate_age_ms: Optional[float] = None) -> int:
    """Compute a dynamic Jito tip based on entry age, position size, and network congestion.

    Tiers (highest-priority first):
      1. Early entry (candidate_age_ms <= JITO_EARLY_ENTRY_TIP_AGE_MS): JITO_TIP_HIGH_LAMPORTS
      2. Size-scaled base: 25k-75k for large trades
      3. Congestion boost: multiplied by JITO_TIP_FAIL_MULTIPLIER when fail_rate > 0.3
      4. Flat mode (JITO_DYNAMIC_TIP_ENABLED=false): always JITO_TIP_LAMPORTS

    Set JITO_DYNAMIC_TIP_ENABLED=false to always return JITO_TIP_LAMPORTS.
    """
    global _last_jito_tip_lamports, _last_jito_tip_mode, _last_jito_tip_ts

    if not JITO_DYNAMIC_TIP_ENABLED:
        _last_jito_tip_lamports = JITO_TIP_LAMPORTS
        _last_jito_tip_mode = "flat"
        _last_jito_tip_ts = time.time()
        return JITO_TIP_LAMPORTS

    mode = "base"
    # Tier 1: early-entry window — use elevated tip for freshest candidates
    if candidate_age_ms is not None and candidate_age_ms <= JITO_EARLY_ENTRY_TIP_AGE_MS:
        base = JITO_TIP_HIGH_LAMPORTS
        mode = "high_early_entry"
    # Tier 2: size-scaled base
    elif trade_size_usd >= 50:
        base = 75_000
    elif trade_size_usd >= 20:
        base = 40_000
    elif trade_size_usd >= 10:
        base = 25_000
    else:
        base = JITO_TIP_LAMPORTS  # default for small / unknown sizes

    # Tier 3: congestion boost on top of current base
    fail_rate = _jito_recent_fail_rate()
    if fail_rate > 0.3:
        base = int(base * JITO_TIP_FAIL_MULTIPLIER)
        if mode == "base":
            mode = "congestion_boost"

    tip = max(JITO_TIP_MIN_LAMPORTS, min(JITO_TIP_MAX_LAMPORTS, base))
    log.info(
        "[JITO_TIP] tip=%d mode=%s trade_size_usd=%.2f candidate_age_ms=%s fail_rate=%.2f",
        tip, mode, trade_size_usd,
        f"{candidate_age_ms:.0f}" if candidate_age_ms is not None else "n/a",
        fail_rate,
    )
    _last_jito_tip_lamports = tip
    _last_jito_tip_mode = mode
    _last_jito_tip_ts = time.time()
    return tip

# Solana constants
WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

# ==================== CANARY STATE ====================

@dataclass
class CanaryState:
    """Tracks canary-specific state for safety limits."""
    daily_loss_usd: float = 0.0
    daily_trades: int = 0
    daily_reset_ts: int = 0
    last_wallet_balance_sol: float = 0.0
    last_wallet_balance_ts: int = 0
    total_live_trades: int = 0
    total_live_pnl_usd: float = 0.0

_canary_state = CanaryState()
_canary_rehydrated = False

# Jito tips paid per-mint, accumulated across all legs (buy + every TP/sell) of an
# open position. Drained by record_canary_trade() at close so tips are booked into
# realized PnL. Keyed by mint; lamports.
_mint_jito_tips_lamports: Dict[str, int] = {}


def record_jito_tip_paid(mint: Optional[str], tip_lamports: int) -> None:
    """Accumulate a Jito tip against the owning mint so close-time PnL nets it out.

    Called by send_transaction_jito_first after a bundle is accepted.
    Safe to call with mint=None (unknown owner) — tip is ignored; caller-side
    telemetry still records _last_jito_tip_lamports.
    """
    if not mint or tip_lamports <= 0:
        return
    _mint_jito_tips_lamports[mint] = _mint_jito_tips_lamports.get(mint, 0) + int(tip_lamports)


def get_accumulated_tip_lamports(mint: str) -> int:
    return int(_mint_jito_tips_lamports.get(mint, 0))


def get_accumulated_tip_usd(mint: str, sol_price_usd: float) -> float:
    if sol_price_usd <= 0:
        return 0.0
    return (get_accumulated_tip_lamports(mint) / LAMPORTS_PER_SOL) * sol_price_usd


def clear_accumulated_tips(mint: str) -> None:
    _mint_jito_tips_lamports.pop(mint, None)

def _rehydrate_daily_loss():
    """Rehydrate daily_loss_usd from history.json on first use after restart.

    Sums abs(pnl_usd) for today's UTC trade_close events where pnl_usd < 0.
    This prevents the daily cap from resetting to 0 on bot restart.
    """
    global _canary_rehydrated
    if _canary_rehydrated:
        return
    _canary_rehydrated = True
    from pathlib import Path
    history_file = Path(__file__).parent / "history.json"
    if not history_file.exists():
        return
    try:
        raw = json.loads(history_file.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return
        now = int(time.time())
        day_start = now - (now % 86400)
        daily_loss = 0.0
        daily_trades = 0
        for ev in raw:
            if ev.get("type") != "trade_close":
                continue
            ts = float(ev.get("ts") or 0)
            if ts < day_start:
                continue
            daily_trades += 1
            pnl = float(ev.get("pnl_usd") or 0)
            if pnl < 0:
                daily_loss += abs(pnl)
        if daily_loss > 0 or daily_trades > 0:
            _canary_state.daily_loss_usd = max(_canary_state.daily_loss_usd, daily_loss)
            _canary_state.daily_trades = max(_canary_state.daily_trades, daily_trades)
            _canary_state.daily_reset_ts = day_start
            log.info(
                "[CANARY_REHYDRATE] daily_loss_usd=$%.2f daily_trades=%d from history.json (today UTC)",
                daily_loss, daily_trades,
            )
    except Exception as e:
        log.warning("[CANARY_REHYDRATE] failed to read history.json: %s", e)

def get_canary_state() -> CanaryState:
    """Get current canary state, resetting daily counters if needed."""
    global _canary_state
    now = int(time.time())
    day_start = now - (now % 86400)  # UTC day boundary

    if _canary_state.daily_reset_ts < day_start:
        _canary_state.daily_loss_usd = 0.0
        _canary_state.daily_trades = 0
        _canary_state.daily_reset_ts = day_start

    return _canary_state

def record_canary_trade(
    pnl_usd: float,
    jito_tip_usd: float = 0.0,
    mint: Optional[str] = None,
    mode: str = "live",
):
    """Record a completed trade for daily-loss tracking.

    Deducts accumulated Jito tips from the reported PnL so daily_loss_usd and
    total_live_pnl_usd reflect the net wallet impact, not gross Jupiter PnL.
    If mint is provided, drains _mint_jito_tips_lamports for that mint.

    mode="paper" routes loss into the halt counter (daily_loss_usd) only —
    lifetime live totals are not mutated. Since paper and live are mutually
    exclusive execution modes, a single daily counter enforces the same cap
    without cross-contamination of lifetime stats.
    """
    _rehydrate_daily_loss()
    state = get_canary_state()
    net_pnl = float(pnl_usd) - float(jito_tip_usd or 0.0)
    state.daily_trades += 1
    if mode == "live":
        state.total_live_trades += 1
        state.total_live_pnl_usd += net_pnl
    if net_pnl < 0:
        state.daily_loss_usd += abs(net_pnl)
    if mint:
        clear_accumulated_tips(mint)
    log.info(
        "[CANARY_TRADE] mode=%s pnl_gross=$%.4f jito_tip=$%.4f pnl_net=$%.4f daily_loss=$%.2f",
        mode, pnl_usd, jito_tip_usd or 0.0, net_pnl, state.daily_loss_usd,
    )

# ==================== WALLET MANAGEMENT ====================

_cached_keypair: Optional[Keypair] = None
_keypair_load_lock = threading.Lock()


def _get_private_key() -> str:
    """Retrieve the Solana private key, preferring OS keychain over env var.

    Lookup order:
      1. macOS Keychain (security find-generic-password)
      2. Linux secret-tool (secret-tool lookup)
      3. SOLANA_PRIVATE_KEY / PRIVATE_KEY env var (with WARNING)

    Raises RuntimeError if no key found anywhere — bot must not start without a key.
    """
    import platform
    import subprocess

    # ── 1. Try OS keychain ────────────────────────────────────────────
    _os = platform.system()
    if _os == "Darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "solana-sniper", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                log.info("Private key loaded from macOS Keychain")
                return result.stdout.strip()
        except Exception as e:
            log.warning("macOS Keychain read failed: %s — falling back to env", e)
    elif _os == "Linux":
        try:
            result = subprocess.run(
                ["secret-tool", "lookup", "service", "solana-sniper"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                log.info("Private key loaded from Linux secret-tool")
                return result.stdout.strip()
        except Exception as e:
            log.warning("Linux secret-tool read failed: %s — falling back to env", e)

    # ── 2. Fallback to env var with WARNING ───────────────────────────
    private_key = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not private_key:
        private_key = os.getenv("PRIVATE_KEY", "").strip()

    if private_key:
        log.warning("SECURITY_WARNING: private key loaded from env, move to keychain")
        return private_key

    # ── 3. No key anywhere — refuse to start ──────────────────────────
    raise RuntimeError("SOLANA_PRIVATE_KEY not found in keychain or environment — cannot start")


def get_keypair() -> Optional[Keypair]:
    """Load keypair via _get_private_key() (keychain → env fallback).

    P6.4: lock guards against two concurrent callers both running
    `_get_private_key` → `Keypair.from_bytes` during cold start. The private
    key bytes are copied into a local bytearray and zeroized after construction
    so the decoded secret does not linger in a Python bytes object for GC.
    """
    global _cached_keypair

    if not SOLANA_AVAILABLE:
        log.error("Solana libraries not available - cannot create keypair")
        return None

    if _cached_keypair is not None:
        return _cached_keypair

    with _keypair_load_lock:
        if _cached_keypair is not None:
            return _cached_keypair

        try:
            private_key = _get_private_key()
        except RuntimeError as e:
            log.error("%s", e)
            return None

        secret_buf: Optional[bytearray] = None
        try:
            import base58
            secret_buf = bytearray(base58.b58decode(private_key))
            _cached_keypair = Keypair.from_bytes(bytes(secret_buf))
            log.info(f"Loaded wallet: {_cached_keypair.pubkey()}")
            return _cached_keypair
        except Exception as e:
            log.error(f"Failed to load keypair: {e}")
            return None
        finally:
            if secret_buf is not None:
                for i in range(len(secret_buf)):
                    secret_buf[i] = 0

def get_wallet_pubkey() -> Optional[str]:
    """Get wallet public key as string."""
    kp = get_keypair()
    if kp:
        return str(kp.pubkey())
    return None

async def _http_session() -> aiohttp.ClientSession:
    global _shared_http_session
    if _shared_http_session is None or _shared_http_session.closed:
        timeout = aiohttp.ClientTimeout(total=15.0, connect=5.0, sock_connect=5.0, sock_read=15.0)
        connector = aiohttp.TCPConnector(limit=64, ttl_dns_cache=300)
        _shared_http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    return _shared_http_session

async def get_wallet_balance_sol() -> Optional[float]:
    """Fetch current SOL balance from RPC."""
    kp = get_keypair()
    if not kp:
        return None
    
    rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    
    try:
        session = await _http_session()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [str(kp.pubkey())]
        }
        async with session.post(rpc_url, json=payload, timeout=10) as resp:
            data = await resp.json()
            if "result" in data and "value" in data["result"]:
                lamports = data["result"]["value"]
                balance = lamports / LAMPORTS_PER_SOL
                
                # Update cached state
                state = get_canary_state()
                state.last_wallet_balance_sol = balance
                state.last_wallet_balance_ts = int(time.time())
                
                return balance
    except Exception as e:
        log.error(f"Failed to fetch wallet balance: {e}")
    
    return None

# ==================== SAFETY CHECKS ====================

def can_open_new_position(current_open_count: int) -> Tuple[bool, str]:
    """
    Check if a new position can be opened based on safety limits.
    Returns (allowed, reason).
    """
    # Master kill switch
    if not TRADING_ENABLED:
        return False, "TRADING_ENABLED=false"
    
    # Solana libraries check
    if not SOLANA_AVAILABLE:
        return False, "solana_libraries_unavailable"
    
    # Keypair check
    if not get_keypair():
        return False, "no_wallet_configured"
    
    # Concurrent position limit
    if current_open_count >= CANARY_MAX_CONCURRENT:
        return False, f"max_concurrent_reached({current_open_count}/{CANARY_MAX_CONCURRENT})"
    
    # Daily loss limit
    state = get_canary_state()
    if state.daily_loss_usd >= CANARY_MAX_DAILY_LOSS_USD:
        return False, f"daily_loss_limit_reached(${state.daily_loss_usd:.2f}/${CANARY_MAX_DAILY_LOSS_USD:.2f})"
    
    return True, "ok"

def get_canary_trade_size_usd() -> float:
    """Get the fixed canary trade size."""
    return CANARY_TRADE_SIZE_USD

# ==================== JUPITER MARK PRICE CACHE ====================

# Short-lived cache for mark-price quotes (open position price updates).
# Prevents N concurrent open positions from each burning 1 RPS every refresh cycle.
MARK_PRICE_CACHE_TTL_MS = int(os.getenv("MARK_PRICE_CACHE_TTL_MS", "500"))
_mark_price_cache: Dict[str, tuple] = {}  # mint → (price_usd, timestamp_monotonic)


def get_cached_mark_price(mint: str) -> Optional[float]:
    """Return a cached mark price if it's fresh enough, else None."""
    entry = _mark_price_cache.get(mint)
    if entry is None:
        return None
    cached_price, cached_ts = entry
    age_ms = (time.monotonic() - cached_ts) * 1000
    if age_ms <= MARK_PRICE_CACHE_TTL_MS:
        return cached_price
    return None


def set_cached_mark_price(mint: str, price_usd: float) -> None:
    """Store a mark price in the short-lived cache."""
    _mark_price_cache[mint] = (price_usd, time.monotonic())


def invalidate_mark_price_cache(mint: str) -> None:
    """Remove a mint from the mark price cache (e.g. on position close)."""
    _mark_price_cache.pop(mint, None)


# ==================== JUPITER SWAP ====================

async def get_jupiter_quote(
    input_mint: str,
    output_mint: str,
    amount: int,  # in smallest units (lamports for SOL)
    slippage_bps: int = 100,  # 1%
    priority: bool = False,  # True for entry buys / stop-loss sells
    skip_throttle: bool = False,  # True for execution quotes: skip 1200ms inter-request sleep
) -> Optional[Dict[str, Any]]:
    """Get a swap quote from Jupiter with rate limiting and retry on 429.

    priority=True: entry buys and emergency sells skip ahead of mark-refresh
    requests in the Jupiter queue, reducing stop-loss and entry latency.
    skip_throttle=True: skip inter-request sleep for execution quotes (quote+swap are
    one atomic op; throttling between them wastes ~1200ms per trade for no benefit).
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage_bps),
        "onlyDirectRoutes": "false",
        "asLegacyTransaction": "false",
    }
    headers = {}
    if JUPITER_API_KEY:
        headers["x-api-key"] = JUPITER_API_KEY

    await _jupiter_acquire(priority=priority)
    try:
        for attempt in range(max(1, JUPITER_RETRY_ATTEMPTS)):
            if skip_throttle:
                # Skip the inter-request sleep but still record this as a request
                # so subsequent callers respect the rate-limit interval.
                global _jupiter_last_request_ts
                _jupiter_last_request_ts = time.monotonic()
                _jupiter_stats["requests_session"] += 1
                _jupiter_stats["last_request_ts"] = time.time()
                _jupiter_stats["throttle_active"] = False
            else:
                await _jupiter_throttle()
            t0 = time.monotonic()
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(
                        JUPITER_QUOTE_API,
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=JUPITER_QUOTE_TIMEOUT_SECONDS),
                    ) as resp:
                        latency_ms = (time.monotonic() - t0) * 1000
                        _jupiter_stats["last_quote_latency_ms"] = round(latency_ms, 1)
                        if resp.status == 200:
                            _jupiter_stats["last_error"] = None
                            return await resp.json()
                        elif resp.status == 429:
                            body = await resp.text()
                            _jupiter_stats["rate_limited_count"] += 1
                            _jupiter_stats["last_error"] = f"429 rate_limited (attempt {attempt + 1})"
                            log.warning("[JUPITER] quote rate-limited 429 attempt=%d: %s", attempt + 1, body[:120])
                            if attempt + 1 < JUPITER_RETRY_ATTEMPTS:
                                await asyncio.sleep(JUPITER_RETRY_DELAY_SECONDS * (attempt + 1))
                        else:
                            body = await resp.text()
                            _jupiter_stats["last_error"] = f"HTTP {resp.status}"
                            log.error("[JUPITER] quote failed: %s - %s", resp.status, body[:200])
                            return None
            except Exception as e:
                _jupiter_stats["last_error"] = str(e)[:80]
                log.error("[JUPITER] quote error: %s", e)
                return None
    finally:
        _jupiter_release()

    return None


async def get_jupiter_swap_transaction(
    quote: Dict[str, Any],
    user_pubkey: str,
    wrap_unwrap_sol: bool = True,
    fee_account: Optional[str] = None,
    priority: bool = False,
    skip_throttle: bool = False,
) -> Optional[str]:
    """Get a swap transaction from Jupiter with rate limiting and retry on 429.

    skip_throttle=True: skip the inter-request sleep (use when this call
    immediately follows a get_jupiter_quote in the same execution flow —
    quote and swap are one atomic operation, throttling between them wastes
    ~1200ms per trade for no rate-limit benefit since they hit different
    endpoints). Still updates _jupiter_last_request_ts so subsequent
    callers (mark refreshes) wait the correct interval.
    """
    payload = {
        "quoteResponse": quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": wrap_unwrap_sol,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    if fee_account:
        payload["feeAccount"] = fee_account

    headers = {}
    if JUPITER_API_KEY:
        headers["x-api-key"] = JUPITER_API_KEY

    await _jupiter_acquire(priority=priority)
    try:
        for attempt in range(max(1, JUPITER_RETRY_ATTEMPTS)):
            if skip_throttle:
                # Skip the inter-request sleep but still record this as a request
                # so subsequent callers respect the rate-limit interval.
                global _jupiter_last_request_ts
                _jupiter_last_request_ts = time.monotonic()
                _jupiter_stats["requests_session"] += 1
                _jupiter_stats["last_request_ts"] = time.time()
                _jupiter_stats["throttle_active"] = False
            else:
                await _jupiter_throttle()
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.post(
                        JUPITER_SWAP_API,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=JUPITER_SWAP_TIMEOUT_SECONDS),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            _jupiter_stats["last_error"] = None
                            return data.get("swapTransaction")
                        elif resp.status == 429:
                            body = await resp.text()
                            _jupiter_stats["rate_limited_count"] += 1
                            _jupiter_stats["last_error"] = f"swap 429 rate_limited (attempt {attempt + 1})"
                            log.warning("[JUPITER] swap rate-limited 429 attempt=%d: %s", attempt + 1, body[:120])
                            if attempt + 1 < JUPITER_RETRY_ATTEMPTS:
                                await asyncio.sleep(JUPITER_RETRY_DELAY_SECONDS * (attempt + 1))
                        else:
                            body = await resp.text()
                            _jupiter_stats["last_error"] = f"swap HTTP {resp.status}"
                            log.error("[JUPITER] swap failed: %s - %s", resp.status, body[:200])
                            return None
            except Exception as e:
                _jupiter_stats["last_error"] = str(e)[:80]
                log.error("[JUPITER] swap error: %s", e)
                return None
    finally:
        _jupiter_release()

    return None

# ==================== TRANSACTION VERIFICATION ====================

# How long to wait before first poll. Helius/Jito typically confirms within ~1s of
# submission; 0.8s starts polling early. If the TX isn't indexed yet we receive null
# and keep retrying — polling early is safe, it costs one cheap HTTP round-trip.
TX_VERIFY_INITIAL_DELAY_SECONDS = float(os.getenv("TX_VERIFY_INITIAL_DELAY_SECONDS", "0.4"))
# Interval between polling attempts. 0.8s cadence on Helius catches confirmation
# within one extra cycle after the chain confirms (~1-2s total from submission).
TX_VERIFY_POLL_INTERVAL_SECONDS = float(os.getenv("TX_VERIFY_POLL_INTERVAL_SECONDS", "0.4"))
# Maximum polls before giving up. 20 × 0.8s + 0.8s initial ≈ 16.8s max window.
# Halved from the prior 32s; still generous for slow blocks or RPC lag.
TX_VERIFY_MAX_ATTEMPTS = int(os.getenv("TX_VERIFY_MAX_ATTEMPTS", "20"))


async def verify_transaction_outcome(sig: str) -> tuple[str, Optional[dict]]:
    """
    Poll for on-chain confirmation of a submitted transaction.

    Execution states:
      "confirmed_success"      — transaction landed, meta.err is null  (C)
      "confirmed_failure"      — transaction landed, meta.err non-null (D)
      "verification_timeout"   — could not confirm within timeout window (E)

    Only "confirmed_success" should be treated as a true execution success.
    "confirmed_failure" and "verification_timeout" must both be treated as
    NOT successful — no position should be opened or closed from them.
    """
    rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")

    await asyncio.sleep(TX_VERIFY_INITIAL_DELAY_SECONDS)

    session = await _http_session()
    for attempt in range(TX_VERIFY_MAX_ATTEMPTS):
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignatureStatuses",
                "params": [
                    [sig],
                    {"searchTransactionHistory": True},
                ],
            }
            async with session.post(
                rpc_url, json=payload,
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as resp:
                    if resp.status != 200:
                        log.warning(
                            "[TX_VERIFY] HTTP %d sig=%.20s attempt=%d",
                            resp.status, sig, attempt + 1,
                        )
                        await asyncio.sleep(TX_VERIFY_POLL_INTERVAL_SECONDS)
                        continue
                    data = await resp.json()
                    statuses = ((data.get("result") or {}).get("value") or [])
                    status = statuses[0] if statuses else None

                    if status is None:
                        # Not yet indexed — TX may still be propagating
                        log.debug("[TX_VERIFY] sig=%.20s not found (attempt=%d)", sig, attempt + 1)
                        await asyncio.sleep(TX_VERIFY_POLL_INTERVAL_SECONDS)
                        continue

                    confirmation_status = status.get("confirmationStatus")
                    err = status.get("err")

                    if confirmation_status in ("confirmed", "finalized"):
                        if err is None:
                            log.info(
                                "[TX_VERIFY] confirmed_success sig=%.20s status=%s",
                                sig, confirmation_status,
                            )
                            return "confirmed_success", None
                        else:
                            log.error(
                                "[TX_VERIFY] confirmed_failure sig=%.20s status=%s err=%s",
                                sig, confirmation_status, err,
                            )
                            return "confirmed_failure", err
                    else:
                        # "processed" but not yet confirmed — keep polling
                        log.debug(
                            "[TX_VERIFY] sig=%.20s status=%s (attempt=%d), waiting",
                            sig, confirmation_status, attempt + 1,
                        )
                        await asyncio.sleep(TX_VERIFY_POLL_INTERVAL_SECONDS)

        except Exception as e:
            log.warning("[TX_VERIFY] error sig=%.20s attempt=%d: %s", sig, attempt + 1, e)
            await asyncio.sleep(TX_VERIFY_POLL_INTERVAL_SECONDS)

    log.error(
        "[TX_VERIFY] verification_timeout sig=%.20s after %d attempts",
        sig, TX_VERIFY_MAX_ATTEMPTS,
    )
    return "verification_timeout", None


async def get_token_balance(mint: str) -> Optional[int]:
    """Return the wallet's total holdings of `mint` in raw base units, or None on RPC failure.

    Sums balances across all token accounts the wallet owns for `mint` so stale/duplicate
    associated token accounts are not missed. Used by classify_verification_timeout to
    decide whether an uncertain sell actually moved tokens off the wallet.
    """
    kp = get_keypair()
    if not kp:
        return None
    rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    try:
        session = await _http_session()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                str(kp.pubkey()),
                {"mint": mint},
                {"encoding": "jsonParsed"},
            ],
        }
        async with session.post(
            rpc_url, json=payload,
            timeout=aiohttp.ClientTimeout(total=5.0),
        ) as resp:
            if resp.status != 200:
                log.warning("[TOKEN_BAL] HTTP %d mint=%s", resp.status, mint)
                return None
            data = await resp.json()
            accounts = ((data.get("result") or {}).get("value") or [])
            total_raw = 0
            for acc in accounts:
                info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                tok = info.get("tokenAmount") or {}
                amount = tok.get("amount")
                if amount:
                    try:
                        total_raw += int(amount)
                    except (TypeError, ValueError):
                        continue
            return total_raw
    except Exception as e:
        log.warning("[TOKEN_BAL] fetch failed mint=%s: %s", mint, e)
        return None


async def classify_verification_timeout(sig: str, mint: Optional[str] = None) -> tuple[str, Optional[dict]]:
    """One-shot post-timeout classifier.
    Re-check signature status immediately; if mint is provided, also inspect direct wallet holdings
    to avoid blind duplicate sell attempts after an uncertain confirmation window.
    Returns: confirmed_success | confirmed_failure | uncertain
    """
    outcome, err = await verify_transaction_outcome(sig)
    if outcome != "verification_timeout":
        return outcome, err
    if mint:
        try:
            bal = await get_token_balance(mint)
            if bal is not None and int(bal) == 0:
                log.warning("[TX_VERIFY] timeout classified as confirmed_success via zero wallet balance sig=%.20s mint=%s", sig, mint)
                return "confirmed_success", None
        except Exception as e:
            log.warning("[TX_VERIFY] timeout wallet-balance recheck failed sig=%.20s mint=%s err=%s", sig, mint, e)
    return "uncertain", None


# ==================== JITO BUNDLE SUBMISSION ====================

async def submit_jito_bundle(
    signed_tx_b58: str,
    tip_lamports: int = 0,
) -> Optional[str]:
    """Submit a single signed transaction to Jito block engine as a bundle.

    Returns the Jito bundle_id (UUID) on acceptance, None on rejection. The bundle_id
    is NOT a transaction signature — the tx sig is extracted separately from the
    signed transaction object by the caller.
    """
    bundle_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendBundle",
        "params": [[signed_tx_b58]],
    }
    try:
        session = await _http_session()
        async with session.post(
            JITO_BLOCK_ENGINE_URL,
            json=bundle_payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            if "result" in data:
                bundle_id = data["result"]
                log.info(f"[JITO] Bundle accepted: bundle_id={bundle_id} tip={tip_lamports}")
                _record_jito_outcome(True)
                return bundle_id
            err = data.get("error", {})
            log.error(f"[JITO] Bundle rejected: {err}")
    except Exception as e:
        log.error(f"[JITO] Bundle submission error: {e}")
    _record_jito_outcome(False)
    return None

async def send_transaction_rpc(
    signed_tx_base64: str,
    skip_preflight: bool = True,
) -> Optional[str]:
    """Send a signed transaction via RPC. Returns signature if successful."""
    rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
    
    try:
        session = await _http_session()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                signed_tx_base64,
                {
                    "encoding": "base64",
                    "skipPreflight": skip_preflight,
                    "preflightCommitment": "confirmed",
                    "maxRetries": 3,
                }
            ]
        }
        for attempt in range(2):
            async with session.post(rpc_url, json=payload, timeout=20) as resp:
                data = await resp.json()
                if "result" in data:
                    _record_jito_outcome(True)
                    return data["result"]
                elif "error" in data:
                    log.error(f"Transaction failed attempt={attempt+1}: {data['error']}")
            await asyncio.sleep(0.25)
    except Exception as e:
        log.error(f"Send transaction error: {e}")
    _record_jito_outcome(False)
    return None

async def send_transaction_jito_first(
    signed_tx_bytes: bytes,
    signed_tx_b64: str,
    trade_size_usd: float = 0.0,
    candidate_age_ms: Optional[float] = None,
    signed_tx: "Optional[VersionedTransaction]" = None,
    mint: Optional[str] = None,
) -> Optional[str]:
    """Try Jito bundle first, fall back to standard RPC sendTransaction.

    Returns the transaction signature (base58) on success, None on failure.
    candidate_age_ms: age of the signal candidate in ms — used for early-entry tip tier.
    signed_tx: the already-signed VersionedTransaction; REQUIRED when JITO_ENABLED so the
               caller-side tx signature can be read from signed_tx.signatures[0]
               (Jito returns only a bundle_id; the on-chain sig must come from the signer).
    mint: the owning position's mint; when the Jito tip lands, it is accumulated against
          this mint so close-time PnL nets it out via record_canary_trade.
    """
    if JITO_ENABLED:
        try:
            import base58 as b58
            signed_tx_b58 = b58.b58encode(signed_tx_bytes).decode("ascii")
            tip = compute_jito_tip(trade_size_usd, candidate_age_ms=candidate_age_ms)
            bundle_id = await submit_jito_bundle(signed_tx_b58, tip_lamports=tip)
            if bundle_id:
                # The bundle_id is NOT a tx sig. Read the canonical signature from the
                # signed VersionedTransaction — signatures[0] is the fee-payer/primary sig
                # and matches what lands on-chain.
                if signed_tx is None:
                    log.error(
                        "[JITO] signed_tx not supplied — cannot extract tx sig; "
                        "bundle_id=%s will NOT be verifiable. Falling back to RPC.",
                        bundle_id,
                    )
                else:
                    try:
                        sig_b58 = str(signed_tx.signatures[0])
                    except Exception as e:
                        log.error("[JITO] failed to read signed_tx.signatures[0]: %s", e)
                        sig_b58 = None
                    if sig_b58:
                        # Tip successfully submitted; book it against the mint so close
                        # accounting nets it from realized PnL.
                        record_jito_tip_paid(mint, tip)
                        log.info(f"[JITO] Bundle accepted bundle_id={bundle_id} tx_sig={sig_b58}")
                        return sig_b58
            log.warning("[JITO] Bundle rejected or sig-unreadable, falling back to RPC sendTransaction")
        except Exception as e:
            log.warning(f"[JITO] Error, falling back to RPC: {e}")
    return await send_transaction_rpc(signed_tx_b64)


# ==================== LIVE BUY EXECUTION ====================

async def execute_live_buy(
    mint: str,
    size_usd: float,
    sol_price_usd: float,
    max_slippage_pct: float = 0.03,
    verify_immediately: bool = True,
) -> Tuple[bool, Optional[str], Optional[str], Optional[float]]:
    """
    Execute a live buy order.

    Returns: (success, tx_signature, error_message, actual_fill_px)
      actual_fill_px — USD per token derived from Jupiter quote outAmount:
                       (lamports_spent / 1e9 * sol_usd) / (out_tokens / 1e6)
                       None on failure or when outAmount is unavailable.
    """
    if not SOLANA_AVAILABLE:
        return False, None, "solana_libraries_unavailable", None

    kp = get_keypair()
    if not kp:
        return False, None, "no_wallet_keypair", None

    # Calculate SOL amount to spend
    sol_amount = size_usd / sol_price_usd
    lamports = int(sol_amount * LAMPORTS_PER_SOL)

    # P6.2: rent-exemption guard. Wrapping SOL + opening the output ATA requires
    # ~0.003 SOL of rent-exempt lamports beyond the trade amount. Skipping this
    # check lets Jupiter submit a tx that fails at sim-time, wasting a slot and
    # blacklisting us from the next attempt.
    _RENT_RESERVE_LAMPORTS = int(0.003 * LAMPORTS_PER_SOL)
    _required_lamports = lamports + _RENT_RESERVE_LAMPORTS
    _state_for_balance = get_canary_state()
    _balance_ts = int(_state_for_balance.last_wallet_balance_ts or 0)
    if int(time.time()) - _balance_ts > 30:
        await get_wallet_balance_sol()
    _bal_sol = float(_state_for_balance.last_wallet_balance_sol or 0.0)
    _bal_lamports = int(_bal_sol * LAMPORTS_PER_SOL)
    if _bal_lamports < _required_lamports:
        log.warning(
            "[LIVE_BUY] rent_exempt_shortfall mint=%s have=%d need=%d (trade=%d + rent=%d) — aborting",
            mint, _bal_lamports, _required_lamports, lamports, _RENT_RESERVE_LAMPORTS,
        )
        return False, None, "insufficient_sol_for_rent", None

    _t_buy_start = time.monotonic()
    log.info(f"[LIVE_BUY] mint={mint} size_usd=${size_usd:.2f} sol_amount={sol_amount:.6f} lamports={lamports}")

    # ── Per-stage timing instrumentation ──────────────────────────────────────
    # quote_ms / swap_tx_ms / submit_ms / verify_ms are logged at WARNING level
    # so they appear in logs_sim.out without requiring DEBUG mode.
    # These feed the HUD's execution_breakdown field once wired.

    # 1. Get Jupiter quote (SOL -> Token) — skip throttle: execution is time-critical;
    # mark-refresh may have run recently and throttling wastes 0-1200ms per trade.
    _t0 = time.monotonic()
    quote = await get_jupiter_quote(
        input_mint=WSOL_MINT,
        output_mint=mint,
        amount=lamports,
        slippage_bps=int(max_slippage_pct * 10000),
        priority=True,
        skip_throttle=True,
    )
    _quote_ms = round((time.monotonic() - _t0) * 1000, 1)

    if not quote:
        log.warning("[LIVE_BUY] quote_ms=%.0f FAILED mint=%s", _quote_ms, mint)
        return False, None, "jupiter_quote_failed", None

    expected_out = int(quote.get("outAmount", 0))
    in_amount = int(quote.get("inAmount", lamports))
    if expected_out == 0:
        log.warning("[LIVE_BUY] outAmount=0 mint=%s — pool not indexed; aborting", mint)
        return False, None, "jupiter_quote_out_zero", None

    # P6.1: price-impact gate. Jupiter returns priceImpactPct as a decimal
    # string ("0.08" = 8%). Abort if the pool is too thin for our size.
    try:
        _pi_raw = quote.get("priceImpactPct")
        _pi = float(_pi_raw) if _pi_raw is not None else 0.0
    except (TypeError, ValueError):
        _pi = 0.0
    if _pi > CANARY_MAX_PRICE_IMPACT_PCT:
        log.warning(
            "[LIVE_BUY] price_impact_veto mint=%s impact=%.2f%% threshold=%.2f%% — aborting",
            mint, _pi * 100, CANARY_MAX_PRICE_IMPACT_PCT * 100,
        )
        return False, None, "price_impact_too_high", None
    log.info("[LIVE_BUY] Jupiter quote: in=%d lamports -> out=%d tokens impact=%.2f%%",
             in_amount, expected_out, _pi * 100)

    # ── Compute actual fill price from Jupiter quote ──────────────────────────
    # price = (SOL spent × USD/SOL) / tokens received
    # Uses 1e6 token decimals (pump.fun SPL standard).
    # This is the quote's guaranteed-minimum fill; actual fill ≥ this price.
    _TOKEN_DECIMALS = 6
    _actual_fill_px: Optional[float] = None
    if expected_out > 0 and in_amount > 0 and sol_price_usd > 0:
        _tokens_received = expected_out / (10 ** _TOKEN_DECIMALS)
        _sol_spent_usd = (in_amount / 1e9) * sol_price_usd
        _actual_fill_px = _sol_spent_usd / _tokens_received
        log.info("[LIVE_BUY] fill_px=%.10g (in=%d out=%d sol_usd=%.2f)",
                 _actual_fill_px, in_amount, expected_out, sol_price_usd)

    # 2. Get swap transaction — skip throttle: quote + swap are one atomic op,
    # throttling between them wastes ~1200ms per trade for no rate-limit benefit.
    _t0 = time.monotonic()
    swap_tx_b64 = await get_jupiter_swap_transaction(
        quote=quote,
        user_pubkey=str(kp.pubkey()),
        priority=True,
        skip_throttle=True,
    )
    _swap_tx_ms = round((time.monotonic() - _t0) * 1000, 1)

    if not swap_tx_b64:
        log.warning("[LIVE_BUY] swap_tx_ms=%.0f FAILED mint=%s", _swap_tx_ms, mint)
        return False, None, "jupiter_swap_tx_failed", None

    # 3. Deserialize, sign, and send
    try:
        _t0 = time.monotonic()
        tx_bytes = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)

        # Sign the transaction
        signed_tx = VersionedTransaction(tx.message, [kp])
        signed_tx_bytes = bytes(signed_tx)
        signed_tx_b64 = base64.b64encode(signed_tx_bytes).decode('utf-8')
        _sign_ms = round((time.monotonic() - _t0) * 1000, 1)

        # Send transaction (Jito bundle first, RPC fallback). Pass signed_tx so
        # the Jito path can read tx.signatures[0] (Jito only returns bundle_id).
        # Pass mint so the Jito tip is booked against the position for net-PnL accounting.
        _t0 = time.monotonic()
        sig = await send_transaction_jito_first(
            signed_tx_bytes, signed_tx_b64,
            trade_size_usd=size_usd,
            signed_tx=signed_tx,
            mint=mint,
        )
        _submit_ms = round((time.monotonic() - _t0) * 1000, 1)

        if not sig:
            log.warning("[LIVE_BUY] submit_ms=%.0f FAILED (no sig) mint=%s", _submit_ms, mint)
            return False, None, "transaction_send_failed", None

        if not verify_immediately:
            # Background verification path — return immediately after sig received.
            # Caller is responsible for spawning verify_transaction_outcome as a background task.
            # Ghost-recon and quoteability-guard handle stale/failed positions within 35-300s.
            _total_ms = round((time.monotonic() - _t_buy_start) * 1000, 1)
            log.warning(
                "[LIVE_BUY] timing mint=%s quote_ms=%.0f swap_tx_ms=%.0f sign_ms=%.0f "
                "submit_ms=%.0f verify_ms=DEFERRED total_ms=%.0f sig=%s fill_px=%s",
                mint, _quote_ms, _swap_tx_ms, _sign_ms, _submit_ms, _total_ms, sig,
                f"{_actual_fill_px:.10g}" if _actual_fill_px else "unavailable",
            )
            return True, sig, None, _actual_fill_px

        # ── EXECUTION TRUTH: verify on-chain outcome before declaring success ──
        # submission accepted ≠ trade success.
        # Only confirmed_success (meta.err == null) is a real execution.
        log.info(f"[LIVE_BUY] Transaction sent: {sig} — verifying on-chain outcome")
        _t0 = time.monotonic()
        outcome, on_chain_err = await verify_transaction_outcome(sig)
        _verify_ms = round((time.monotonic() - _t0) * 1000, 1)
        _total_ms = round((time.monotonic() - _t_buy_start) * 1000, 1)

        log.warning(
            "[LIVE_BUY] timing mint=%s quote_ms=%.0f swap_tx_ms=%.0f sign_ms=%.0f "
            "submit_ms=%.0f verify_ms=%.0f total_ms=%.0f outcome=%s",
            mint, _quote_ms, _swap_tx_ms, _sign_ms, _submit_ms, _verify_ms, _total_ms, outcome,
        )

        if outcome == "confirmed_success":
            log.info(f"[LIVE_BUY] confirmed_success sig={sig}")
            return True, sig, None, _actual_fill_px
        elif outcome == "confirmed_failure":
            err_str = str(on_chain_err)[:80]
            log.error(f"[LIVE_BUY] confirmed_failure sig={sig} err={err_str}")
            # Return sig so caller can audit/log the failed TX
            return False, sig, f"tx_failed_on_chain:{err_str}", None
        else:  # verification_timeout
            log.error(f"[LIVE_BUY] verification_timeout sig={sig} — treating as uncertain, NOT success")
            return False, sig, "tx_verification_timeout", None
        # ── END EXECUTION TRUTH ────────────────────────────────────────────────

    except Exception as e:
        log.error(f"[LIVE_BUY] Transaction signing/sending failed: {e}")
        return False, None, f"tx_error:{str(e)[:50]}", None

# ==================== LIVE SELL EXECUTION ====================

async def execute_live_sell(
    mint: str,
    token_amount: int,  # in token smallest units
    sol_price_usd: float,
    max_slippage_pct: float = 0.03,
) -> Tuple[bool, Optional[str], Optional[str], Optional[int]]:
    """
    Execute a live sell order (token -> SOL).

    Returns: (success, tx_signature, error_message, out_lamports)
      out_lamports — SOL received expressed in lamports per Jupiter quote outAmount;
                     None on failure or when the quote is unavailable.
    """
    if not SOLANA_AVAILABLE:
        return False, None, "solana_libraries_unavailable", None

    kp = get_keypair()
    if not kp:
        return False, None, "no_wallet_keypair", None

    log.info(f"[LIVE_SELL] mint={mint} token_amount={token_amount}")

    # 1. Get Jupiter quote (Token -> SOL) — skip throttle: execution is time-critical
    quote = await get_jupiter_quote(
        input_mint=mint,
        output_mint=WSOL_MINT,
        amount=token_amount,
        slippage_bps=int(max_slippage_pct * 10000),
        priority=True,
        skip_throttle=True,
    )

    if not quote:
        return False, None, "jupiter_quote_failed", None

    expected_sol = int(quote.get("outAmount", 0))
    log.info(f"[LIVE_SELL] Jupiter quote: {token_amount} tokens -> {expected_sol} lamports")

    # 2. Get swap transaction — priority=True so sell path preempts mark refreshes.
    # skip_throttle=True: sell quote just ran; no need to wait 1200ms again.
    swap_tx_b64 = await get_jupiter_swap_transaction(
        quote=quote,
        user_pubkey=str(kp.pubkey()),
        priority=True,
        skip_throttle=True,
    )

    if not swap_tx_b64:
        return False, None, "jupiter_swap_tx_failed", None

    # 3. Deserialize, sign, and send
    try:
        tx_bytes = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)

        signed_tx = VersionedTransaction(tx.message, [kp])
        signed_tx_bytes = bytes(signed_tx)
        signed_tx_b64 = base64.b64encode(signed_tx_bytes).decode('utf-8')

        # Estimate USD value for dynamic tip sizing
        sell_usd_est = (expected_sol / LAMPORTS_PER_SOL) * sol_price_usd if sol_price_usd > 0 else 0.0
        sig = await send_transaction_jito_first(
            signed_tx_bytes, signed_tx_b64,
            trade_size_usd=sell_usd_est,
            signed_tx=signed_tx,
            mint=mint,
        )

        if not sig:
            return False, None, "transaction_send_failed", None

        # ── EXECUTION TRUTH: verify on-chain outcome before declaring success ──
        log.info(f"[LIVE_SELL] Transaction sent: {sig} — verifying on-chain outcome")
        outcome, on_chain_err = await verify_transaction_outcome(sig)

        if outcome == "confirmed_success":
            log.info(f"[LIVE_SELL] confirmed_success sig={sig}")
            # out_lamports from Jupiter quote is the best approximation of proceeds.
            # Actual received SOL may differ slightly due to slippage within bounds.
            return True, sig, None, expected_sol
        elif outcome == "confirmed_failure":
            err_str = str(on_chain_err)[:80]
            log.error(f"[LIVE_SELL] confirmed_failure sig={sig} err={err_str}")
            return False, sig, f"tx_failed_on_chain:{err_str}", None
        else:  # verification_timeout
            refined_outcome, refined_err = await classify_verification_timeout(sig, mint=mint)
            if refined_outcome == "confirmed_success":
                log.warning(f"[LIVE_SELL] verification_timeout refined_to_success sig={sig}")
                return True, sig, None, expected_sol
            if refined_outcome == "confirmed_failure":
                err_str = str(refined_err)[:80]
                log.error(f"[LIVE_SELL] verification_timeout refined_to_failure sig={sig} err={err_str}")
                return False, sig, f"tx_failed_on_chain:{err_str}", None
            log.error(f"[LIVE_SELL] verification_timeout sig={sig} — treating as uncertain, NOT success")
            return False, sig, "tx_verification_timeout", None
        # ── END EXECUTION TRUTH ────────────────────────────────────────────────

    except Exception as e:
        log.error(f"[LIVE_SELL] Transaction signing/sending failed: {e}")
        return False, None, f"tx_error:{str(e)[:50]}", None

# ==================== HUD DATA ====================

def get_canary_hud_data() -> Dict[str, Any]:
    """Get canary-specific data for HUD display."""
    state = get_canary_state()
    kp = get_keypair()
    
    return {
        "canary_mode": True,
        "trading_enabled": TRADING_ENABLED,
        "solana_available": SOLANA_AVAILABLE,
        "wallet_configured": kp is not None,
        "wallet_pubkey": str(kp.pubkey()) if kp else None,
        "wallet_balance_sol": state.last_wallet_balance_sol,
        "wallet_balance_ts": state.last_wallet_balance_ts,
        "trade_size_usd": CANARY_TRADE_SIZE_USD,
        "max_concurrent": CANARY_MAX_CONCURRENT,
        "max_daily_loss_usd": CANARY_MAX_DAILY_LOSS_USD,
        "daily_loss_usd": state.daily_loss_usd,
        "daily_loss_remaining_usd": max(0, CANARY_MAX_DAILY_LOSS_USD - state.daily_loss_usd),
        "daily_trades": state.daily_trades,
        "total_live_trades": state.total_live_trades,
        "total_live_pnl_usd": state.total_live_pnl_usd,
        "failsafe_stop_pct": CANARY_FAILSAFE_STOP_PCT,
        # Jito tip telemetry — current runtime values for HUD display
        "jito_tip_last": _last_jito_tip_lamports,
        "jito_tip_mode": _last_jito_tip_mode,
        "jito_tip_base": JITO_TIP_LAMPORTS,
        "jito_tip_high": JITO_TIP_HIGH_LAMPORTS,
        "jito_dynamic_enabled": JITO_DYNAMIC_TIP_ENABLED,
        "jito_early_entry_age_ms": JITO_EARLY_ENTRY_TIP_AGE_MS,
        "jito_tip_last_ts": _last_jito_tip_ts,
    }

# ==================== OPERATOR CONTROLS ====================

def enable_live_tx_submission():
    """Arm live TX submission. Required before trading can be enabled.
    Only arms the TX infrastructure — does NOT enable buys/entries."""
    global LIVE_TX_SUBMISSION_ENABLED
    LIVE_TX_SUBMISSION_ENABLED = True
    log.warning("[OPERATOR] LIVE TX SUBMISSION ENABLED — TX infrastructure armed. Trading (buys) still requires enable_trading().")

def disable_live_tx_submission():
    """Disarm live TX submission. Also disables trading as a safety cascade."""
    global LIVE_TX_SUBMISSION_ENABLED, TRADING_ENABLED
    LIVE_TX_SUBMISSION_ENABLED = False
    TRADING_ENABLED = False  # safety cascade: TX off → trading off
    log.warning("[OPERATOR] LIVE TX SUBMISSION DISABLED — safety cascade: TRADING also disabled.")

def is_live_tx_submission_enabled() -> bool:
    """Return True if the TX infrastructure is armed."""
    return LIVE_TX_SUBMISSION_ENABLED

def enable_trading():
    """Enable new entry buys. Requires LIVE_TX_SUBMISSION_ENABLED first.
    Raises RuntimeError if TX submission is not armed."""
    global TRADING_ENABLED
    if not LIVE_TX_SUBMISSION_ENABLED:
        log.error("[OPERATOR] enable_trading BLOCKED — Live TX Submission must be enabled first.")
        raise RuntimeError("live_tx_submission_not_enabled")
    TRADING_ENABLED = True
    log.warning("[OPERATOR] TRADING ENABLED (buys active) via operator control.")

def disable_trading():
    """Disable new entry buys immediately. Does not affect TX submission or exits."""
    global TRADING_ENABLED
    TRADING_ENABLED = False
    log.warning("[OPERATOR] TRADING DISABLED (buys blocked) via operator control.")

def is_trading_enabled() -> bool:
    """Check if trading is currently enabled."""
    return TRADING_ENABLED
