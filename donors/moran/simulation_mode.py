import os
import re
import json
import time
import math
import asyncio
import logging
import random
import signal
import urllib.parse
import hashlib
import fcntl
from collections import OrderedDict
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Optional, Any, List
from datetime import datetime, timezone

import aiohttp
import websockets
from dotenv import load_dotenv

from constants import (
    MAX_TEST_BUDGET_USD,
    STOP_LOSS,
    DEFAULT_DOWNSIDE_STOP_PCT,
    MAX_POSITION_LOSS_PCT,
    BREAKEVEN_TRIGGER_PCT,
    TRAIL_FROM_MFE_PCT,
    TP1_PCT, TP1_SELL,
    TP2_PCT, TP2_SELL,
    TP3_PCT, TP3_SELL,
)
from models import PaperTrade, ExecutionIntent, ExecutionResult

# override=True: .env always wins over any inherited shell environment variables.
# Without this, a shell with RPC_URL/RPC_URLS exported (e.g. from a prior session)
# silently bypasses the .env values and forces the process onto the wrong RPC endpoint.
load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ABSOLUTE_FAILSAFE_STOP_PCT = float(os.getenv("ABSOLUTE_FAILSAFE_STOP_PCT", "0.20"))
# Time-based stop tightening: narrows stop as position ages to prevent slow bleed
STOP_TIGHTEN_PHASE2_SECONDS = int(os.getenv("STOP_TIGHTEN_PHASE2_SECONDS", "120"))   # tighten at 2 min
STOP_TIGHTEN_PHASE2_PCT = float(os.getenv("STOP_TIGHTEN_PHASE2_PCT", "0.08"))         # to -8%
STOP_TIGHTEN_PHASE3_SECONDS = int(os.getenv("STOP_TIGHTEN_PHASE3_SECONDS", "300"))   # tighten at 5 min
STOP_TIGHTEN_PHASE3_PCT = float(os.getenv("STOP_TIGHTEN_PHASE3_PCT", "0.06"))         # to -6%
# Pre-entry Jupiter-quote sanity ratio. Kills fills that route into pools far
# above the T+30 reference_price. 1.3x was rejecting legitimate fresh pump.fun
# fills (observed 1.56-2.08x ratios on real pools). 2.0x admits normal walk-up
# while still blocking 3x+ rug pumps.
PRE_ENTRY_SURGE_RATIO_MAX = float(os.getenv("PRE_ENTRY_SURGE_RATIO_MAX", "2.0"))
# Rapid dump detection: exit immediately if price falls this fast
RAPID_DUMP_WINDOW_SECONDS = int(os.getenv("RAPID_DUMP_WINDOW_SECONDS", "15"))
RAPID_DUMP_PCT = float(os.getenv("RAPID_DUMP_PCT", "0.10"))  # 10% drop in 30s = exit
log = logging.getLogger("sim")

PUMPFUN_PROGRAM_ID = os.getenv("PUMPFUN_PROGRAM_ID", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
RAYDIUM_AUTHORITY = os.getenv("RAYDIUM_AUTHORITY", "")
RAYDIUM_AMM_V4_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM_PROGRAM_ID = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
PUMPSWAP_AMM_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"  # pump.fun native DEX (95%+ of migrations since Mar 2025)
# Comma-separated program IDs to subscribe to via logsSubscribe.
# Default: pump.fun only.  To also catch direct Raydium pool creations from
# other launchpads, add the CPMM program:
#   LOGS_MENTION_KEYS=6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P,CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C
# WARNING: subscribing to Raydium AMM v4 (675kPX...) is extremely noisy (millions of txs/day).
# CPMM is lower volume.  Each key opens a separate logsSubscribe on the same WebSocket.
LOGS_MENTION_KEYS = [x.strip() for x in os.getenv("LOGS_MENTION_KEYS", os.getenv("LOGS_MENTION_KEY", PUMPFUN_PROGRAM_ID)).split(",") if x.strip()]
# Keep single-key compat for code that reads LOGS_MENTION_KEY
LOGS_MENTION_KEY = LOGS_MENTION_KEYS[0] if LOGS_MENTION_KEYS else PUMPFUN_PROGRAM_ID
WS_URLS = [x.strip() for x in os.getenv("WS_URLS", os.getenv("WS_URL", "")).split(",") if x.strip()]
RPC_URLS = [x.strip() for x in os.getenv("RPC_URLS", os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")).split(",") if x.strip()]
RPC_URL = RPC_URLS[0] if RPC_URLS else "https://api.mainnet-beta.solana.com"


def _extract_helius_api_key() -> str:
    k = os.getenv("HELIUS_API_KEY", "").strip()
    if k:
        return k
    for src in [os.getenv("WS_URL", ""), os.getenv("RPC_URL", "")]:
        if not src:
            continue
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(src).query)
            v = (q.get("api-key") or [""])[0].strip()
            if v:
                return v
        except Exception:
            pass
    return ""


HELIUS_API_KEY = _extract_helius_api_key()
HELIUS_ENHANCED_TX_URL = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""

# Secondary/tertiary RPCs for read-only calls — offload Helius from non-critical reads.
# HELIUS_RPC_URL remains exclusive for tx submission, getTransaction verification,
# and anything called from live_canary.py.
SECONDARY_RPC_URL = os.getenv("SECONDARY_RPC_URL", "").strip()
TERTIARY_RPC_URL  = os.getenv("TERTIARY_RPC_URL",  "").strip()
_READ_RPC_POOL = [u for u in (HELIUS_RPC_URL, SECONDARY_RPC_URL, TERTIARY_RPC_URL) if u]
_read_rpc_counter = 0
# P3.6: per-endpoint throttle state. When a caller reports a 429 (or other
# rate-limit signal), _mark_read_rpc_throttled stamps the endpoint's cooldown
# deadline; _next_read_rpc_url skips any endpoint still in cooldown so we don't
# spin on a throttled host until every sibling in the pool is also throttled.
_READ_RPC_COOLDOWN_UNTIL: Dict[str, float] = {}
_READ_RPC_COOLDOWN_SECONDS: float = float(os.getenv("READ_RPC_COOLDOWN_SECONDS", "10.0"))

def _next_read_rpc_url() -> str:
    """Round-robin RPC endpoint for read-only calls (getAsset, getSignaturesForAddress,
    getTokenLargestAccounts). Returns "" only if no RPC is configured at all.
    Counter is monotonic — mod by pool length gives round-robin. Skips endpoints
    that are currently throttled (see _mark_read_rpc_throttled); if every endpoint
    is cooling down, returns the least-throttled (earliest cooldown-expiry) one
    rather than blocking so callers still make forward progress.
    """
    global _read_rpc_counter
    if not _READ_RPC_POOL:
        return ""
    now = time.time()
    for _ in range(len(_READ_RPC_POOL)):
        candidate = _READ_RPC_POOL[_read_rpc_counter % len(_READ_RPC_POOL)]
        _read_rpc_counter += 1
        if _READ_RPC_COOLDOWN_UNTIL.get(candidate, 0.0) <= now:
            return candidate
    # Everything cooling down — pick the one expiring soonest.
    return min(_READ_RPC_POOL, key=lambda u: _READ_RPC_COOLDOWN_UNTIL.get(u, 0.0))

def _mark_read_rpc_throttled(url: str, cooldown_seconds: Optional[float] = None) -> None:
    """P3.6: tag an endpoint as recently rate-limited. Subsequent calls to
    _next_read_rpc_url will skip this URL until `cooldown_seconds` have passed
    (default READ_RPC_COOLDOWN_SECONDS). Multiple 429s extend the cooldown
    exponentially up to 60s so repeat offenders back off further."""
    if not url:
        return
    cs = float(cooldown_seconds or _READ_RPC_COOLDOWN_SECONDS)
    now = time.time()
    prev_until = _READ_RPC_COOLDOWN_UNTIL.get(url, 0.0)
    # If we're already cooling down and hit again, double the cooldown up to 60s.
    if prev_until > now:
        remaining = prev_until - now
        cs = min(60.0, max(cs, remaining * 2.0))
    _READ_RPC_COOLDOWN_UNTIL[url] = now + cs
    log.warning("[RPC_POOL] marked endpoint throttled cooldown=%.1fs url=%s...", cs, url[:40])

QUOTE_MINT_IDS = {
    "So11111111111111111111111111111111111111112",  # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4fY5jY7R6puj949iFDUFx",  # USDT
}
WSOL_MINT = "So11111111111111111111111111111111111111112"
EVENT_CANDIDATE_DEDUPE_MAX = int(os.getenv("EVENT_CANDIDATE_DEDUPE_MAX", "50000"))
DUP_SKIP_ACTIVITY_COOLDOWN_SECONDS = int(os.getenv("DUP_SKIP_ACTIVITY_COOLDOWN_SECONDS", "60"))
MARK_PRICE_STALE_AFTER_SECONDS = int(os.getenv("MARK_PRICE_STALE_AFTER_SECONDS", "45"))
MARK_PRICE_RECENT_GRACE_SECONDS = int(os.getenv("MARK_PRICE_RECENT_GRACE_SECONDS", "90"))
DEPENDENCY_RETRY_MAX_AGE_MS = int(os.getenv("DEPENDENCY_RETRY_MAX_AGE_MS", "45000"))
GHOST_RECON_INITIAL_DELAY_SECONDS = int(os.getenv("GHOST_RECON_INITIAL_DELAY_SECONDS", "45"))
GHOST_RECON_INTERVAL_SECONDS = int(os.getenv("GHOST_RECON_INTERVAL_SECONDS", "30"))
GHOST_RECON_MIN_POSITION_AGE_SECONDS = int(os.getenv("GHOST_RECON_MIN_POSITION_AGE_SECONDS", "45"))
# P2.3: absolute upper bound on GHOST_PENDING — if wallet fetches keep failing, a
# position could sit indefinitely. After this age the periodic recon force-terminalizes
# regardless of FETCH_ERROR, treating uncertain wallet as "gave up, assume ghost".
MAX_GHOST_PENDING_SECONDS = int(os.getenv("MAX_GHOST_PENDING_SECONDS", "300"))
QUOTEABILITY_VALIDATION_WINDOW_SECONDS = int(os.getenv("QUOTEABILITY_VALIDATION_WINDOW_SECONDS", "60"))  # 60s: replay shows identical P&L to 35s with no additional losses; Raydium pools take 30-90s to index
QUOTEABILITY_REQUIRED_EXEC_MARKS = int(os.getenv("QUOTEABILITY_REQUIRED_EXEC_MARKS", "1"))
# Auto-exit: close a stale-mark position after this many seconds without any live price mark.
# Prevents positions that never receive a price feed from being stranded in stale_open_inventory.
# In live mode _sell_fraction -> _live_sell_gate: real sell if tokens present, ghost-close if not.
STALE_MARK_EXIT_TIMEOUT_SECONDS = int(os.getenv("STALE_MARK_EXIT_TIMEOUT_SECONDS", "45"))
TERMINAL_STALE_POSITION_AGE_SECONDS = int(os.getenv("TERMINAL_STALE_POSITION_AGE_SECONDS", str(48 * 3600)))
# Forced exit cooldown: minimum seconds between successive exit attempts on the same position.
# Without this, QUOTEABILITY_GUARD_EXIT hammers every 1s loop iteration.
FORCED_EXIT_RETRY_COOLDOWN_SECONDS = float(os.getenv("FORCED_EXIT_RETRY_COOLDOWN_SECONDS", "20.0"))
# After this many consecutive failed forced exit attempts, escalate to ghost terminalization.
FORCED_EXIT_MAX_ATTEMPTS = int(os.getenv("FORCED_EXIT_MAX_ATTEMPTS", "5"))
# Ghost auto-terminalize when unresolvable: position must be this old AND mark stale
GHOST_UNRESOLVED_AUTO_TERM_AGE_SECONDS = int(os.getenv("GHOST_UNRESOLVED_AUTO_TERM_AGE_SECONDS", "300"))
# Buy verify sees wallet_raw=0 at T+4s; hold in GHOST_PENDING for this long before finalizing
# GHOST_BUY_NO_TOKENS. Gives late-landing TXs time to settle while the stop/trail loop keeps monitoring.
GHOST_BUY_PENDING_RECHECK_SECONDS = int(os.getenv("GHOST_BUY_PENDING_RECHECK_SECONDS", "60"))
TERMINAL_STALE_POSITION_VALUE_USD = float(os.getenv("TERMINAL_STALE_POSITION_VALUE_USD", "10"))
# TTL exit: force-close positions held beyond MAX_POSITION_AGE_SECONDS unless
# current ROI exceeds TTL_EXIT_MIN_PROGRESS_PCT. Prevents indefinitely-held stuck positions.
TTL_EXIT_ENABLED = os.getenv("TTL_EXIT_ENABLED", "true").strip().lower() == "true"
MAX_POSITION_AGE_SECONDS = int(os.getenv("MAX_POSITION_AGE_SECONDS", "900"))
TTL_EXIT_MIN_PROGRESS_PCT = float(os.getenv("TTL_EXIT_MIN_PROGRESS_PCT", "5.0"))
MARK_DEBUG_MINT = os.getenv("MARK_DEBUG_MINT", "Atnow54m14oACTZviQLk24WADrUyti1Nergd5aU8pump").strip()
# Executable mark: price open positions using a shallow Jupiter sell-quote outAmount
# instead of a full-position liquidation quote. This preserves executable realism while
# avoiding catastrophic price-impact distortion on thin fresh pools.
EXECUTABLE_MARK_ENABLED = os.getenv("EXECUTABLE_MARK_ENABLED", "true").lower() == "true"
EXECUTABLE_MARK_SLIPPAGE_BPS = int(os.getenv("EXECUTABLE_MARK_SLIPPAGE_BPS", "500"))  # 5% — match buy side
EXECUTABLE_MARK_NOTIONAL_USD = float(os.getenv("EXECUTABLE_MARK_NOTIONAL_USD", "0.15"))
EXECUTABLE_MARK_POSITION_FRACTION = float(os.getenv("EXECUTABLE_MARK_POSITION_FRACTION", "0.02"))
EXECUTABLE_MARK_MIN_TOKENS = int(os.getenv("EXECUTABLE_MARK_MIN_TOKENS", "1"))
# Paper-mode confirmation delay: model the 1-3s on-chain settlement window so
# paper entries see the same price drift live entries absorb between submit
# and fill_px observation. Without this, paper buys lock in pre-execution
# reference price and never lose to entry-side adverse moves.
PAPER_CONFIRMATION_DELAY_SECONDS = float(os.getenv("PAPER_CONFIRMATION_DELAY_SECONDS", "2.0"))
PAPER_FRESH_BUY_QUOTE_ENABLED = os.getenv("PAPER_FRESH_BUY_QUOTE_ENABLED", "true").lower() == "true"
PAPER_FRESH_BUY_QUOTE_SLIPPAGE_BPS = int(os.getenv("PAPER_FRESH_BUY_QUOTE_SLIPPAGE_BPS", "500"))

# Instrumentation: stale-candidate thresholds (ms) for candidate_timing structure
DIAG_STALE_PARSE_MS = int(os.getenv("DIAG_STALE_PARSE_MS", "5000"))
DIAG_STALE_DECISION_MS = int(os.getenv("DIAG_STALE_DECISION_MS", "10000"))
DIAG_STALE_EXECUTION_MS = int(os.getenv("DIAG_STALE_EXECUTION_MS", "15000"))

CONTEXTUAL_MINT_PATTERNS = [
    re.compile(r"(?i)initialize2[^\n]*?mint\s*[:=]\s*([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"(?i)base[_\s-]?mint\s*[:=]\s*([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"(?i)quote[_\s-]?mint\s*[:=]\s*([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"(?i)token[_\s-]?mint\s*[:=]\s*([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"(?i)lp[_\s-]?mint\s*[:=]\s*([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"(?i)\bmint\b\s*[:=]\s*([1-9A-HJ-NP-Za-km-z]{32,44})"),
]

EXCLUDED_MINT_IDS = {
    "ComputeBudget111111111111111111111111111111",
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ",
    # Program IDs / authorities that can appear in logs and must never be treated as mints.
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun program
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6 program
    "675kPX9MHTjS2zt1qfr1NYHuzefHh8D6wL7N8qf3g1w",  # Raydium AMM v4 program
    "5Q544fKrFoe6tsEbJkQ9v9fQ6be8M8iXvP8bFzR1XkR",  # Raydium authority
    "So11111111111111111111111111111111111111112",  # Wrapped SOL quote token
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap AMM program (noise in logs)
    # Jito tip program: address ends in "pump" so it passes is_valid_pump_mint and
    # was entering the candidate pipeline as a false mint. Not a token — must be rejected at parse.
    "jitodontfront11111111111111111111111111pump",
    # Jito tip accounts (region-mapped); appear in swap/bundle logs and must never be treated as mints.
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",  # Jito tip NY
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",  # Jito tip Frankfurt
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",  # Jito tip Tokyo
    # Pump migrator Raydium authority (pool creator, not a mint)
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",
}


def is_excluded_mint(mint: str) -> bool:
    if not mint:
        return True
    m = str(mint).strip()

    # Ruthless prefix exclusion for known program-id families that poison lane-2.
    low = m.lower()
    if low.startswith("pamm") or low.startswith("jup") or low.startswith("rayd") or low.startswith("sysvar"):
        return True
    # Jito tip program / tip accounts: "jitodontfront..." ends in "pump" and slips past is_valid_pump_mint.
    if low.startswith("jitodontfront") or low.startswith("jito4"):
        return True

    dynamic_exclusions = {
        str(os.getenv("PUMPFUN_PROGRAM_ID", "")).strip(),
        str(os.getenv("RAYDIUM_AUTHORITY", "")).strip(),
        str(os.getenv("RAYDIUM_AMM_V4_PROGRAM_ID", "")).strip(),
        str(os.getenv("RAYDIUM_CPMM_PROGRAM_ID", "")).strip(),
        str(os.getenv("RAYDIUM_CPMM_AUTHORITY", "")).strip(),
    }
    dynamic_exclusions.discard("")
    return m in EXCLUDED_MINT_IDS or m in QUOTE_MINT_IDS or m in dynamic_exclusions


def is_fast_candidate_mint(mint: str) -> bool:
    if not mint:
        return False
    m = str(mint).strip()
    if len(m) < 32 or len(m) > 44:
        return False
    if "AAAA" in m:
        return False
    if is_excluded_mint(m):
        return False
    return True


def is_valid_pump_mint(mint: str) -> bool:
    if not is_fast_candidate_mint(mint):
        return False
    return str(mint).strip().endswith("pump")


def is_valid_dual_lane_mint(mint: str) -> bool:
    # Dual-lane: pump suffix OR fast candidate mint.
    if is_valid_pump_mint(mint):
        return True
    return is_fast_candidate_mint(mint)


def _extract_contextual_mint_from_line(line: str) -> Optional[str]:
    text = str(line)
    for pat in CONTEXTUAL_MINT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        cand = m.group(1).strip()
        if is_valid_dual_lane_mint(cand):
            return cand
    return None


def _extract_mint_from_candidate_logs(logs: list[str]) -> tuple[Optional[str], str]:
    if not logs:
        return None, "none"

    joined = "\n".join(str(x) for x in logs)

    for line in logs:
        cand = _extract_contextual_mint_from_line(line)
        if cand:
            return cand, "log_context"

    for line in logs:
        line_text = str(line)
        if line_text.startswith("Program data:") or line_text.startswith("Program return:"):
            continue
        if not any(hint in line_text for hint in (
            "InitializeMint", "CreatePool", "InitializeAccount", "MintTo", "mint", "baseMint", "quoteMint", "tokenMint"
        )):
            continue
        for token in re.findall(r"[1-9A-HJ-NP-Za-km-z]{32,44}", line_text):
            if is_valid_dual_lane_mint(token):
                return token, "log_hint"

    for token in re.findall(r"[1-9A-HJ-NP-Za-km-z]{32,44}", joined):
        if is_valid_pump_mint(token):
            return token, "log_token"

    return None, "none"


def _extract_mint_from_rpc_tx(tx: dict) -> tuple[Optional[str], str, str]:
    try:
        if not tx:
            return None, "none", "no_balances"

        meta = tx.get("meta") or {}
        post = meta.get("postTokenBalances") or []
        pre = meta.get("preTokenBalances") or []
        balances = post + pre
        seen = set()
        excluded_seen = False

        for row in balances:
            mint = row.get("mint")
            if not mint or mint in seen:
                continue
            seen.add(mint)
            if not is_valid_dual_lane_mint(mint):
                excluded_seen = True
                continue
            return mint, "tx_balances", "ok"

        inner = (meta.get("innerInstructions") or [])
        for group in inner:
            for ix in (group.get("instructions") or []):
                parsed = ix.get("parsed") or {}
                info = parsed.get("info") or {}
                for k in ("mint", "tokenMint", "baseMint", "quoteMint"):
                    cand = info.get(k)
                    if cand and is_valid_dual_lane_mint(cand):
                        return cand, "tx_inner", "ok"

                raw_accounts = [cand for cand in (ix.get("accounts") or []) if isinstance(cand, str)]
                for cand in raw_accounts:
                    if is_valid_pump_mint(cand):
                        return cand, "tx_inner_accounts", "ok"

        msg = ((tx.get("transaction") or {}).get("message") or {})
        for row in (msg.get("accountKeys") or []):
            cand = row.get("pubkey") if isinstance(row, dict) else row
            if isinstance(cand, str) and is_valid_pump_mint(cand):
                return cand, "tx_account_keys", "ok"

        for line in (meta.get("logMessages") or []):
            line_text = str(line)
            if line_text.startswith("Program data:") or line_text.startswith("Program return:"):
                continue
            cand = _extract_contextual_mint_from_line(line_text)
            if cand:
                return cand, "tx_log_context", "ok"
            for token in re.findall(r"[1-9A-HJ-NP-Za-km-z]{32,44}", line_text):
                if is_valid_dual_lane_mint(token):
                    return token, "tx_log", "ok"

        if excluded_seen:
            return None, "none", "excluded_mint"
    except Exception:
        return None, "none", "no_balances"
    return None, "none", "no_balances"


def is_high_confidence_mint_source(mint: str, source: str) -> bool:
    if not mint:
        return False
    if is_valid_pump_mint(mint):
        return True
    return source in {"helius_token_transfer", "helius_account_data", "tx_balances", "tx_inner"}
RUGCHECK_API_BASE = os.getenv("RUGCHECK_API_BASE", "https://api.rugcheck.xyz/v1")
SOLSNIFFER_API_BASE = os.getenv("SOLSNIFFER_API_BASE", "https://api.solsniffer.com/v1")

SNIPER_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SNIPER_TG_CHAT_ID = os.getenv("SNIPER_ALERT_CHAT_ID", "")

SLIPPAGE_PCT = float(os.getenv("LIVE_BUY_MAX_SLIPPAGE_PCT", "0.03"))  # was 0.005 — 0.5% too tight for meme pools; 3% realistic for fresh Raydium
JITO_TIP_SOL = 0.001
BASE_MIN_LIQUIDITY_USD = float(os.getenv("BASE_MIN_LIQUIDITY_USD", "25000.0"))  # floor for dynamic liquidity threshold
WS_HEARTBEAT_SECONDS = 60
WS_PING_INTERVAL_SECONDS = float(os.getenv("WS_PING_INTERVAL_SECONDS", "45"))
WS_PING_TIMEOUT_SECONDS = float(os.getenv("WS_PING_TIMEOUT_SECONDS", "30"))
WS_STALE_HEARTBEAT_SECONDS = float(os.getenv("WS_STALE_HEARTBEAT_SECONDS", "120"))
WS_STALE_CANDIDATE_SECONDS = float(os.getenv("WS_STALE_CANDIDATE_SECONDS", "180"))
PRICE_RETRY_ATTEMPTS = int(os.getenv("PRICE_RETRY_ATTEMPTS", "2"))       # was 4 — if Jupiter/DexScreener don't have it in 2 tries, it's not indexed yet
PRICE_RETRY_DELAY_SECONDS = float(os.getenv("PRICE_RETRY_DELAY_SECONDS", "0.5"))  # was 0.75 — save 0.5s total
MARK_PRICE_RETRY_ATTEMPTS = int(os.getenv("MARK_PRICE_RETRY_ATTEMPTS", "2"))
MARK_PRICE_RETRY_DELAY_SECONDS = float(os.getenv("MARK_PRICE_RETRY_DELAY_SECONDS", "0.25"))
MARK_PRICE_CACHE_TTL_MS = int(os.getenv("MARK_PRICE_CACHE_TTL_MS", "500"))  # 500ms cache to prevent API hammering
PUMP_CURVE_MARKET_CAP_USD = float(os.getenv("PUMP_CURVE_MARKET_CAP_USD", "13000"))
PUMP_TOTAL_SUPPLY = float(os.getenv("PUMP_TOTAL_SUPPLY", "1000000000"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "25000"))  # was $1k — thin pools amplify slippage; $25k minimum meaningfully improves fills
LOW_LIQUIDITY_PAPER_BUY_CAP = int(os.getenv("LOW_LIQUIDITY_PAPER_BUY_CAP", "5"))
# Take-profit tier values live in constants.py (P4.1 SSOT). TP1/2/3 imported above.
# On-chain fast-path veto (bypass rugcheck timeout when mint/freeze authority already revoked)
ONCHAIN_VETO_ENABLED = os.getenv("ONCHAIN_VETO_ENABLED", "true").lower() == "true"
ONCHAIN_VETO_TIMEOUT_SECONDS = float(os.getenv("ONCHAIN_VETO_TIMEOUT_SECONDS", "2.0"))
# Token age gate: veto tokens older than this at decision time
MAX_TOKEN_AGE_SECONDS = int(os.getenv("MAX_TOKEN_AGE_SECONDS", "120"))  # was 600 — tokens older than 2 min at decision time have likely already peaked
# Holder concentration check (off by default — adds ~200ms via Helius RPC)
HOLDER_CHECK_ENABLED = os.getenv("HOLDER_CHECK_ENABLED", "true").lower() == "true"  # was false — catches 80%+ concentrated supply (rug prime)
TOP5_HOLDER_PCT_MAX  = float(os.getenv("TOP5_HOLDER_PCT_MAX", "60.0"))  # veto if top-5 > 60%
HOLDER_CHECK_TIMEOUT_SECONDS = float(os.getenv("HOLDER_CHECK_TIMEOUT_SECONDS", "2.0"))

# Phase 4 execution settings
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "paper").strip().lower()
if EXECUTION_MODE not in {"paper", "live"}:
    EXECUTION_MODE = "paper"

# Safety latch: live trading requires explicit arming phrase.
LIVE_ARM_ACK = os.getenv("ALLOW_LIVE_TRADING", "").strip()
if EXECUTION_MODE == "live" and LIVE_ARM_ACK != "YES_I_UNDERSTAND_LIVE_RISK":
    EXECUTION_MODE = "paper"
    log.error(
        "[LIVE_ARM] EXECUTION_MODE downgraded to 'paper' — "
        "ALLOW_LIVE_TRADING is missing or does not match required value. "
        "Set ALLOW_LIVE_TRADING=YES_I_UNDERSTAND_LIVE_RISK in .env to arm live trading."
    )

# Profile determination — reads ACTIVE_PROFILE from env; infers if absent.
# This is additive metadata only — no behavioral change.
ACTIVE_PROFILE: str = os.getenv("ACTIVE_PROFILE", "").strip().upper()
if not ACTIVE_PROFILE:
    ACTIVE_PROFILE = "LIVE_CANARY_SAFE" if EXECUTION_MODE == "live" else "PAPER"
_ACTIVE_ENV_SOURCE: str = ".env"

# Lock policy — controls the liquidity lock gate in rug_check().
# standard       : require locked=True OR burned=True from rugcheck top-level liquidity field
# lp_pct_threshold: RESEARCH ONLY — accept pump_fun tokens where launchpad.platform=pump_fun
#                   AND markets[].lp.lpLockedPct >= RESEARCH_LP_LOCK_PCT_MIN AND lpUnlocked=0
#                   AND lpLockedUSD >= MIN_LIQUIDITY_USD.
#                   Addresses rugcheck schema inconsistency: pump.fun migrations return
#                   liquidity:{} (empty) while lock data is in markets[].lp.*.
LOCK_POLICY: str = os.getenv("LOCK_POLICY", "standard").strip().lower()
if LOCK_POLICY not in {"standard", "lp_pct_threshold"}:
    log.error("[LOCK_POLICY] Unrecognized value '%s' — defaulting to 'standard'", LOCK_POLICY)
    LOCK_POLICY = "standard"
RESEARCH_LP_LOCK_PCT_MIN: float = float(os.getenv("RESEARCH_LP_LOCK_PCT_MIN", "95.0"))

log.info("=" * 60)
log.info("[PROFILE] Active Profile    : %s", ACTIVE_PROFILE)
log.info("[PROFILE] Execution Mode    : %s", EXECUTION_MODE)
log.info("[PROFILE] Lock Policy       : %s", LOCK_POLICY)
log.info("[PROFILE] Active Env Source : %s", _ACTIVE_ENV_SOURCE)
log.info("[PROFILE] Real Wallet Active: %s", str(EXECUTION_MODE == "live").upper())
log.info("[PROFILE] Sim Budget Auth   : %s", str(EXECUTION_MODE != "live").upper())
log.info("[PROFILE] Live TX Permitted : %s", str(EXECUTION_MODE == "live").upper())
log.info("[PROFILE] Live Arm ACK      : %s", str(LIVE_ARM_ACK == "YES_I_UNDERSTAND_LIVE_RISK").upper())
if LOCK_POLICY == "lp_pct_threshold":
    log.warning("[PROFILE] !! RESEARCH LOCK POLICY ACTIVE — lp_pct_threshold min=%.1f%%", RESEARCH_LP_LOCK_PCT_MIN)
log.info("=" * 60)

MAX_POSITION_SIZE_SOL = float(os.getenv("MAX_POSITION_SIZE", "0.05"))
LIVE_CANARY_TRADES = int(os.getenv("LIVE_CANARY_TRADES", "5"))
LIVE_BASE_TRADE_USD = float(os.getenv("LIVE_BASE_TRADE_USD", "0.50"))
LIVE_SELL_MAX_SLIPPAGE_PCT = float(os.getenv("LIVE_SELL_MAX_SLIPPAGE_PCT", "0.10"))  # was 0.02 — 10% ensures stop-loss TXs execute on hard dumps
LIVE_SELL_TP_SLIPPAGE_PCT  = float(os.getenv("LIVE_SELL_TP_SLIPPAGE_PCT",  "0.03"))  # tighter 3% for TP exits (we want best price, not just execution)

# Pre-entry drift guard: re-quote right before buy and reject if price dropped too far from signal price.
# Prevents "buying the dump" — entering after the initial pump has already reversed.
ENTRY_DRIFT_MAX_DROP_PCT = float(os.getenv("ENTRY_DRIFT_MAX_DROP_PCT", "0.15"))   # reject if price dropped 15%+ since signal
ENTRY_DRIFT_ENABLED = os.getenv("ENTRY_DRIFT_ENABLED", "true").strip().lower() == "true"
# Pre-entry swap route gate: verify Jupiter can actually route a sell BEFORE buying.
# Default is now OFF — with the pump-curve price fallback removed, only tokens with a
# real DEX price (Jupiter/DexScreener) can reach this point.  Raydium pools are often
# not yet indexed by Jupiter at migration time (30-120s lag), so the gate would add 5s
# of latency and block legitimate entries.  With route-retry logic in the delayed-entry
# worker this is now safe: if routing fails at T+30s, the worker retries at T+45/60/75s.
ENTRY_SWAP_ROUTE_CHECK_ENABLED = os.getenv("ENTRY_SWAP_ROUTE_CHECK_ENABLED", "true").strip().lower() == "true"
ENTRY_SWAP_ROUTE_TIMEOUT_SECONDS = float(os.getenv("ENTRY_SWAP_ROUTE_TIMEOUT_SECONDS", "5"))

# Compute budget + priority policy (micro-lamports/CU)
CU_PRICE_MIN = int(os.getenv("CU_PRICE_MIN", "10000"))
CU_PRICE_MAX = int(os.getenv("CU_PRICE_MAX", "150000"))
CU_LIMIT_DEFAULT = int(os.getenv("CU_LIMIT_DEFAULT", "200000"))
CU_LIMIT_HEAVY = int(os.getenv("CU_LIMIT_HEAVY", "300000"))

# Deterministic Jito tip config
JITO_TIP_LAMPORTS = int(os.getenv("JITO_TIP_LAMPORTS", "20000"))
JITO_REGION = os.getenv("JITO_REGION", "ny").strip().lower()
JITO_TIP_ACCOUNTS = {
    # Official Jito tip accounts (deterministic mapping for region selection)
    "ny": "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "frankfurt": "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "tokyo": "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
}
# P6.5: full rotation pool of 8 official Jito tip accounts. Round-robin by
# default; a pinned JITO_REGION still works (returns the region's account).
# Setting JITO_TIP_ACCOUNT in env overrides both.
JITO_TIP_ACCOUNTS_ALL = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pivKeVBBYvnzRtkaMZC",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]
JITO_TIP_ROTATE = os.getenv("JITO_TIP_ROTATE", "true").strip().lower() == "true"
_jito_tip_rr_idx: int = 0

EVENT_QUEUE_MAX = int(os.getenv("EVENT_QUEUE_MAX", "2048"))
EVENT_WORKERS = max(1, int(os.getenv("EVENT_WORKERS", "3")))
MTM_REFRESH_CONCURRENCY = max(1, int(os.getenv("MTM_REFRESH_CONCURRENCY", "4")))
OPEN_POSITION_REFRESH_INTERVAL_SECONDS = float(os.getenv("OPEN_POSITION_REFRESH_INTERVAL_SECONDS", "1.5"))

RAYDIUM_OWNER_CANDIDATES = [
    str(os.getenv("RAYDIUM_AUTHORITY", "")).strip(),
    str(os.getenv("RAYDIUM_CPMM_AUTHORITY", "")).strip(),
    str(os.getenv("RAYDIUM_AMM_V4_AUTHORITY", "")).strip(),
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",  # Pump migrator Raydium authority
]
RAYDIUM_OWNER_CANDIDATES = [x for x in RAYDIUM_OWNER_CANDIDATES if x]

# Reserve-miss negative cache: suppress repeated getTokenAccountsByOwner fan-outs
# on the same mint for this many seconds after a full-owner miss. Stops the
# repeated RESERVE_ALL_OWNERS_MISS spam that was consuming event loop cycles.
RESERVE_MISS_SUPPRESS_SECONDS = float(os.getenv("RESERVE_MISS_SUPPRESS_SECONDS", "25.0"))
# Per-mint miss cap: after this many misses within the suppress window, log once
# and skip silently until TTL expires (no telemetry spam).
RESERVE_MISS_CAP_PER_WINDOW = int(os.getenv("RESERVE_MISS_CAP_PER_WINDOW", "3"))
# Hard cap on total per-mint reserve-miss retries (across all windows).
# Once a mint exceeds this count it is permanently suppressed for the session.
# Prevents 600+ suppressed events from mints that will never have a reserve.
RESERVE_MISS_TOTAL_CAP = int(os.getenv("RESERVE_MISS_TOTAL_CAP", "8"))
# Global session-level telemetry write cap for reserve-miss events across ALL mints.
# After this many total RESERVE_ALL_OWNERS_MISS events have been written, suppress
# further telemetry writes (counter still tracks internally). Eliminates runtime_state
# write amplification from high-volume reserve misses with many unique mints.
RESERVE_MISS_TELEMETRY_SESSION_CAP = int(os.getenv("RESERVE_MISS_TELEMETRY_SESSION_CAP", "60"))

# Mint extraction retry tuning (helps keep decision latency bounded)
MINT_PARSE_RETRIES = int(os.getenv("MINT_PARSE_RETRIES", "3"))  # P1-1: was 4, env often overrides to 2 (too low)
MINT_PARSE_RETRY_DELAY_SECONDS = float(os.getenv("MINT_PARSE_RETRY_DELAY_SECONDS", "0.15"))
MINT_PARSE_FETCH_TIMEOUT_SECONDS = float(os.getenv("MINT_PARSE_FETCH_TIMEOUT_SECONDS", "3"))   # P1-1: was 8, env often overrides to 1.2 (too aggressive for Helius p95)
MINT_PARSE_CANDIDATE_BUDGET_SECONDS = float(os.getenv("MINT_PARSE_CANDIDATE_BUDGET_SECONDS", "5"))  # P1-1: was 4; give retries time to complete within budget
DEPENDENCY_PRIMARY_BUDGET_SECONDS = float(os.getenv("DEPENDENCY_PRIMARY_BUDGET_SECONDS", "0.85"))
MINT_PARSE_LATE_SETTLE_DELAY_SECONDS = float(os.getenv("MINT_PARSE_LATE_SETTLE_DELAY_SECONDS", "0.6"))
MINT_PARSE_PIN_RPC_ATTEMPTS = int(os.getenv("MINT_PARSE_PIN_RPC_ATTEMPTS", "1"))
SIG_PARSE_CACHE_MAX = int(os.getenv("SIG_PARSE_CACHE_MAX", "5000"))
HELIUS_MUTABLE_TIMEOUT_SECONDS = float(os.getenv("HELIUS_MUTABLE_TIMEOUT_SECONDS", "3.0"))

# Limit concurrent getTransaction calls to avoid Helius 429 bursts when multiple
# candidates arrive simultaneously.  Default 3 keeps headroom under the ~10 rps free tier.
_GET_TX_SEMAPHORE = asyncio.Semaphore(int(os.getenv("GET_TX_CONCURRENCY", "3")))

# Migration retry queue: re-attempt price lookup for confirmed migrations that failed
# on first pass (Jupiter/DexScreener indexing lag after Raydium pool creation).
MIGRATION_RETRY_ENABLED = os.getenv("MIGRATION_RETRY_ENABLED", "true").strip().lower() == "true"
MIGRATION_RETRY_DELAYS_SECONDS = [float(x) for x in os.getenv("MIGRATION_RETRY_DELAYS_SECONDS", "30,60,90").split(",")]
MIGRATION_RETRY_QUEUE_MAX = int(os.getenv("MIGRATION_RETRY_QUEUE_MAX", "100"))

# Delayed entry: cache confirmed migrations, wait N seconds, then enter only if price held.
# Skips the initial sniper dump (15-30s) and ensures Jupiter has indexed the pool.
DELAYED_ENTRY_ENABLED = os.getenv("DELAYED_ENTRY_ENABLED", "true").strip().lower() == "true"
DELAYED_ENTRY_SECONDS = float(os.getenv("DELAYED_ENTRY_SECONDS", "30"))
# Max acceptable drop from reference price at dequeue time — beyond this, discard.
DELAYED_ENTRY_MAX_DROP_PCT = float(os.getenv("DELAYED_ENTRY_MAX_DROP_PCT", "0.15"))

# ── PRE-ENTRY SETTLE WINDOW ──────────────────────────────────────────────────
# After all pre-execution gates pass, take a final price sample, wait briefly,
# and re-sample. If the price dropped beyond SETTLE_MAX_DROP_PCT in that window
# the token is in an active first-tick dump — reject the entry. Empirically,
# detect_to_exec_ms >= 1500ms trades had 33.6% WR vs 22.6% for faster exec
# (advisor-confirmed audit, 2026-04-23). The accidental latency was filtering
# out early dumpers; this makes that filter intentional.
SETTLE_WINDOW_ENABLED = os.getenv("SETTLE_WINDOW_ENABLED", "true").strip().lower() == "true"
SETTLE_WINDOW_SECONDS = float(os.getenv("SETTLE_WINDOW_SECONDS", "1.2"))
SETTLE_MAX_DROP_PCT = float(os.getenv("SETTLE_MAX_DROP_PCT", "0.03"))

# Entry scoring (pre-fetched during 30s delayed-entry wait — zero added latency at exec time).
# Total possible = 100. Threshold gates execution at T+30.
# Components: time_to_graduate(40) + dexscreener(30) + holder(20) + socials(10).
ENTRY_SCORE_ENABLED = os.getenv("ENTRY_SCORE_ENABLED", "true").strip().lower() == "true"
ENTRY_SCORE_THRESHOLD = int(os.getenv("ENTRY_SCORE_THRESHOLD", "50"))
ENTRY_SCORE_FETCH_TIMEOUT_SECONDS = float(os.getenv("ENTRY_SCORE_FETCH_TIMEOUT_SECONDS", "1.5"))
ENTRY_SCORE_AWAIT_TIMEOUT_SECONDS = float(os.getenv("ENTRY_SCORE_AWAIT_TIMEOUT_SECONDS", "0.5"))

# ── RUNTIME CONFIG SYNC LOG ───────────────────────────────────────────────────
# Emitted at process start. If the HUD shows different values, the process is
# reading stale env vars or a different .env file. Use this to diagnose drift.
log.info("=" * 60)
log.info("[CONFIG_SYNC] EXECUTION_MODE              = %s", EXECUTION_MODE)
log.info("[CONFIG_SYNC] ACTIVE_PROFILE              = %s", os.getenv("ACTIVE_PROFILE", "NOT_SET"))
log.info("[CONFIG_SYNC] TRADE_SIZE_USD (LIVE)       = $%.4f", LIVE_BASE_TRADE_USD)
log.info("[CONFIG_SYNC] CANARY_TRADE_SIZE_USD       = $%.4f  (see live_canary.py)",
         float(os.getenv("CANARY_TRADE_SIZE_USD", "5.0")))
log.info("[CONFIG_SYNC] DEFAULT_DOWNSIDE_STOP_PCT   = %.1f%%", DEFAULT_DOWNSIDE_STOP_PCT * 100)
log.info("[CONFIG_SYNC] ABSOLUTE_FAILSAFE_STOP_PCT  = %.1f%%", ABSOLUTE_FAILSAFE_STOP_PCT * 100)
log.info("[CONFIG_SYNC] CANARY_FAILSAFE_STOP_PCT    = %.1f%%  (see live_canary.py)",
         float(os.getenv("CANARY_FAILSAFE_STOP_PCT", "0.50")) * 100)
log.info("[CONFIG_SYNC] MAX_POSITION_LOSS_PCT        = %.1f%%", MAX_POSITION_LOSS_PCT * 100)
log.info("[CONFIG_SYNC] MIN_LIQUIDITY_USD            = $%.0f", MIN_LIQUIDITY_USD)
log.info("[CONFIG_SYNC] QUOTEABILITY_WINDOW_S        = %ds", QUOTEABILITY_VALIDATION_WINDOW_SECONDS)
log.info("[CONFIG_SYNC] JITO_TIP_LAMPORTS            = %d", JITO_TIP_LAMPORTS)
log.info("[CONFIG_SYNC] JITO_TIP_HIGH_LAMPORTS       = %d  (early-entry tier, see live_canary.py)",
         int(os.getenv("JITO_TIP_HIGH_LAMPORTS", "50000")))
log.info("[CONFIG_SYNC] JITO_DYNAMIC_TIP_ENABLED     = %s", os.getenv("JITO_DYNAMIC_TIP_ENABLED", "true"))
log.info("[CONFIG_SYNC] JITO_EARLY_ENTRY_TIP_AGE_MS  = %s ms", os.getenv("JITO_EARLY_ENTRY_TIP_AGE_MS", "5000"))
log.info("[CONFIG_SYNC] CU_PRICE_MIN                 = %d", CU_PRICE_MIN)
log.info("[CONFIG_SYNC] CU_PRICE_MAX                 = %d", CU_PRICE_MAX)
log.info("[CONFIG_SYNC] DELAYED_ENTRY                = enabled=%s delay=%ss max_drop=%.0f%%",
         DELAYED_ENTRY_ENABLED, DELAYED_ENTRY_SECONDS, DELAYED_ENTRY_MAX_DROP_PCT * 100)
log.info("[CONFIG_SYNC] SETTLE_WINDOW                = enabled=%s window=%.2fs max_drop=%.2f%%",
         SETTLE_WINDOW_ENABLED, SETTLE_WINDOW_SECONDS, SETTLE_MAX_DROP_PCT * 100)
log.info("[CONFIG_SYNC] ENTRY_SCORE                  = enabled=%s threshold=%d graduation_proxy=time_since_detection (pairCreatedAt deprecated)",
         ENTRY_SCORE_ENABLED, ENTRY_SCORE_THRESHOLD)
log.info("[CONFIG_SYNC] BREAKEVEN_TRIGGER_PCT        = %.1f%%", BREAKEVEN_TRIGGER_PCT * 100)
log.info("[CONFIG_SYNC] TRAIL_FROM_MFE_PCT           = %.1f%%", TRAIL_FROM_MFE_PCT * 100)
log.info("[CONFIG_SYNC] GHOST_BUY_PENDING_RECHECK    = %ds", GHOST_BUY_PENDING_RECHECK_SECONDS)
log.info("[CONFIG_SYNC] PRE_ENTRY_SURGE_RATIO_MAX    = %.2fx", PRE_ENTRY_SURGE_RATIO_MAX)
log.info("[CONFIG_SYNC] SECONDARY_RPC_URL            = %s", SECONDARY_RPC_URL or "NOT_SET")
log.info("[CONFIG_SYNC] TERTIARY_RPC_URL             = %s", TERTIARY_RPC_URL or "NOT_SET")
log.info("[CONFIG_SYNC] READ_RPC_POOL_SIZE           = %d  (round-robin for getAsset/getSignaturesForAddress/getTokenLargestAccounts)",
         len(_READ_RPC_POOL))
log.info("[CONFIG_SYNC] GET_TX_CONCURRENCY           = %d",
         int(os.getenv("GET_TX_CONCURRENCY", "3")))
log.info("=" * 60)
# ── END RUNTIME CONFIG SYNC LOG ──────────────────────────────────────────────

if not WS_URLS:
    raise SystemExit("Missing WS_URL/WS_URLS")

STATE_FILE = Path(__file__).parent / "runtime_state.json"
STATE_LOCK_FILE = Path(__file__).parent / ".runtime_state.lock"
PROCESS_LOCK_FILE = Path("/tmp/solana_sniper_simulation.lock")
HISTORY_FILE = Path(__file__).parent / "history.json"
# P5.1: operator commands flow through an append-only jsonl owned by the HUD.
# This loop tails the file on each _process_operator_commands cycle and
# persists the highest-processed offset in runtime_state.operator_cmd_cursor.
OPERATOR_COMMANDS_FILE = Path(__file__).parent / "data" / "operator_commands.jsonl"
OPERATOR_COMMANDS_LOCK = Path(__file__).parent / "data" / ".operator_commands.lock"
METRICS_SNAPSHOT_FILE = Path(__file__).parent / "metrics_snapshots.jsonl"
METRICS_SNAPSHOT_INTERVAL_SECONDS = int(os.getenv("METRICS_SNAPSHOT_INTERVAL_SECONDS", "5"))
METRICS_SNAPSHOT_MAX_BYTES = int(os.getenv("METRICS_SNAPSHOT_MAX_BYTES", str(50 * 1024 * 1024)))


def _rotate_metrics_snapshot_if_needed():
    try:
        if not METRICS_SNAPSHOT_FILE.exists():
            return
        if METRICS_SNAPSHOT_FILE.stat().st_size < METRICS_SNAPSHOT_MAX_BYTES:
            return
        import gzip, shutil
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        gz_path = METRICS_SNAPSHOT_FILE.with_name(f"metrics_snapshots.{stamp}.jsonl.gz")
        with open(METRICS_SNAPSHOT_FILE, "rb") as src, gzip.open(gz_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        METRICS_SNAPSHOT_FILE.unlink()
    except Exception as e:
        log.warning("[SNAPSHOT_ROTATE] failed: %s", e)
VETO_LOG_FILE = Path(__file__).parent / "logs" / "veto_history.jsonl"  # was hardcoded Mac path
_HISTORY_CACHE: Optional[list[dict]] = None
# P2.8: set by append_history, cleared by _flush_history_to_disk_sync.
_HISTORY_DIRTY: bool = False
_HISTORY_FLUSH_INTERVAL_SECONDS: float = 0.5


def acquire_process_lock():
    PROCESS_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(PROCESS_LOCK_FILE, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("simulation_mode singleton lock already held; refusing second writer")
    fh.seek(0)
    fh.truncate(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def default_metrics(day: str) -> dict:
    return {
        "day_utc": day,
        "raw_candidates": 0,
        "qualified_candidates": 0,
        "total_scans": 0,
        "unique_mints": 0,
        "duplicates_skipped": 0,
        "vetoed_liquidity": 0,
        "vetoed_price": 0,
        "successful_entries": 0,
        "candidate_duplicates_deduped": 0,
        "mint_extracted_from_logs": 0,
        "mint_extracted_from_rpc": 0,
        "mint_parse_cache_hits": 0,
        "mint_parse_successes": 0,
        "mint_parse_retry_count": 0,
        "mint_parse_latency_sum_ms": 0.0,
        "mint_parse_gettx_wait_sum_ms": 0.0,
        "parse_failures": 0,
        "parse_fail_no_sig": 0,
        "parse_fail_rpc_429": 0,
        "parse_fail_rpc_timeout": 0,
        "parse_fail_no_balances": 0,
        "parse_fail_excluded_mint": 0,
        "parse_fail_low_confidence": 0,
        "tx_seen_but_unusable": 0,
        "rpc_429_count": 0,
        "ws_disconnects": 0,
        "ws_1011_reconnects": 0,
        "closed_wins": 0,
        "closed_losses": 0,
        "win_rate_pct": 0.0,
        "loss_rate_pct": 0.0,
        "avg_decision_latency_ms": 0.0,
        "decision_count": 0,
        "decision_latency_sum_ms": 0.0,
        "market_temp_mints_per_hour": 0.0,
        "session_start_ts": int(time.time()),
    }


def zero_phase5c_block() -> dict:
    return {
        "realized_pnl_usd": 0.0,
        "unrealized_pnl_usd": 0.0,
        "total_pnl_usd": 0.0,
        "closed_trades": 0,
        "open_positions": 0,
        "avg_hold_time_seconds": 0.0,
        "avg_mfe_pct": 0.0,
        "avg_mae_pct": 0.0,
        "expectancy": {
            "per_trade_usd": 0.0,
            "win_rate_pct": 0.0,
            "avg_win_usd": 0.0,
            "avg_loss_usd": 0.0,
        },
        "win_loss_by_close_reason": {},
    }


def read_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        # P2.7: atomic read. os.replace makes writes atomic at the inode level, but
        # a partial parse can still fail on rare filesystems or if a non-canonical
        # writer (analyze.sh, HUD writer) is mid-flush. Acquire LOCK_SH so we block
        # during an LOCK_EX writer pass, and retry once on parse failure before
        # falling back to defaults — silently returning defaults masks real corruption.
        for _attempt in range(2):
            lock_fh = None
            try:
                STATE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
                lock_fh = open(STATE_LOCK_FILE, "a+")
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
                st = json.loads(STATE_FILE.read_text())
                st.setdefault("recent_activity", [])
                st.setdefault("telemetry_events", [])
                st.setdefault("active_positions", [])
                st.setdefault("paper_positions", [])
                st.setdefault("shadow_watch", [])
                st.setdefault("daily_metrics", default_metrics(utc_day()))
                st.setdefault("all_time_metrics", default_metrics("all-time"))
                st.setdefault("operator_commands", [])
                st.setdefault("operator_controls", {
                    "disable_new_entries": False,
                    "default_downside_stop_pct": DEFAULT_DOWNSIDE_STOP_PCT,
                    "absolute_failsafe_stop_pct": ABSOLUTE_FAILSAFE_STOP_PCT,
                })
                return st
            except Exception as _read_err:
                if _attempt == 1:
                    log.warning("[STATE_READ] parse failed after retry — falling back to defaults: %s", _read_err)
            finally:
                if lock_fh is not None:
                    try:
                        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                        lock_fh.close()
                    except Exception:
                        pass
    return {
        "killswitch": False,
        "avg_latency_ms": 0.0,
        "pnl_usd": 0.0,
        "updated_at": int(time.time()),
        "recent_activity": [],
        "telemetry_events": [],
        "active_positions": [],
        "paper_positions": [],
        "shadow_watch": [],
        "daily_metrics": default_metrics(utc_day()),
        "all_time_metrics": default_metrics("all-time"),
        "operator_commands": [],
        "operator_controls": {
            "disable_new_entries": False,
            "default_downside_stop_pct": DEFAULT_DOWNSIDE_STOP_PCT,
            "absolute_failsafe_stop_pct": ABSOLUTE_FAILSAFE_STOP_PCT,
        },
    }


def _locked_state_update(mutator, force_flush: bool = False) -> bool:
    lock_fh = None
    t_lock_start = time.perf_counter()
    try:
        STATE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock_fh = open(STATE_LOCK_FILE, "a+")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        t_lock_acquired = time.perf_counter()

        if STATE_FILE.exists():
            try:
                base = json.loads(STATE_FILE.read_text())
            except Exception:
                base = read_state()
        else:
            base = read_state()

        mutator(base)
        base["updated_at"] = int(time.time())

        trace_runtime = base.get("trace_runtime") or {}
        trace_runtime["state_lock_wait_ms"] = round((t_lock_acquired - t_lock_start) * 1000, 4)
        base["trace_runtime"] = trace_runtime
        payload = json.dumps(base)
        trace_runtime["state_payload_bytes"] = len(payload.encode("utf-8"))
        # Re-serialize with payload_bytes included
        payload = json.dumps(base)
        tmp_path = STATE_FILE.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)

        if force_flush:
            dir_fd = os.open(str(STATE_FILE.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        return True
    except Exception:
        return False
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                lock_fh.close()
            except Exception:
                pass


def write_state(force_flush: bool = False, **kwargs) -> bool:
    return _locked_state_update(lambda base: base.update(kwargs), force_flush=force_flush)


def load_history() -> list[dict]:
    global _HISTORY_CACHE
    if _HISTORY_CACHE is not None:
        return list(_HISTORY_CACHE)
    if HISTORY_FILE.exists():
        try:
            raw = HISTORY_FILE.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                log.warning("[HISTORY] history.json is not a list — resetting cache")
                _HISTORY_CACHE = []
            else:
                # Enforce cap on startup in case the file was written by an older version
                # with a lower cap, or grew beyond the limit outside this process.
                _HISTORY_CACHE = data[-10000:]
                if len(data) != len(_HISTORY_CACHE):
                    log.warning("[HISTORY] history.json had %d events — truncated to %d on load", len(data), len(_HISTORY_CACHE))
            return list(_HISTORY_CACHE)
        except json.JSONDecodeError as e:
            log.error("[HISTORY] history.json JSON parse error (offset %d): %s — starting fresh", e.pos, e)
            _HISTORY_CACHE = []
            return []
        except Exception as e:
            log.error("[HISTORY] history.json load failed: %s — starting fresh", e)
            _HISTORY_CACHE = []
            return []
    _HISTORY_CACHE = []
    return []


def append_history(event: dict):
    # P2.8: sync path only updates the in-memory cache + flips the dirty flag.
    # Disk I/O (serialize + atomic rename) is batched by the engine's
    # _history_flush_loop (runs every 500ms in a thread), so hot async callers
    # (signal/close/partial_realize emitters) never block on file I/O. A crash
    # loses at most ~500ms of history.json; runtime_state.json remains canonical
    # for recovery-critical state.
    global _HISTORY_CACHE, _HISTORY_DIRTY
    if _HISTORY_CACHE is None:
        _HISTORY_CACHE = load_history()
    _HISTORY_CACHE.append(event)
    _HISTORY_CACHE = _HISTORY_CACHE[-10000:]
    _HISTORY_DIRTY = True


def _flush_history_to_disk_sync() -> bool:
    """Blocking write of _HISTORY_CACHE to history.json via atomic rename.
    Intended to be dispatched through asyncio.to_thread from _history_flush_loop
    (or invoked directly during shutdown). Returns True on success."""
    global _HISTORY_DIRTY
    if _HISTORY_CACHE is None:
        _HISTORY_DIRTY = False
        return True
    try:
        payload = json.dumps(_HISTORY_CACHE)
        _tmp = HISTORY_FILE.with_suffix(".json.tmp")
        _tmp.write_text(payload)
        _tmp.replace(HISTORY_FILE)
        _HISTORY_DIRTY = False
        return True
    except Exception as e:
        log.warning("[HISTORY_FLUSH] write failed — will retry on next tick: %s", e)
        return False


def history_realized_pnl_since(ts_start: int) -> float:
    """Sum realized P&L from history since ts_start.

    Sums ONLY trade_close events. trade_close.pnl_usd carries the cumulative
    net P&L for the full position (inclusive of all partial sells). Summing
    partial_realize events on top would double-count because partials are
    incremental fragments already included in the final trade_close figure.
    """
    cutoff = int(ts_start or 0)
    total = 0.0
    for x in load_history():
        ev_type = str(x.get("type") or "")
        ev_ts   = int(x.get("ts", 0) or 0)
        if ev_ts < cutoff:
            continue
        if ev_type == "trade_close":
            total += float(x.get("pnl_usd") or 0.0)
    return round(total, 6)


def append_telemetry_event(event_type: str, mint: str = "", detail: str = "", severity: str = "info", ts: Optional[int] = None, max_rows: int = 3000):
    row = {
        "ts": int(ts or time.time()),
        "type": event_type,
        "mint": mint,
        "detail": detail,
        "severity": severity,
    }

    def _mut(base):
        rows = base.get("telemetry_events") or []
        rows.append(row)
        base["telemetry_events"] = rows[-max_rows:]

    _locked_state_update(_mut)


def append_recent_activity(action: str, mint: str, live_pnl: float = 0.0, ts: Optional[int] = None, max_rows: int = 300):
    stamp = int(ts or time.time())
    telemetry_row = {
        "ts": stamp,
        "type": action.replace(" ", "_"),
        "mint": mint,
        "detail": "",
        "severity": "info",
    }

    def _mut(base):
        rows = base.get("recent_activity") or []
        rows.append(
            {
                "timestamp": stamp,
                "action": action,
                "mint": mint,
                "live_pnl": float(live_pnl),
            }
        )
        base["recent_activity"] = rows[-max_rows:]
        telemetry = base.get("telemetry_events") or []
        telemetry.append(telemetry_row)
        base["telemetry_events"] = telemetry[-3000:]

    _locked_state_update(_mut)


def append_veto(
    mint: str,
    reason: str,
    source: str = "simulation",
    historical_threshold_usd: Optional[float] = None,
    current_policy_threshold_usd: Optional[float] = None,
    historical_threshold_context_only: bool = False,
):
    VETO_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts_now = int(time.time())
    effective_current_policy = float(current_policy_threshold_usd if current_policy_threshold_usd is not None else MIN_LIQUIDITY_USD)
    effective_historical = float(historical_threshold_usd if historical_threshold_usd is not None else effective_current_policy)
    row = {
        "ts": ts_now,
        "mint": mint,
        "reason": reason,
        "source": source,
        "historical_threshold_usd": effective_historical,
        "current_policy_threshold_usd": effective_current_policy,
        "historical_threshold_context_only": bool(historical_threshold_context_only),
    }
    with open(VETO_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    queue_telemetry_event("VETO", mint=mint, detail=reason, severity="warn", ts=ts_now)


def rotate_veto_log_if_needed(max_bytes: int = 5 * 1024 * 1024):
    if not VETO_LOG_FILE.exists():
        return
    try:
        if VETO_LOG_FILE.stat().st_size <= max_bytes:
            return
    except Exception as e:
        log.warning("[VETO_LOG_ROTATE] stat failed: %s", e)
        return
    ts = int(time.time())
    bak = VETO_LOG_FILE.with_suffix(f".jsonl.{ts}.bak")
    try:
        VETO_LOG_FILE.rename(bak)
        VETO_LOG_FILE.write_text("")
    except Exception as e:
        # P6.8: rename can fail on Windows/cross-device/locked-file; fall back
        # to in-place truncation so logging stays bounded even without rotation.
        log.warning("[VETO_LOG_ROTATE] rename failed (%s) — truncating in place", e)
        try:
            VETO_LOG_FILE.write_text("")
        except Exception as e2:
            log.error("[VETO_LOG_ROTATE] truncate also failed: %s", e2)


# ============================================================================
# BATCHED FLUSH INFRASTRUCTURE (Phase 11 latency optimization)
# Non-critical telemetry/activity/debug-state writes are queued and flushed
# in a single serialized batch every 500ms to reduce I/O churn in hot path.
# Canonical writes (history, accounting, _mut_entry, _mut_post_execution,
# _metrics_bump, last_ws_message_ts, last_candidate_seen_ts) remain synchronous.
# ============================================================================

_BATCHED_TELEMETRY: list[dict] = []
_BATCHED_ACTIVITY: list[dict] = []
_BATCHED_STATE: dict = {}
_BATCHED_FLUSH_INTERVAL_MS = 500
_BATCHED_FLUSH_SCHEDULED = False
_BATCHED_FLUSH_LOCK: Optional[asyncio.Lock] = None


def _get_flush_lock() -> asyncio.Lock:
    global _BATCHED_FLUSH_LOCK
    if _BATCHED_FLUSH_LOCK is None:
        _BATCHED_FLUSH_LOCK = asyncio.Lock()
    return _BATCHED_FLUSH_LOCK


def queue_telemetry_event(event_type: str, mint: str = "", detail: str = "", severity: str = "info", ts: Optional[int] = None):
    """Queue telemetry for batched flush (non-blocking). Use for non-critical HUD events."""
    _BATCHED_TELEMETRY.append({
        "ts": int(ts or time.time()),
        "type": event_type,
        "mint": mint,
        "detail": detail,
        "severity": severity,
    })
    _maybe_schedule_flush()


def queue_recent_activity(action: str, mint: str, live_pnl: float = 0.0, ts: Optional[int] = None):
    """Queue activity for batched flush (non-blocking). Use for non-critical HUD activity feed."""
    stamp = int(ts or time.time())
    _BATCHED_ACTIVITY.append({
        "timestamp": stamp,
        "action": action,
        "mint": mint,
        "live_pnl": float(live_pnl),
    })
    # Also queue corresponding telemetry event
    _BATCHED_TELEMETRY.append({
        "ts": stamp,
        "type": action.replace(" ", "_"),
        "mint": mint,
        "detail": "",
        "severity": "info",
    })
    _maybe_schedule_flush()


def queue_state_update(**kwargs):
    """Queue non-critical state fields for batched flush (debug/diagnostic fields only)."""
    _BATCHED_STATE.update(kwargs)
    _maybe_schedule_flush()


def _maybe_schedule_flush():
    global _BATCHED_FLUSH_SCHEDULED
    if _BATCHED_FLUSH_SCHEDULED:
        return
    _BATCHED_FLUSH_SCHEDULED = True
    try:
        loop = asyncio.get_running_loop()
        loop.call_later(_BATCHED_FLUSH_INTERVAL_MS / 1000.0, lambda: asyncio.create_task(_do_batched_flush()))
    except RuntimeError:
        # No running loop yet (startup); flush will happen on next schedule
        _BATCHED_FLUSH_SCHEDULED = False


async def _do_batched_flush():
    global _BATCHED_FLUSH_SCHEDULED
    async with _get_flush_lock():
        _BATCHED_FLUSH_SCHEDULED = False
        if not _BATCHED_TELEMETRY and not _BATCHED_ACTIVITY and not _BATCHED_STATE:
            return

        telemetry_batch = list(_BATCHED_TELEMETRY)
        activity_batch = list(_BATCHED_ACTIVITY)
        state_batch = dict(_BATCHED_STATE)
        _BATCHED_TELEMETRY.clear()
        _BATCHED_ACTIVITY.clear()
        _BATCHED_STATE.clear()

    def _mut_batched(base):
        if telemetry_batch:
            rows = list(base.get("telemetry_events") or [])
            rows.extend(telemetry_batch)
            base["telemetry_events"] = rows[-3000:]
        if activity_batch:
            rows = list(base.get("recent_activity") or [])
            rows.extend(activity_batch)
            base["recent_activity"] = rows[-300:]
        if state_batch:
            base.update(state_batch)

    await asyncio.to_thread(_locked_state_update, _mut_batched)


async def tg_send(token: str, chat_id: str, text: str):
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json=payload, timeout=6)
    except Exception:
        pass


# PaperTrade, ExecutionIntent, ExecutionResult are defined in models.py and imported above.


class PaperExecutionAdapter:
    def __init__(self, engine: "SimEngine"):
        self.engine = engine

    async def execute(self, intent: ExecutionIntent) -> ExecutionResult:
        t0 = time.perf_counter()
        # P1.10: paper-mode daily loss cap (shares CANARY_MAX_DAILY_LOSS_USD with live).
        # Paper PnL accumulates into daily_loss_usd via record_canary_trade(mode="paper");
        # when the cap is hit, new entries halt until UTC midnight, mirroring live.
        try:
            from live_canary import get_canary_state, CANARY_MAX_DAILY_LOSS_USD
            _daily_state = get_canary_state()
            if _daily_state.daily_loss_usd >= CANARY_MAX_DAILY_LOSS_USD:
                return ExecutionResult(
                    ok=False, mode="paper", tx_sig=None,
                    cu_price=None, cu_limit=None, jito_tip_lamports=None,
                    priority_tier="base", cost_to_trade_usd=0.0,
                    error=f"daily_loss_limit_reached(${_daily_state.daily_loss_usd:.2f}/${CANARY_MAX_DAILY_LOSS_USD:.2f})",
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    extra={"size_usd": round(intent.size_usd, 4)},
                )
        except Exception:
            pass
        sol_usd = await self.engine._sol_usd()
        observed = await self.engine._fetch_priority_fee_p75()

        cu_price = max(CU_PRICE_MIN, min(CU_PRICE_MAX, int(observed)))
        cu_limit = CU_LIMIT_DEFAULT
        tip_lamports = JITO_TIP_LAMPORTS

        tip_usd = (tip_lamports / 1_000_000_000) * sol_usd
        priority_lamports_est = int((cu_price * cu_limit) / 1_000_000)
        priority_usd = (priority_lamports_est / 1_000_000_000) * sol_usd
        projected_cost_to_trade_usd = tip_usd + priority_usd

        total_fees = projected_cost_to_trade_usd

        base_px = float(intent.reference_price_usd or 0.0)
        if base_px <= 0:
            fallback_px = await self.engine._entry_reference_price_with_retries(intent.mint)
            base_px = float(fallback_px or 0.0)
        if base_px <= 0:
            return ExecutionResult(
                ok=False,
                mode="paper",
                tx_sig=None,
                cu_price=cu_price,
                cu_limit=cu_limit,
                jito_tip_lamports=tip_lamports,
                priority_tier=LiveExecutionAdapter._priority_tier(cu_price),
                cost_to_trade_usd=round(projected_cost_to_trade_usd, 6),
                error="paper_entry_price_unavailable",
                latency_ms=(time.perf_counter() - t0) * 1000,
                extra={
                    "tip_usd": round(tip_usd, 6),
                    "priority_usd": round(priority_usd, 6),
                    "fees_usd": round(total_fees, 6),
                    "entry_px": None,
                    "size_usd": round(intent.size_usd, 4),
                    "ix_order": [
                        "0:setComputeUnitPrice",
                        "1:setComputeUnitLimit",
                        "2:buyInstruction",
                        "3:jitoTip",
                    ],
                },
            )

        # Model the 1-3s on-chain confirmation window: live's entry_px=fill_px
        # is observed AFTER the swap settles, so it absorbs intra-window drift.
        # Paper previously used pre-execution reference_price + static slippage,
        # giving paper trades a free pass on entry-side adverse moves. This is
        # the structural source of the 100% paper WR in the 04-24 cohort.
        _entry_px_source = "slippage_markup"
        _fresh_quote_px: Optional[float] = None
        if PAPER_CONFIRMATION_DELAY_SECONDS > 0:
            try:
                await asyncio.sleep(PAPER_CONFIRMATION_DELAY_SECONDS)
            except Exception:
                pass
        if PAPER_FRESH_BUY_QUOTE_ENABLED:
            try:
                from live_canary import get_jupiter_quote, WSOL_MINT as _WSOL_BUY
                _sol_usd_buy = await self.engine._sol_usd()
                if _sol_usd_buy and _sol_usd_buy > 0:
                    _sol_in_lamports = int((intent.size_usd / _sol_usd_buy) * 1_000_000_000)
                    if _sol_in_lamports > 0:
                        _buy_quote = await get_jupiter_quote(
                            input_mint=_WSOL_BUY,
                            output_mint=intent.mint,
                            amount=_sol_in_lamports,
                            slippage_bps=PAPER_FRESH_BUY_QUOTE_SLIPPAGE_BPS,
                        )
                        if _buy_quote:
                            _out_base_units = int(_buy_quote.get("outAmount", 0))
                            if _out_base_units > 0:
                                _TOKEN_DECIMALS = 6
                                _tokens_out = _out_base_units / (10 ** _TOKEN_DECIMALS)
                                if _tokens_out > 0:
                                    _fresh_quote_px = intent.size_usd / _tokens_out
                                    _entry_px_source = "jupiter_buy_quote"
            except Exception as _exc:
                log.warning("[PAPER_BUY] fresh buy quote failed mint=%s: %s", intent.mint, _exc)

        if _fresh_quote_px is not None and _fresh_quote_px > 0:
            entry_px = _fresh_quote_px
        else:
            entry_px = base_px * (1 + intent.max_slippage_pct)
        log.info(
            "[PAPER_BUY] entry_px=%.10g base_px=%.10g slippage_pct=%.4f source=%s delay_s=%.2f real_ref=%s mint=%s",
            entry_px, base_px, intent.max_slippage_pct, _entry_px_source,
            PAPER_CONFIRMATION_DELAY_SECONDS,
            intent.reference_price_is_real, intent.mint,
        )
        qty = intent.size_usd / max(entry_px, 1e-9)

        now_ts = time.time()
        trade = PaperTrade(
            mint=intent.mint,
            ts=now_ts,
            size_usd=intent.size_usd,
            reason=f"migration:{intent.mint[:8]}",
            entry_px=entry_px,
            current_px=entry_px,
            current_px_source="entry_reference",
            last_mark_ts=now_ts,
            high_px=entry_px,
            low_px=entry_px,
            stop_px=entry_px * (1 - DEFAULT_DOWNSIDE_STOP_PCT),  # P3.2: canonical stop at entry
            qty_total=qty,
            qty_remaining=qty,
            fees_usd=total_fees,
            status="open",
            entry_ts=now_ts,
            entry_price=entry_px,
            last_price=entry_px,
            last_price_update_at=0.0,
            mark_price_source="fallback",
            mark_notional_usd_used=0.0,
            mark_notional_size_used=0.0,
            mark_confidence_level="LOW",
            mark_vs_full_liq_diff_pct=None,
            mark_stale=True,
            mark_staleness_seconds=0.0,
            unrealized_roi_pct=0.0,
            realized_roi_pct=0.0,
            exit_ts=0.0,
            exit_price=0.0,
            close_reason="",
            qty_closed=0.0,
            proceeds_usd=0.0,
            cost_basis_total_usd=intent.size_usd,
            remaining_basis_usd=intent.size_usd,
            realized_basis_usd=0.0,
            realized_fees_usd=0.0,
            close_recorded=False,
        )
        # P2.1: atomic insert — reject duplicate-signal races cleanly.
        inserted = await self.engine._atomic_add_trade(intent.mint, trade)
        if not inserted:
            log.warning("[PAPER_BUY] duplicate insert race mint=%s — existing trade kept, this attempt dropped", intent.mint)
            return ExecutionResult(
                ok=False, mode="paper", tx_sig=None,
                cu_price=cu_price, cu_limit=cu_limit, jito_tip_lamports=tip_lamports,
                priority_tier=LiveExecutionAdapter._priority_tier(cu_price),
                cost_to_trade_usd=round(projected_cost_to_trade_usd, 6),
                error="duplicate_insert_race",
                latency_ms=(time.perf_counter() - t0) * 1000,
                extra={"size_usd": round(intent.size_usd, 4)},
            )
        self.engine.budget -= (intent.size_usd + total_fees)

        ts_now = int(time.time())
        persisted = self.engine._persist_successful_entry(intent.mint, intent.size_usd, ts_now)
        if not persisted:
            # Roll back in-memory trade if atomic persistence fails.
            self.engine.trades.pop(intent.mint, None)
            self.engine.budget += (intent.size_usd + total_fees)
            return ExecutionResult(
                ok=False,
                mode="paper",
                tx_sig=None,
                cu_price=cu_price,
                cu_limit=cu_limit,
                jito_tip_lamports=tip_lamports,
                priority_tier=LiveExecutionAdapter._priority_tier(cu_price),
                cost_to_trade_usd=round(projected_cost_to_trade_usd, 6),
                error="state_persist_failed",
                latency_ms=(time.perf_counter() - t0) * 1000,
                extra={
                    "tip_usd": round(tip_usd, 6),
                    "priority_usd": round(priority_usd, 6),
                    "fees_usd": round(total_fees, 6),
                    "entry_px": round(entry_px, 10),
                    "size_usd": round(intent.size_usd, 4),
                    "ix_order": [
                        "0:setComputeUnitPrice",
                        "1:setComputeUnitLimit",
                        "2:buyInstruction",
                        "3:jitoTip",
                    ],
                },
            )

        return ExecutionResult(
            ok=True,
            mode="paper",
            tx_sig=None,
            cu_price=cu_price,
            cu_limit=cu_limit,
            jito_tip_lamports=tip_lamports,
            priority_tier=LiveExecutionAdapter._priority_tier(cu_price),
            cost_to_trade_usd=round(projected_cost_to_trade_usd, 6),
            error=None,
            latency_ms=(time.perf_counter() - t0) * 1000,
            extra={
                "tip_usd": round(tip_usd, 6),
                "priority_usd": round(priority_usd, 6),
                "fees_usd": round(total_fees, 6),
                "entry_px": round(entry_px, 10),
                "size_usd": round(intent.size_usd, 4),
                "ix_order": [
                    "0:setComputeUnitPrice",
                    "1:setComputeUnitLimit",
                    "2:buyInstruction",
                    "3:jitoTip",
                ],
            },
        )


class LiveExecutionAdapter:
    """
    Live Canary Execution Adapter
    
    Uses Jupiter API for swaps and sends transactions via RPC.
    All trades are gated by safety controls in live_canary module.
    """
    def __init__(self, engine: "SimEngine"):
        self.engine = engine
        # Import live canary module
        try:
            from live_canary import (
                can_open_new_position, execute_live_buy, get_canary_trade_size_usd,
                is_trading_enabled, get_wallet_pubkey, SOLANA_AVAILABLE
            )
            self._canary_available = True
            self._can_open = can_open_new_position
            self._execute_buy = execute_live_buy
            self._get_trade_size = get_canary_trade_size_usd
            self._is_enabled = is_trading_enabled
            self._get_wallet = get_wallet_pubkey
            self._solana_ok = SOLANA_AVAILABLE
        except ImportError as e:
            log.warning(f"Live canary module not available: {e}")
            self._canary_available = False
            self._solana_ok = False

    @staticmethod
    def _priority_tier(cu_price: int) -> str:
        if cu_price <= (CU_PRICE_MIN + (CU_PRICE_MAX - CU_PRICE_MIN) * 0.25):
            return "base"
        if cu_price <= (CU_PRICE_MIN + (CU_PRICE_MAX - CU_PRICE_MIN) * 0.7):
            return "burst"
        return "ceiling"

    async def _background_verify_live_buy(self, sig: str, mint: str) -> None:
        """Background task: verify on-chain outcome after optimistic submit return.

        Two-phase:
          Phase 1 — TX outcome: confirmed_success / confirmed_failure / verification_timeout
          Phase 2 — Token balance check (confirmed_success only):
            Wait 4s for RPC propagation, then fetch live token balance.
            If balance > 0  → LIVE_BUY_TOKENS_CONFIRMED — real tokens in wallet.
            If balance == 0 → LIVE_BUY_SWAP_FAILED_NO_TOKENS — TX confirmed but swap
                              failed internally (e.g. slippage exceeded post-sign).
                              Remove the in-memory position so no ghost is tracked.
        """
        try:
            from live_canary import verify_transaction_outcome
            outcome, on_chain_err = await verify_transaction_outcome(sig)
            if outcome == "confirmed_success":
                self.engine._exec_audit["live_tx_verified_success"] += 1
                self.engine._exec_audit["last_tx_verify_state"] = "confirmed_success"
                self.engine._exec_audit["last_tx_verify_err"] = None

                # ── Phase 2: token balance check ──────────────────────────────────
                # A TX can confirm on-chain while the swap fails internally — the RPC
                # instruction succeeds but Jupiter's internal router reverts the swap,
                # leaving the wallet with 0 tokens.  Wait 4s for balance propagation
                # then verify tokens actually arrived before trusting the position.
                await asyncio.sleep(4.0)
                wallet_raw = await self.engine._fetch_live_token_balance(mint)

                if wallet_raw == self.engine._BALANCE_FETCH_ERROR:
                    # Can't confirm either way — log and leave position in place.
                    # Ghost-recon will clean it up if tokens genuinely didn't arrive.
                    log.warning(
                        "[LIVE_BUY_BG] token balance fetch failed after buy mint=%s sig=%s "
                        "— leaving position; ghost-recon will resolve if needed",
                        mint, sig,
                    )
                    append_telemetry_event(
                        "LIVE_BUY_TOKEN_CHECK_FAILED", mint=mint,
                        detail=f"tx={sig} wallet_raw=FETCH_ERROR — ghost-recon deferred",
                        severity="warn",
                    )
                elif wallet_raw > 0:
                    log.warning(
                        "[LIVE_BUY_BG] LIVE_BUY_TOKENS_CONFIRMED mint=%s wallet_raw=%d sig=%s — position live",
                        mint, wallet_raw, sig,
                    )
                    append_telemetry_event(
                        "LIVE_BUY_TOKENS_CONFIRMED", mint=mint,
                        detail=f"tx={sig} wallet_raw={wallet_raw}", severity="info",
                    )
                else:
                    # wallet_raw == 0 at T+4s: TX confirmed but tokens haven't landed yet.
                    # Do NOT remove the position immediately — late-landing swaps have been
                    # observed. Mark GHOST_PENDING and schedule a recheck in
                    # GHOST_BUY_PENDING_RECHECK_SECONDS; the recheck either clears back to
                    # OPEN (late-landed tokens) or finalizes as GHOST_BUY_NO_TOKENS.
                    tr = self.engine.trades.get(mint)
                    if tr is not None and tr.position_truth_state == "OPEN":
                        tr.position_truth_state = "GHOST_PENDING"
                        tr.ghost_pending_since = time.time()
                        log.warning(
                            "[LIVE_BUY_BG] LIVE_BUY_PENDING_NO_TOKENS mint=%s sig=%s — "
                            "wallet_raw=0 after 4s; marking GHOST_PENDING, recheck in %ds",
                            mint, sig, GHOST_BUY_PENDING_RECHECK_SECONDS,
                        )
                        append_telemetry_event(
                            "LIVE_BUY_PENDING_NO_TOKENS", mint=mint,
                            detail=f"tx={sig} wallet_raw=0 after_propagation_wait=4s — GHOST_PENDING recheck={GHOST_BUY_PENDING_RECHECK_SECONDS}s",
                            severity="warn",
                        )
                        asyncio.create_task(self._ghost_pending_recheck(mint, sig, "confirmed_success"))
                # ── End phase 2 ──────────────────────────────────────────────────

            elif outcome == "confirmed_failure":
                self.engine._exec_audit["live_tx_verified_failure"] += 1
                self.engine._exec_audit["last_tx_verify_state"] = "confirmed_failure"
                self.engine._exec_audit["last_tx_verify_err"] = str(on_chain_err)[:80] if on_chain_err else None
                append_telemetry_event("LIVE_BUY_CONFIRMED_FAILURE", mint=mint, detail=f"tx={sig} err={on_chain_err}", severity="error")
                log.error("[LIVE_BUY_BG] confirmed_failure mint=%s sig=%s err=%s", mint, sig, on_chain_err)
            else:
                self.engine._exec_audit["live_tx_verification_timeout"] += 1
                self.engine._exec_audit["last_tx_verify_state"] = "verification_timeout"
                log.warning("[LIVE_BUY_BG] verification_timeout mint=%s sig=%s — checking token balance to resolve", mint, sig)

                # TX status unknown — use wallet balance as ground truth.
                # Wait 4s for RPC propagation then check whether tokens actually arrived.
                await asyncio.sleep(4.0)
                wallet_raw = await self.engine._fetch_live_token_balance(mint)

                if wallet_raw == self.engine._BALANCE_FETCH_ERROR:
                    log.warning(
                        "[LIVE_BUY_BG] verification_timeout + token balance fetch failed mint=%s sig=%s "
                        "— leaving position; ghost-recon will resolve if needed",
                        mint, sig,
                    )
                    append_telemetry_event(
                        "LIVE_BUY_TOKEN_CHECK_FAILED", mint=mint,
                        detail=f"tx={sig} outcome=verification_timeout wallet_raw=FETCH_ERROR — ghost-recon deferred",
                        severity="warn",
                    )
                elif wallet_raw > 0:
                    log.warning(
                        "[LIVE_BUY_BG] LIVE_BUY_TOKENS_CONFIRMED_AFTER_TIMEOUT mint=%s wallet_raw=%d sig=%s — TX succeeded despite timeout; position live",
                        mint, wallet_raw, sig,
                    )
                    append_telemetry_event(
                        "LIVE_BUY_TOKENS_CONFIRMED_AFTER_TIMEOUT", mint=mint,
                        detail=f"tx={sig} wallet_raw={wallet_raw} outcome=verification_timeout_resolved_via_balance",
                        severity="info",
                    )
                else:
                    # wallet_raw == 0 under verification_timeout: same late-landing concern
                    # as the confirmed_success branch — mark GHOST_PENDING and recheck.
                    tr = self.engine.trades.get(mint)
                    if tr is not None and tr.position_truth_state == "OPEN":
                        tr.position_truth_state = "GHOST_PENDING"
                        tr.ghost_pending_since = time.time()
                        log.warning(
                            "[LIVE_BUY_BG] LIVE_BUY_PENDING_NO_TOKENS mint=%s sig=%s — "
                            "verification_timeout + wallet_raw=0 after 4s; GHOST_PENDING recheck in %ds",
                            mint, sig, GHOST_BUY_PENDING_RECHECK_SECONDS,
                        )
                        append_telemetry_event(
                            "LIVE_BUY_PENDING_NO_TOKENS", mint=mint,
                            detail=f"tx={sig} wallet_raw=0 outcome=verification_timeout after_propagation_wait=4s — GHOST_PENDING recheck={GHOST_BUY_PENDING_RECHECK_SECONDS}s",
                            severity="warn",
                        )
                        asyncio.create_task(self._ghost_pending_recheck(mint, sig, "verification_timeout"))

            self.engine._flush_diagnostics()
        except Exception as e:
            log.warning("[LIVE_BUY_BG] verify error mint=%s sig=%s: %s", mint, sig, e)

    async def _ghost_pending_recheck(self, mint: str, sig: str, outcome: str):
        """
        Delayed recheck for positions marked GHOST_PENDING by the buy-verify path.
        After a bounded wait, re-probe wallet balance:
          - tokens present → clear back to OPEN (late-landed swap)
          - wallet_raw == 0 → finalize as GHOST_BUY_NO_TOKENS (original removal path)
          - fetch error    → leave as GHOST_PENDING for periodic ghost-recon to resolve
        """
        try:
            await asyncio.sleep(GHOST_BUY_PENDING_RECHECK_SECONDS)
            tr = self.engine.trades.get(mint)
            if tr is None:
                return
            if tr.position_truth_state != "GHOST_PENDING":
                return
            wallet_raw = await self.engine._fetch_live_token_balance(mint)
            if wallet_raw == self.engine._BALANCE_FETCH_ERROR:
                log.warning(
                    "[LIVE_BUY_BG] ghost_pending_recheck FETCH_ERROR mint=%s sig=%s — leaving GHOST_PENDING for recon",
                    mint, sig,
                )
                return
            if wallet_raw > 0:
                tr.position_truth_state = "OPEN"
                tr.ghost_pending_since = 0.0
                log.warning(
                    "[LIVE_BUY_BG] LIVE_BUY_CONFIRMED_LATE mint=%s wallet_raw=%d sig=%s outcome=%s — cleared GHOST_PENDING",
                    mint, wallet_raw, sig, outcome,
                )
                append_telemetry_event(
                    "LIVE_BUY_CONFIRMED_LATE", mint=mint,
                    detail=f"tx={sig} wallet_raw={wallet_raw} outcome={outcome} recheck={GHOST_BUY_PENDING_RECHECK_SECONDS}s",
                    severity="info",
                )
                return
            # wallet_raw == 0 confirmed after recheck window — original removal path.
            removed = self.engine.trades.pop(mint, None)
            if removed is not None:
                log.error(
                    "[LIVE_BUY_BG] LIVE_BUY_SWAP_FAILED_NO_TOKENS mint=%s sig=%s outcome=%s — "
                    "wallet_raw=0 after %ds recheck; removing ghost position entry_px=%.10g size_usd=%.4f",
                    mint, sig, outcome, GHOST_BUY_PENDING_RECHECK_SECONDS,
                    removed.entry_px, removed.size_usd,
                )
                append_telemetry_event(
                    "LIVE_BUY_SWAP_FAILED_NO_TOKENS", mint=mint,
                    detail=f"tx={sig} wallet_raw=0 outcome={outcome} recheck={GHOST_BUY_PENDING_RECHECK_SECONDS}s — position removed",
                    severity="error",
                )
                if (not removed.canonical_close_emitted
                        and not removed.forced_exit_unresolved
                        and not (removed.exit_ts and removed.exit_ts > 0)):
                    self.engine._finalize_trade_close(removed, "GHOST_BUY_NO_TOKENS")
                self.engine._state_sync()
        except Exception as e:
            log.warning("[LIVE_BUY_BG] ghost_pending_recheck error mint=%s sig=%s: %s", mint, sig, e)

    async def execute(self, intent: ExecutionIntent) -> ExecutionResult:
        t0 = time.perf_counter()
        sol_usd = await self.engine._sol_usd()

        observed = await self.engine._fetch_priority_fee_p75()
        cu_price = max(CU_PRICE_MIN, min(CU_PRICE_MAX, int(observed)))
        cu_limit = CU_LIMIT_DEFAULT
        tip_lamports = JITO_TIP_LAMPORTS
        tip_account = self.engine._jito_tip_account()

        # Estimate execution cost (priority fee + Jito tip)
        priority_lamports_est = int((cu_price * cu_limit) / 1_000_000)
        total_fee_sol = (priority_lamports_est + tip_lamports) / 1_000_000_000
        cost_to_trade_usd = total_fee_sol * sol_usd

        tx_sig = None
        error = None
        fill_px = None  # actual Jupiter fill price; set in live execution path only

        ix_order = [
            "0:jupiterSwap",
            "1:priorityFee(auto)",
        ]

        # Safety gate: check if canary module is available
        if not self._canary_available:
            error = "live_canary_module_unavailable"
        elif not self._solana_ok:
            error = "solana_libraries_unavailable"
        elif not self._is_enabled():
            error = "trading_disabled"
        else:
            # Check safety limits
            open_count = len([t for t in self.engine.trades.values() if t.status == "open"])
            allowed, reason = self._can_open(open_count)
            
            if not allowed:
                error = f"canary_blocked:{reason}"
            else:
                # EXECUTE REAL TRADE
                log.warning(f"[LIVE_CANARY] Executing REAL trade: mint={intent.mint} size=${intent.size_usd:.2f}")

                success, sig, err_msg, fill_px = await self._execute_buy(
                    mint=intent.mint,
                    size_usd=intent.size_usd,
                    sol_price_usd=sol_usd,
                    max_slippage_pct=intent.max_slippage_pct,
                    verify_immediately=False,  # Background verify saves 1.5-3s on critical path
                )
                # Fire background verification immediately after optimistic return
                if success and sig:
                    asyncio.create_task(self._background_verify_live_buy(sig, intent.mint))

                if success:
                    tx_sig = sig
                    log.warning(f"[LIVE_CANARY] Trade SUCCESS: {sig}")

                    # ── LIVE POSITION TRACKING ──────────────────────────────────────
                    # Execution truth confirmed: on-chain outcome verified as success
                    # (meta.err == null). Only now do we create a canonical PaperTrade
                    # so the live position enters the MTM / stop-loss / exit lifecycle.
                    # We do NOT deduct from engine.budget (real capital came from the wallet).
                    # We do NOT roll back on persist failure — the on-chain trade
                    # already happened; keeping the in-memory record is safer.
                    try:
                        now_ts = time.time()
                        # Prefer actual Jupiter fill price over the pre-execution reference
                        # estimate. fill_px = (SOL spent × USD/SOL) / tokens received from
                        # the quote's outAmount — the real executed rate, not the graduation
                        # curve constant that contaminates ~90% of entries otherwise.
                        if fill_px is not None and fill_px > 0:
                            entry_px = fill_px
                            log.warning(
                                "[LIVE_CANARY] entry_px=%.10g SOURCE=jupiter_fill (was reference=%.10g) mint=%s",
                                entry_px, float(intent.reference_price_usd or 0.0), intent.mint,
                            )
                        else:
                            # fill_px unavailable (Jupiter buy quote had outAmount=0 for new pool).
                            # Try a fresh DEX price fetch FIRST (DexScreener + Jupiter price API + RPC
                            # reserves) — these index new pools faster than the buy-quote path.
                            # Only fall back to intent.reference_price_usd (which may be the graduation
                            # curve constant ~6.16e-05) when all live sources are exhausted.
                            fetched = await self.engine._entry_reference_price_with_retries(intent.mint)
                            entry_px = float(fetched or 0.0)
                            if entry_px > 0:
                                log.warning(
                                    "[LIVE_CANARY] entry_px=%.10g SOURCE=dex_price_fetch (fill_px unavailable) mint=%s",
                                    entry_px, intent.mint,
                                )
                            else:
                                entry_px = float(intent.reference_price_usd or 0.0)
                                if entry_px > 0:
                                    log.warning(
                                        "[LIVE_CANARY] entry_px=%.10g SOURCE=reference_price_fallback (dex+fill unavailable) mint=%s",
                                        entry_px, intent.mint,
                                    )
                        # Sentinel fallback — price unknown but position must be tracked.
                        # mark_stale=True + last_price_update_at=0 forces immediate MTM refresh.
                        _entry_px_is_sentinel = False
                        if entry_px <= 0:
                            entry_px = 1e-12
                            _entry_px_is_sentinel = True
                            log.warning("[LIVE_CANARY] entry price unavailable for mint=%s; position tracked with sentinel px, exits BLOCKED until first real mark", intent.mint)

                        qty = intent.size_usd / max(entry_px, 1e-12)
                        live_trade = PaperTrade(
                            mint=intent.mint,
                            ts=now_ts,
                            size_usd=intent.size_usd,
                            reason=f"live_canary:tx:{(tx_sig or 'unknown')[:20]}",
                            decision_id=intent.decision_id,
                            entry_px=entry_px,
                            current_px=entry_px,
                            current_px_source="entry_reference",
                            last_mark_ts=now_ts,
                            high_px=entry_px,
                            low_px=entry_px,
                            stop_px=entry_px * (1 - DEFAULT_DOWNSIDE_STOP_PCT),  # P3.2: canonical stop at entry
                            qty_total=qty,
                            qty_remaining=qty,
                            fees_usd=cost_to_trade_usd,
                            status="open",
                            entry_ts=now_ts,
                            entry_price=entry_px,
                            last_price=entry_px,
                            last_price_update_at=0.0,
                            mark_price_source="fallback",
                            mark_notional_usd_used=0.0,
                            mark_notional_size_used=0.0,
                            mark_confidence_level="LOW",
                            mark_vs_full_liq_diff_pct=None,
                            mark_stale=True,
                            mark_staleness_seconds=0.0,
                            unrealized_roi_pct=0.0,
                            realized_roi_pct=0.0,
                            exit_ts=0.0,
                            exit_price=0.0,
                            close_reason="",
                            qty_closed=0.0,
                            proceeds_usd=0.0,
                            cost_basis_total_usd=intent.size_usd,
                            remaining_basis_usd=intent.size_usd,
                            realized_basis_usd=0.0,
                            realized_fees_usd=0.0,
                            close_recorded=False,
                            entry_px_sentinel=_entry_px_is_sentinel,
                        )
                        # P2.1: atomic insert — duplicate-signal races are real on bursty migrations.
                        # On race, keep the existing trade (first writer wins) and log; the TX itself
                        # already landed on-chain, so this is purely about in-memory dedup.
                        _inserted = await self.engine._atomic_add_trade(intent.mint, live_trade)
                        if not _inserted:
                            log.error("[LIVE_CANARY] duplicate insert race mint=%s tx=%s — in-memory keeps existing, THIS TX's tokens now untracked until reconcile", intent.mint, tx_sig)
                        ts_now_int = int(now_ts)
                        persisted = self.engine._persist_successful_entry(intent.mint, intent.size_usd, ts_now_int)
                        if persisted:
                            log.warning("[LIVE_CANARY] Position tracked: mint=%s entry_px=%.10g size_usd=%.2f qty=%.4g stop_px=%.10g tx=%s",
                                        intent.mint, entry_px, intent.size_usd, qty,
                                        entry_px * (1 - DEFAULT_DOWNSIDE_STOP_PCT), tx_sig)
                        else:
                            log.error("[LIVE_CANARY] CRITICAL: state persist failed for live position mint=%s tx=%s — in-memory record retained", intent.mint, tx_sig)
                    except Exception as _live_track_exc:
                        log.error("[LIVE_CANARY] CRITICAL: position tracking raised %s for mint=%s tx=%s", _live_track_exc, intent.mint, tx_sig)
                    # ── END LIVE POSITION TRACKING ───────────────────────────────────
                else:
                    # Preserve the submitted sig for audit even on failure
                    if sig:
                        tx_sig = sig
                    error = err_msg or "live_execution_failed"
                    log.error(f"[LIVE_CANARY] Trade FAILED: {error}")

                # ── EXECUTION TRUTH TELEMETRY ──────────────────────────────────
                # Emit verification-state telemetry based on error string pattern.
                # success=True → confirmed_success; error prefix distinguishes failure mode.
                if success:
                    append_telemetry_event(
                        "LIVE_BUY_CONFIRMED_SUCCESS", mint=intent.mint,
                        detail=f"tx={tx_sig}", severity="info",
                    )
                elif error and error.startswith("tx_failed_on_chain:"):
                    on_chain_err_detail = error[len("tx_failed_on_chain:"):]
                    append_telemetry_event(
                        "LIVE_BUY_CONFIRMED_FAILURE", mint=intent.mint,
                        detail=f"tx={tx_sig} err={on_chain_err_detail}", severity="error",
                    )
                elif error == "tx_verification_timeout":
                    append_telemetry_event(
                        "LIVE_BUY_VERIFICATION_TIMEOUT", mint=intent.mint,
                        detail=f"tx={tx_sig}", severity="error",
                    )
                # ── END EXECUTION TRUTH TELEMETRY ─────────────────────────────

        return ExecutionResult(
            ok=error is None,
            mode="live_canary",
            tx_sig=tx_sig,
            cu_price=cu_price,
            cu_limit=cu_limit,
            jito_tip_lamports=tip_lamports,
            priority_tier=self._priority_tier(cu_price),
            cost_to_trade_usd=round(cost_to_trade_usd, 6),
            error=error,
            latency_ms=(time.perf_counter() - t0) * 1000,
            extra={
                "tip_account": tip_account,
                "ix_order": ix_order,
                "priority_fee_p75_observed": int(observed),
                "size_usd": round(intent.size_usd, 4),
                "wallet": self._get_wallet() if self._canary_available else None,
                "trading_enabled": self._is_enabled() if self._canary_available else False,
                # Jupiter fill price fields — written to execution_attempt history event.
                # fill_price_usd: actual (SOL_spent × SOL/USD) / tokens_received from quote.
                # reference_price_is_real: True when fill_price_usd used; False = graduation curve.
                "fill_price_usd": round(fill_px, 12) if fill_px is not None and fill_px > 0 else None,
                "reference_price_is_real": (fill_px is not None and fill_px > 0) or intent.reference_price_is_real,
            },
        )


class SimEngine:
    def __init__(self):
        self.budget = MAX_TEST_BUDGET_USD
        self.ws_idx = 0
        self.rpc_idx = 0
        self.rpc_429_streak = 0
        self.trades: Dict[str, PaperTrade] = {}
        # P2.1: serialize check-then-insert on self.trades so two concurrent signals
        # for the same mint can't both pass "not in self.trades" and double-insert,
        # losing PnL tracking on one of them.
        self._trades_lock: asyncio.Lock = asyncio.Lock()
        # P2.2: per-mint sell serialization. Two sells on the same mint (e.g. TP
        # stage-2 firing while HARD_CAP also triggers) would double-deduct qty and
        # submit overlapping Jupiter quotes at stale sizes. Each sell acquires
        # the mint-keyed lock before quote → sign → submit.
        self._sell_locks: Dict[str, asyncio.Lock] = {}
        self.closed_trades: list[dict] = [x for x in load_history() if x.get("type") == "trade_close"]
        self._last_graveyard_refresh = 0

        self.start_ts = int(time.time())
        self.current_liquidity_usd = MIN_LIQUIDITY_USD
        self.paper_buys_under_lowered_threshold = 0
        self.daily_seen_mints: set[str] = set()
        self.seen_candidate_keys: OrderedDict[str, int] = OrderedDict()
        self._dup_skip_last_emit: dict[str, int] = {}
        self._sig_parse_cache: OrderedDict[str, tuple[Optional[str], str, str]] = OrderedDict()
        self._sig_timeout_cooldown: dict[str, float] = {}
        self._terminal_timeout_suppression: dict[str, float] = {}
        self._dependency_retry_cooldown: dict[str, float] = {}
        self._rpc_timeout_sig_counts: dict[str, int] = {}
        self._candidate_seen_sig_counts: dict[str, int] = {}
        self._mark_price_cache: dict[str, tuple[float, float]] = {}  # mint → (price, timestamp_ms)
        # Negative cache for reserve-owner misses: mint → (first_miss_ts, miss_count)
        # Suppresses repeated getTokenAccountsByOwner fan-outs on the same mint within
        # RESERVE_MISS_SUPPRESS_SECONDS. Cleared automatically when TTL expires.
        self._reserve_miss_cache: dict[str, tuple[float, int]] = {}  # mint → (ts, count)
        # Per-mint latch for ghost-recon safety block (no confirmed sell found).
        # Prevents _ghost_reconciliation_terminalize from re-disabling new entries and
        # re-emitting telemetry every GHOST_RECON_INTERVAL_SECONDS (30s) for the same
        # unresolved ghost. The block is still applied on first detection; subsequent
        # periodic cycles for the same mint are silent and skip the state mutation.
        self._ghost_recon_no_sell_mints: set[str] = set()
        self._last_snapshot_ts: float = 0.0
        # P0-2: In-memory WS timestamp cache — flushed to disk every 5s via
        # _ws_state_flush_interval.  Eliminates per-message disk I/O on the hot path.
        self._ws_last_message_ts: int = 0
        self._ws_last_candidate_ts: int = 0
        self._ws_last_heartbeat_ts: int = 0
        self._ws_last_subscribe_ts: int = 0
        self._ws_state_last_flush: float = 0.0
        self._ws_state_flush_interval: float = 5.0  # seconds
        self._ws_cached_state: Optional[dict] = None
        self._ws_cached_state_ts: float = 0.0
        # P4-2: Per-endpoint health tracking for weighted WS selection
        self._ws_endpoint_stats: dict[str, dict] = {}  # url → {connects, disconnects, uptime_sum, last_fail_ts}
        self._ws_consecutive_failures: int = 0  # P4-3: for exponential backoff
        self._migration_retry_queue: asyncio.Queue = asyncio.Queue(maxsize=MIGRATION_RETRY_QUEUE_MAX)
        # Entry score pre-fetch tasks keyed by mint. Task kicked off at T=0 (migration
        # enqueued), awaited at T+30 (delayed entry dequeued). Pop after consumption.
        self._migration_scores: dict[str, asyncio.Task] = {}
        self._score_debug_logged = 0  # temporary: first 3 tokens per session get SCORE_DEBUG log
        self._parse_timeout_suppressed_count = 0
        self._post_timeout_candidate_reseen_count = 0
        self._candidate_seen_after_ws_reconnect_count = 0
        self.parser_debug_events_left = 5
        self._log_sample_count = 0          # how many raw log samples written so far
        self._log_sample_max = int(os.getenv("LOG_SAMPLE_MAX", "20"))  # write first N events to debug file

        self.execution_mode = EXECUTION_MODE
        self.live_trade_attempts = 0

        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=EVENT_QUEUE_MAX)
        self.event_workers = EVENT_WORKERS
        self._workers_started = False
        self._seen_lock = asyncio.Lock()
        self._mtm_semaphore = asyncio.Semaphore(MTM_REFRESH_CONCURRENCY)
        self._open_position_next_due: dict[str, float] = {}
        self._finalize_hot_until = 0.0

        self.paper_adapter = PaperExecutionAdapter(self)
        self.live_adapter = LiveExecutionAdapter(self)
        self._http_session: Optional[aiohttp.ClientSession] = None

        st = read_state()
        dm = st.get("daily_metrics") or default_metrics(utc_day())
        persisted_start_ts = int((dm.get("session_start_ts") if isinstance(dm, dict) else 0) or st.get("process_start_ts") or 0)
        if persisted_start_ts > 0:
            self.start_ts = persisted_start_ts
        if dm.get("day_utc") != utc_day():
            dm = default_metrics(utc_day())
            self.start_ts = int(dm.get("session_start_ts") or time.time())
        st["daily_metrics"] = dm
        st["all_time_metrics"] = st.get("all_time_metrics") or default_metrics("all-time")
        self.live_trade_attempts = int(st.get("live_trade_attempts", 0) or 0)
        self.budget = float(st.get("paper_budget_left", self.budget) or self.budget)
        self._restore_paper_positions_from_state(st)
        current_paper_rows = self._paper_positions_snapshot()
        current_capital_rows = self._capital_open_position_rows(current_paper_rows, include_stale=True)
        self._ensure_canonical_capital_fields(st, rows=current_paper_rows, cash_budget=self.budget)
        current_canonical_unrealized_usd = round(sum(float(row.get("unrealized_pnl_usd") or 0.0) for row in current_capital_rows), 6)
        current_canonical_capital_usd = round(
            float(st.get("canonical_start_capital_usd") or 0.0)
            + float(st.get("canonical_realized_usd") or 0.0)
            + current_canonical_unrealized_usd,
            6,
        )
        current_session_window_budget_usd = round(float(st.get("session_window_budget_usd") or st.get("session_start_budget_usd") or MAX_TEST_BUDGET_USD), 6)

        write_state(
            daily_metrics=st["daily_metrics"],
            all_time_metrics=st["all_time_metrics"],
            last_ws_heartbeat_ts=int(st.get("last_ws_heartbeat_ts", 0) or 0),
            last_ws_message_ts=int(st.get("last_ws_message_ts", 0) or 0),
            last_candidate_seen_ts=int(st.get("last_candidate_seen_ts", 0) or 0),
            last_successful_subscribe_ts=int(st.get("last_successful_subscribe_ts", 0) or 0),
            operator_controls={
                "default_downside_stop_pct": DEFAULT_DOWNSIDE_STOP_PCT,
                "absolute_failsafe_stop_pct": ABSOLUTE_FAILSAFE_STOP_PCT,
                **(st.get("operator_controls") or {}),
                "disable_new_entries": False,  # safety reset: always start with entries enabled
            },
            process_start_ts=self.start_ts,
            min_liquidity_usd_active=self.current_liquidity_usd,
            execution_mode=self.execution_mode,
            live_trade_attempts=self.live_trade_attempts,
            canonical_start_capital_usd=round(float(st.get("canonical_start_capital_usd") or 0.0), 6),
            canonical_realized_usd=round(float(st.get("canonical_realized_usd") or 0.0), 6),
            canonical_unrealized_usd=round(current_canonical_unrealized_usd, 6),
            canonical_capital_usd=round(current_canonical_capital_usd, 6),
            session_window_budget_usd=round(current_session_window_budget_usd, 6),
            **({"sim_budget_usd": round(current_canonical_capital_usd, 6)} if self.execution_mode != "live" else {}),
            event_queue_max=EVENT_QUEUE_MAX,
            event_workers=self.event_workers,
            ws_endpoint_count=len(WS_URLS),
            rpc_endpoint_count=len(RPC_URLS),
            ws_redundancy_ok=(len(WS_URLS) >= 2),
            execution_mode_requested=os.getenv("EXECUTION_MODE", "paper").strip().lower(),
            live_arm_ack_present=(LIVE_ARM_ACK == "YES_I_UNDERSTAND_LIVE_RISK"),
            startup_profile={
                "current_profile": ACTIVE_PROFILE,
                "current_mode": EXECUTION_MODE,
                "active_env_source": _ACTIVE_ENV_SOURCE,
                "lock_policy": LOCK_POLICY,
                "research_lp_lock_pct_min": RESEARCH_LP_LOCK_PCT_MIN if LOCK_POLICY == "lp_pct_threshold" else None,
                "live_arm_ack_present": LIVE_ARM_ACK == "YES_I_UNDERSTAND_LIVE_RISK",
                "real_wallet_active": EXECUTION_MODE == "live",
                "sim_budget_authoritative": EXECUTION_MODE != "live",
                "live_tx_permitted": EXECUTION_MODE == "live",
                "startup_ts": int(time.time()),
            },
        )
        if len(WS_URLS) < 2:
            append_telemetry_event("WS_REDUNDANCY_LOW", detail=f"ws_endpoints={len(WS_URLS)}", severity="warn")

        # ── Instrumentation: in-memory diagnostic accumulators ──────────────
        self._funnel = {
            "raw_candidates_seen": 0, "qualified_candidates": 0,
            "parsed_successfully": 0, "parse_failed": 0,
            "veto_liquidity": 0, "veto_price": 0, "veto_dependency": 0, "veto_other": 0,
            "entry_eligible": 0, "execution_attempted": 0,
            "execution_succeeded": 0, "execution_failed": 0,
            # Migration retry / delayed entry telemetry (Task 4)
            "migration_retry_enqueued": 0, "migration_retry_attempts_total": 0,
            "migration_retry_price_found": 0, "migration_retry_exhausted_count": 0,
            "migration_retry_missing_ref_price_count": 0,
            "delayed_entry_enqueued": 0, "delayed_entry_price_alive": 0,
            "delayed_entry_dump_discarded": 0,
            "settle_window_rejected": 0,
            # Reserve discovery suppression telemetry
            "reserve_miss_total": 0,
            "reserve_miss_suppressed": 0,
            "reserve_miss_unique_mints": 0,
            # Session start timestamp — set once at startup, preserved across flushes
            "funnel_session_start_ts": 0,
            # P2-1 / P2-3: blocked-entry breakdown (separate from execution failures)
            "entry_blocked_trading_disabled": 0,
            "entry_blocked_duplicate": 0,
            "entry_blocked_budget": 0,
        }
        # P0-3: Restore persisted funnel counters so restart does not zero them.
        # Only numeric fields are restored; funnel_session_start_ts is always fresh.
        try:
            _saved_funnel = (read_state().get("entry_funnel") or {})
            for _k in list(self._funnel.keys()):
                if _k == "funnel_session_start_ts":
                    continue
                if _k in _saved_funnel and isinstance(_saved_funnel[_k], (int, float)):
                    self._funnel[_k] = int(_saved_funnel[_k])
        except Exception:
            pass
        self._funnel["funnel_session_start_ts"] = int(time.time())
        self._terminal_blockers = {
            "liquidity_unlocked": 0, "rpc_timeout": 0, "parse_low_confidence": 0,
            "excluded_mint": 0, "dependency_unavailable": 0, "queue_stale": 0,
            "execution_blocked": 0, "execution_failed": 0, "duplicate": 0, "other": 0,
        }
        self._exec_audit = {
            "execution_attempts": 0, "execution_prechecks_passed": 0, "execution_blocked": 0,
            "live_submit_attempts": 0, "live_submit_success": 0, "live_submit_failures": 0,
            # Execution-truth verification counters (post-submission on-chain outcome)
            "live_tx_verified_success": 0,   # confirmed_success: meta.err == null
            "live_tx_verified_failure": 0,   # confirmed_failure: meta.err non-null
            "live_tx_verification_timeout": 0,  # uncertain: could not confirm in window
            "last_tx_verify_state": None,    # last verification outcome string
            "last_tx_verify_err": None,      # last on-chain err dict (confirmed_failure only)
            "last_failure_reason": None, "last_tx_signature": None,
            "avg_submit_latency_ms": 0.0, "_submit_latency_sum": 0.0, "_submit_count": 0,
        }
        self._timing = {
            "avg_time_seen_to_parse_ms": 0.0, "avg_time_parse_to_decision_ms": 0.0,
            "avg_time_decision_to_execution_ms": 0.0,
            "stale_before_parse": 0, "stale_before_decision": 0, "stale_before_execution": 0,
            "total_samples": 0,
            "_seen_to_parse_sum": 0.0, "_parse_to_decision_sum": 0.0, "_decision_to_exec_sum": 0.0,
        }
        self._rpc_health = {"endpoints": {}, "total_requests": 0, "total_timeouts": 0, "total_successes": 0}

        # P5-1: per-task timing accumulators for the 5 parallel decision checks
        self._timing["_decision_samples"] = 0
        self._timing["_mutable_ms_sum"] = 0.0
        self._timing["_price_ms_sum"] = 0.0
        self._timing["_rug_ms_sum"] = 0.0
        self._timing["_holder_ms_sum"] = 0.0
        self._timing["_onchain_ms_sum"] = 0.0
        # P6.3: reset hourly so avg_* reflect recent behavior, not process lifetime.
        self._timing_window_start_ts = time.time()

    def _maybe_roll_timing_window(self):
        if time.time() - getattr(self, "_timing_window_start_ts", 0.0) < 3600:
            return
        samples = max(1, self._timing.get("total_samples", 0))
        _dsamp = max(1, self._timing.get("_decision_samples", 0))
        log.info(
            "[TIMING_HOURLY] samples=%d avg_seen_to_parse=%.1fms avg_parse_to_decision=%.1fms "
            "avg_decision_to_exec=%.1fms avg_mutable=%.1fms avg_price=%.1fms avg_rug=%.1fms "
            "avg_holder=%.1fms avg_onchain=%.1fms",
            self._timing.get("total_samples", 0),
            self._timing.get("_seen_to_parse_sum", 0.0) / samples,
            self._timing.get("_parse_to_decision_sum", 0.0) / _dsamp,
            self._timing.get("_decision_to_exec_sum", 0.0) / samples,
            self._timing.get("_mutable_ms_sum", 0.0) / _dsamp,
            self._timing.get("_price_ms_sum", 0.0) / _dsamp,
            self._timing.get("_rug_ms_sum", 0.0) / _dsamp,
            self._timing.get("_holder_ms_sum", 0.0) / _dsamp,
            self._timing.get("_onchain_ms_sum", 0.0) / _dsamp,
        )
        for k in (
            "_seen_to_parse_sum", "_parse_to_decision_sum", "_decision_to_exec_sum",
            "_mutable_ms_sum", "_price_ms_sum", "_rug_ms_sum",
            "_holder_ms_sum", "_onchain_ms_sum",
        ):
            self._timing[k] = 0.0
        for k in ("total_samples", "_decision_samples",
                  "stale_before_parse", "stale_before_decision", "stale_before_execution"):
            self._timing[k] = 0
        self._timing_window_start_ts = time.time()

    def _flush_diagnostics(self):
        self._maybe_roll_timing_window()
        """Non-blocking flush of all in-memory diagnostic structures to runtime_state."""
        n_req = max(1, self._rpc_health["total_requests"])
        n_to = self._rpc_health["total_timeouts"]
        current_rpc = getattr(self, "rpc_url", "")
        rpc_health_out = {
            "endpoints": self._rpc_health["endpoints"],
            "current_rpc_url": current_rpc,
            "timeout_rate_pct": round((n_to / n_req) * 100.0, 2) if n_req > 0 else 0.0,
            "total_requests": self._rpc_health["total_requests"],
            "total_timeouts": n_to,
        }
        samples = max(1, self._timing["total_samples"])
        _dsamp = max(1, self._timing.get("_decision_samples", 0))
        timing_out = {
            "avg_time_seen_to_parse_ms": round(self._timing["_seen_to_parse_sum"] / samples, 2),
            "avg_time_parse_to_decision_ms": round(self._timing["_parse_to_decision_sum"] / _dsamp, 2),  # use decision_samples for apples-to-apples with task metrics
            "avg_time_decision_to_execution_ms": round(self._timing["_decision_to_exec_sum"] / samples, 2),
            "stale_before_parse": self._timing["stale_before_parse"],
            "stale_before_decision": self._timing["stale_before_decision"],
            "stale_before_execution": self._timing["stale_before_execution"],
            "total_samples": self._timing["total_samples"],
            # P5-1: per-task average latencies (wall-clock blocked waiting for each parallel task)
            "decision_task_samples": self._timing.get("_decision_samples", 0),
            "avg_mutable_task_ms": round(self._timing.get("_mutable_ms_sum", 0.0) / _dsamp, 3),
            "avg_price_task_ms": round(self._timing.get("_price_ms_sum", 0.0) / _dsamp, 3),
            "avg_rug_task_ms": round(self._timing.get("_rug_ms_sum", 0.0) / _dsamp, 3),
            "avg_holder_task_ms": round(self._timing.get("_holder_ms_sum", 0.0) / _dsamp, 3),
            "avg_onchain_task_ms": round(self._timing.get("_onchain_ms_sum", 0.0) / _dsamp, 3),
        }
        exec_audit_out = {k: v for k, v in self._exec_audit.items() if not k.startswith("_")}
        queue_state_update(
            entry_funnel=dict(self._funnel),
            terminal_blockers_summary=dict(self._terminal_blockers),
            execution_audit=exec_audit_out,
            candidate_timing=timing_out,
            rpc_health=rpc_health_out,
        )

    @property
    def ws_url(self):
        return WS_URLS[self.ws_idx]

    def rotate_ws(self):
        # P4-2: Health-weighted endpoint selection — prefer endpoints with
        # highest uptime ratio and apply 30s cooldown for recently-failed ones.
        now = time.time()
        best_idx = (self.ws_idx + 1) % len(WS_URLS)
        if len(WS_URLS) > 1:
            best_score = -1.0
            for i, url in enumerate(WS_URLS):
                if i == self.ws_idx:
                    continue  # skip the one that just failed
                stats = self._ws_endpoint_stats.get(url, {})
                last_fail = stats.get("last_fail_ts", 0)
                if now - last_fail < 30:
                    continue  # cooldown period — skip recently failed
                connects = max(1, stats.get("connects", 1))
                disconnects = stats.get("disconnects", 0)
                score = (connects - disconnects) / connects  # uptime ratio
                if score > best_score:
                    best_score = score
                    best_idx = i
            # If all are on cooldown, fall back to simple round-robin
            if best_score < 0:
                best_idx = (self.ws_idx + 1) % len(WS_URLS)
        # Record failure for current endpoint
        cur_url = WS_URLS[self.ws_idx]
        cur_stats = self._ws_endpoint_stats.setdefault(cur_url, {"connects": 0, "disconnects": 0, "last_fail_ts": 0})
        cur_stats["disconnects"] += 1
        cur_stats["last_fail_ts"] = now
        self.ws_idx = best_idx
        # Record connect attempt for new endpoint
        new_url = WS_URLS[self.ws_idx]
        new_stats = self._ws_endpoint_stats.setdefault(new_url, {"connects": 0, "disconnects": 0, "last_fail_ts": 0})
        new_stats["connects"] += 1
        log.warning(f"WS failover -> {self.ws_url}")

    @property
    def rpc_url(self):
        return RPC_URLS[self.rpc_idx]

    def rotate_rpc(self):
        self.rpc_idx = (self.rpc_idx + 1) % len(RPC_URLS)
        log.warning(f"RPC failover -> {self.rpc_url}")

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=15, connect=3, sock_connect=3, sock_read=12)
            try:
                resolver = aiohttp.AsyncResolver()
            except Exception:
                resolver = aiohttp.ThreadedResolver()
            connector = aiohttp.TCPConnector(limit=64, ttl_dns_cache=300, ssl=False, resolver=resolver)
            self._http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self._http_session

    async def _rpc_post(self, body: dict, timeout: float = 8.0):
        s = await self._get_http_session()
        ct = aiohttp.ClientTimeout(total=timeout, connect=timeout, sock_connect=timeout, sock_read=timeout)
        _t0 = time.perf_counter()
        _t_headers: float = -1.0

        async def _do_request() -> tuple:
            async with s.post(self.rpc_url, json=body, timeout=ct) as r:
                nonlocal _t_headers
                _t_headers = time.perf_counter()
                data = await r.json()
                return r.status, data

        # Python 3.11+ changed asyncio.wait() so that when the caller is cancelled it
        # cancels all pending inner tasks and WAITS for their teardown before propagating.
        # A slow public RPC (publicnode.com) takes 7-8s to close its TCP socket, causing
        # the outer parse-budget CancelledError to block the worker for 7-8s instead of
        # releasing immediately.  Fix: use asyncio.shield so the shield—not _req_task—is
        # cancelled on timeout/cancel.  Shield teardown is instant; _req_task drains in bg.
        _req_task = asyncio.ensure_future(_do_request())
        try:
            status, data = await asyncio.wait_for(asyncio.shield(_req_task), timeout=timeout)
        except asyncio.TimeoutError:
            # Hard wall-clock timeout — cancel subtask in background, release worker now
            if not _req_task.done():
                _req_task.cancel()
            _t_exc = time.perf_counter()
            log.warning(
                "[RPC_PHASE] hard_timeout method=%s exc_total_ms=%.0f rpc=%s",
                (body or {}).get("method", "?"),
                (_t_exc - _t0) * 1000,
                self.rpc_url,
            )
            # instrumentation: rpc_health timeout tracking
            _rpc_ep = self.rpc_url
            _rpc_ep_rec = self._rpc_health["endpoints"].setdefault(_rpc_ep, {"requests": 0, "successes": 0, "timeouts": 0, "errors": 0, "avg_latency_ms": 0.0, "_latency_sum": 0.0, "last_timeout_ts": None})
            _rpc_ep_rec["requests"] += 1
            _rpc_ep_rec["timeouts"] += 1
            _rpc_ep_rec["last_timeout_ts"] = int(time.time())
            self._rpc_health["total_requests"] += 1
            self._rpc_health["total_timeouts"] += 1
            raise
        except BaseException as _exc:
            # CancelledError from outer budget or other external cancel
            if not _req_task.done():
                _req_task.cancel()
            _t_exc = time.perf_counter()
            log.warning(
                "[RPC_PHASE] exc=%s method=%s headers_ms=%d phase=%s exc_total_ms=%.0f rpc=%s",
                type(_exc).__name__,
                (body or {}).get("method", "?"),
                round((_t_headers - _t0) * 1000) if _t_headers >= 0 else -1,
                "body_read" if _t_headers >= 0 else "pre_headers",
                (_t_exc - _t0) * 1000,
                self.rpc_url,
            )
            # instrumentation: rpc_health error tracking
            _rpc_ep = self.rpc_url
            _rpc_ep_rec = self._rpc_health["endpoints"].setdefault(_rpc_ep, {"requests": 0, "successes": 0, "timeouts": 0, "errors": 0, "avg_latency_ms": 0.0, "_latency_sum": 0.0, "last_timeout_ts": None})
            _rpc_ep_rec["requests"] += 1
            _rpc_ep_rec["errors"] += 1
            self._rpc_health["total_requests"] += 1
            raise
        _t_done = time.perf_counter()
        _total_ms = (_t_done - _t0) * 1000
        if _total_ms > 300:
            log.warning(
                "[RPC_PHASE] ok method=%s headers_ms=%.0f body_ms=%.0f total_ms=%.0f rpc=%s",
                (body or {}).get("method", "?"),
                (_t_headers - _t0) * 1000,
                (_t_done - _t_headers) * 1000 if _t_headers >= 0 else -1,
                _total_ms,
                self.rpc_url,
            )
        # instrumentation: rpc_health success tracking
        _rpc_ep = self.rpc_url
        _rpc_ep_rec = self._rpc_health["endpoints"].setdefault(_rpc_ep, {"requests": 0, "successes": 0, "timeouts": 0, "errors": 0, "avg_latency_ms": 0.0, "_latency_sum": 0.0, "last_timeout_ts": None})
        _rpc_ep_rec["requests"] += 1
        _rpc_ep_rec["successes"] += 1
        _rpc_ep_rec["_latency_sum"] = _rpc_ep_rec.get("_latency_sum", 0.0) + _total_ms
        _rpc_ep_rec["avg_latency_ms"] = round(_rpc_ep_rec["_latency_sum"] / max(1, _rpc_ep_rec["successes"]), 2)
        self._rpc_health["total_requests"] += 1
        self._rpc_health["total_successes"] += 1
        return status, data

    def _should_terminalize_trade(self, tr: PaperTrade) -> bool:
        if tr.closed:
            return False
        age_seconds = max(0.0, time.time() - float(tr.entry_ts or tr.ts or time.time()))
        mark_value_usd = max(0.0, float(tr.qty_remaining or 0.0) * float(tr.current_px or tr.last_price or tr.entry_px or 0.0))
        residual_value_usd = max(float(tr.remaining_basis_usd or 0.0), mark_value_usd)
        return (
            age_seconds >= TERMINAL_STALE_POSITION_AGE_SECONDS
            and residual_value_usd <= TERMINAL_STALE_POSITION_VALUE_USD
            and float(tr.qty_remaining or 0.0) > 0.0
        )

    def _terminalize_trade(self, tr: PaperTrade, reason: str = "terminal_stale_dust") -> bool:
        if tr.closed or not self._should_terminalize_trade(tr):
            return False
        now_ts = int(time.time())
        mark_px = float(tr.current_px or tr.last_price or tr.entry_px or 0.0)
        market_value_usd = max(0.0, float(tr.qty_remaining or 0.0) * mark_px) if mark_px > 0 else 0.0
        archive_row = {
            "mint": tr.mint,
            "status": tr.status,
            "entry_ts": int(tr.entry_ts or tr.ts or 0),
            "terminalized_ts": now_ts,
            "reason": reason,
            "qty_remaining": round(float(tr.qty_remaining or 0.0), 6),
            "remaining_basis_usd": round(float(tr.remaining_basis_usd or 0.0), 6),
            "market_value_usd": round(market_value_usd, 6),
            "mark_stale": bool(tr.mark_stale),
            "mark_staleness_seconds": round(float(tr.mark_staleness_seconds or 0.0), 3),
        }

        def _mut(base):
            rows = list(base.get("terminal_positions") or [])
            rows.append(archive_row)
            base["terminal_positions"] = rows[-200:]

        _locked_state_update(_mut)
        append_history({"type": "terminal_position", **archive_row})
        append_telemetry_event("POSITION_TERMINALIZED", mint=tr.mint, detail=f"reason={reason}", severity="warn")
        self.trades.pop(tr.mint, None)
        self._open_position_next_due.pop(tr.mint, None)
        return True

    def _ghost_reconciliation_terminalize(self, tr: PaperTrade) -> bool:
        """
        Terminalize a ghost position — one whose wallet balance is confirmed zero
        (buy TX failed on-chain, or tokens already exited some other way).

        SAFETY GUARD: Only terminalize if a confirmed sell/close record exists in
        history.json. If no confirmed sell is found, block new entries instead of
        silently removing the position — this prevents hiding live losses.

        This is a reconciliation-safety operation, NOT a close or sell:
          - Does NOT submit any on-chain TX
          - Does NOT modify realized_pnl
          - Does NOT modify self.budget
          - Does NOT write a trade_close to history.json
          - Writes a terminal_position audit record (type=ghost_reconciliation_terminal)
          - Pops the trade from self.trades so it is no longer managed
          - State synced by caller
        """
        # GHOST_PENDING guard: if the position is in GHOST_PENDING state, the dedicated
        # ghost_pending_recheck task (line 1382+) owns resolution. It sleeps
        # GHOST_BUY_PENDING_RECHECK_SECONDS, then either promotes to OPEN (LIVE_BUY_CONFIRMED_LATE)
        # or pops the trade cleanly via LIVE_BUY_SWAP_FAILED_NO_TOKENS.
        # Do NOT let periodic recon fire SAFETY BLOCK on a position that is already being
        # actively reconciled — that race was the source of intermittent "entries disabled".
        if tr.position_truth_state == "GHOST_PENDING":
            return False
        # Require a confirmed close record before terminalizing.
        # If wallet shows 0 but no sell exists, the position may be miscounted —
        # block new entries and log an error rather than silently removing it.
        hist_rows = load_history()
        mint_rows = [r for r in hist_rows if str(r.get("mint") or "") == tr.mint]
        confirmed_sell = any(
            str(r.get("type") or "") in ("trade_close", "partial_realize")
            for r in mint_rows
        )
        if not confirmed_sell:
            # Auto-terminalize unresolvable ghosts: wallet=0, no confirmed sell, but
            # position has been stale/unpriced for GHOST_UNRESOLVED_AUTO_TERM_AGE_SECONDS.
            # This avoids permanently blocking new entries for a dead position that had a
            # failed buy TX or externally-sold tokens.
            position_age_s = max(0.0, time.time() - float(tr.entry_ts or tr.ts or time.time()))
            is_auto_terminalizable = (
                bool(tr.mark_stale or tr.mark_staleness_seconds >= STALE_MARK_EXIT_TIMEOUT_SECONDS)
                and position_age_s >= GHOST_UNRESOLVED_AUTO_TERM_AGE_SECONDS
                and tr.forced_exit_attempt_count >= FORCED_EXIT_MAX_ATTEMPTS
            )
            if is_auto_terminalizable:
                log.error(
                    "[GHOST_RECON] AUTO_TERMINALIZE — mint=%s wallet=0 no-sell age=%.0fs stale attempts=%d — unresolvable, terminalizing as GHOST_UNRESOLVED",
                    tr.mint, position_age_s, tr.forced_exit_attempt_count,
                )
                append_telemetry_event(
                    "GHOST_UNRESOLVED_AUTO_TERMINALIZE", mint=tr.mint,
                    detail=f"size_usd={tr.size_usd:.4f} qty_remaining={tr.qty_remaining:.4f} age_s={position_age_s:.0f} attempts={tr.forced_exit_attempt_count} — no confirmed sell, stale, forced-exit exhausted",
                    severity="critical",
                )
                tr.position_truth_state = "GHOST_UNRESOLVED"
                tr.forced_exit_unresolved = True
                # Fall through to terminalize below (skip the confirmed_sell requirement)
            elif tr.mint not in self._ghost_recon_no_sell_mints:
                # First time this mint triggers the safety block — log, emit telemetry,
                # and set the entry disable. Add to latch so subsequent periodic passes
                # skip all of this (the state is already set; re-running every 30s just
                # spams the log and flip-flops the HUD button).
                self._ghost_recon_no_sell_mints.add(tr.mint)
                log.error(
                    "[GHOST_RECON] SAFETY BLOCK — mint=%s wallet=0 but NO confirmed sell in history. "
                    "Refusing to terminalize. Blocking new entries. Investigate manually.",
                    tr.mint,
                )
                append_telemetry_event(
                    "GHOST_RECON_BLOCKED_NO_CONFIRMED_SELL", mint=tr.mint,
                    detail=f"size_usd={tr.size_usd:.4f} qty_remaining={tr.qty_remaining:.4f} — new entries disabled",
                    severity="error",
                )
                # Block new entries until operator clears
                try:
                    def _set_block(base):
                        c = base.get("operator_controls") or {}
                        c["disable_new_entries"] = True
                        base["operator_controls"] = c
                    _locked_state_update(_set_block)
                except Exception as e:
                    log.error("[GHOST_RECON] Failed to set disable_new_entries: %s", e)
                return False
            else:
                # Already latched — skip log/telemetry/state mutation.
                # disable_new_entries is already True from the first detection.
                log.debug(
                    "[GHOST_RECON] SAFETY BLOCK already latched for mint=%s — skipping re-disable",
                    tr.mint,
                )
                return False
        now_ts = int(time.time())
        mark_px = float(tr.current_px or tr.last_price or tr.entry_px or 0.0)
        archive_row = {
            "type": "ghost_reconciliation_terminal",
            "mint": tr.mint,
            "status": tr.status,
            "entry_ts": int(tr.entry_ts or tr.ts or 0),
            "terminalized_ts": now_ts,
            "reason": "ghost_reconciliation_wallet_absent",
            "qty_remaining": round(float(tr.qty_remaining or 0.0), 6),
            "remaining_basis_usd": round(float(tr.remaining_basis_usd or 0.0), 6),
            "size_usd": round(float(tr.size_usd or 0.0), 6),
            "realized_pnl_usd": round(float(tr.realized_pnl or 0.0), 6),
            "proceeds_usd": round(float(tr.proceeds_usd or 0.0), 6),
            "mark_px": round(mark_px, 10),
            "note": "wallet_absent_at_startup_reconciliation — buy TX may have failed on-chain; no sell TX submitted; no PnL mutation",
        }

        def _mut(base):
            rows = list(base.get("terminal_positions") or [])
            rows.append(archive_row)
            base["terminal_positions"] = rows[-200:]

        _locked_state_update(_mut)
        append_history(archive_row)
        append_telemetry_event(
            "GHOST_RECONCILIATION_TERMINALIZE",
            mint=tr.mint,
            detail=f"size_usd={tr.size_usd:.4f} qty_remaining={tr.qty_remaining:.4f} entry_ts={int(tr.entry_ts or 0)}",
            severity="warn",
        )
        log.warning(
            "[GHOST_RECON] terminalized mint=%s size_usd=%.4f — removed from active managed inventory",
            tr.mint, float(tr.size_usd or 0.0),
        )
        self.trades.pop(tr.mint, None)
        self._open_position_next_due.pop(tr.mint, None)
        return True

    async def _history_flush_loop(self):
        """P2.8: periodic batched flush of _HISTORY_CACHE to history.json.
        Runs blocking write in asyncio.to_thread so hot append_history callers
        don't block the event loop. Flushes one final time on cancellation."""
        try:
            while True:
                try:
                    await asyncio.sleep(_HISTORY_FLUSH_INTERVAL_SECONDS)
                    if _HISTORY_DIRTY:
                        await asyncio.to_thread(_flush_history_to_disk_sync)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("[HISTORY_FLUSH] loop iteration failed: %s", e)
        except asyncio.CancelledError:
            try:
                if _HISTORY_DIRTY:
                    _flush_history_to_disk_sync()
            except Exception as e:
                log.warning("[HISTORY_FLUSH] final flush failed: %s", e)
            raise

    async def _periodic_ghost_reconciliation_loop(self):
        """
        Periodic wallet-vs-position reconciliation (live mode only).
        Runs every 5 minutes. Detects positions that became ghosts after startup
        (e.g. tokens sold/transferred externally, or sell TX confirmed without state update).

        Resolution path: _ghost_reconciliation_terminalize() — no TX, no PnL change.
        """
        if self.execution_mode != "live":
            return
        # Short initial delay so externally sold/transferred positions do not linger as active for minutes.
        await asyncio.sleep(GHOST_RECON_INITIAL_DELAY_SECONDS)
        while True:
            open_trades = [
                tr for tr in self._trades_snapshot()
                if not tr.closed and float(tr.qty_remaining or 0.0) > 1e-9
            ]
            if open_trades:
                resolved = 0
                now_ts = time.time()
                for tr in open_trades:
                    position_age_s = max(0.0, now_ts - float(tr.entry_ts or tr.ts or now_ts))
                    if position_age_s < GHOST_RECON_MIN_POSITION_AGE_SECONDS:
                        continue
                    # Skip positions whose buy-verify has marked GHOST_PENDING and is still
                    # within the recheck window — let _ghost_pending_recheck resolve them
                    # first, otherwise the periodic recon will race it and terminalize
                    # (which flips the _ghost_recon_no_sell_mints safety latch).
                    _pending_age = 0.0
                    if tr.position_truth_state == "GHOST_PENDING" and tr.ghost_pending_since > 0:
                        _pending_age = now_ts - float(tr.ghost_pending_since)
                        if _pending_age < GHOST_BUY_PENDING_RECHECK_SECONDS:
                            continue
                    wallet_raw = await self._fetch_live_token_balance(tr.mint)
                    if wallet_raw == self._BALANCE_FETCH_ERROR:
                        # P2.3: if GHOST_PENDING has exceeded the absolute ceiling AND the wallet
                        # is still unreachable, force-terminalize rather than looping forever.
                        if _pending_age >= MAX_GHOST_PENDING_SECONDS:
                            log.error(
                                "[GHOST_RECON_PERIODIC] mint=%s FETCH_ERROR + pending_age=%.0fs >= %ds — "
                                "force-terminalizing stale GHOST_PENDING",
                                tr.mint, _pending_age, MAX_GHOST_PENDING_SECONDS,
                            )
                            append_telemetry_event(
                                "GHOST_PENDING_FORCE_TERMINALIZE", mint=tr.mint,
                                detail=f"pending_age={_pending_age:.0f}s threshold={MAX_GHOST_PENDING_SECONDS}s fetch_error=True",
                                severity="error",
                            )
                            self._ghost_reconciliation_terminalize(tr)
                            resolved += 1
                            continue
                        log.warning(
                            "[GHOST_RECON_PERIODIC] mint=%s wallet_raw=FETCH_ERROR pending_age=%.0fs — skipping",
                            tr.mint, _pending_age,
                        )
                        continue
                    if wallet_raw > 0:
                        continue
                    # Confirmed zero balance post-startup — ghost position
                    log.warning(
                        "[GHOST_RECON_PERIODIC] mint=%s wallet_raw=0 — post-startup ghost detected, terminalizing",
                        tr.mint,
                    )
                    append_telemetry_event(
                        "GHOST_DETECTED_PERIODIC",
                        mint=tr.mint,
                        detail=f"position_age_s={position_age_s:.0f} hold_time={getattr(tr,'hold_time_seconds',0):.0f}s qty_remaining={tr.qty_remaining:.2f}",
                        severity="warn",
                    )
                    self._ghost_reconciliation_terminalize(tr)
                    resolved += 1
                if resolved > 0:
                    self._state_sync()
                    append_telemetry_event(
                        "GHOST_RECONCILIATION_PERIODIC_COMPLETE",
                        detail=f"resolved={resolved} open_checked={len(open_trades)}",
                        severity="warn",
                    )
                    log.warning(
                        "[GHOST_RECON_PERIODIC] complete — resolved=%d ghost(s) out of %d open",
                        resolved, len(open_trades),
                    )
            await asyncio.sleep(GHOST_RECON_INTERVAL_SECONDS)

    async def _startup_ghost_reconciliation_task(self):
        """
        One-shot startup task (live mode only): resolve ghost positions whose wallet
        balance is confirmed zero. Runs ~30s after startup to allow any in-progress
        wallet scans and state restore to settle.

        Ghost positions are open managed records with no wallet backing — their buy TX
        either failed on-chain or the tokens no longer exist. They must not remain as
        actionable open positions eligible for sell logic or contributing to open counts.

        Resolution path: _ghost_reconciliation_terminalize() — no TX, no PnL change.
        """
        if self.execution_mode != "live":
            return
        await asyncio.sleep(30)

        open_trades = [
            tr for tr in self._trades_snapshot()
            if not tr.closed and float(tr.qty_remaining or 0.0) > 1e-9
        ]
        if not open_trades:
            log.info("[GHOST_RECON] no open positions at startup scan — nothing to resolve")
            return

        log.info("[GHOST_RECON] startup scan: checking %d open position(s) for wallet backing", len(open_trades))
        resolved = 0
        now_ts = time.time()
        for tr in open_trades:
            # Skip positions still inside their GHOST_PENDING recheck window (see periodic loop).
            if tr.position_truth_state == "GHOST_PENDING" and tr.ghost_pending_since > 0:
                pending_age = now_ts - float(tr.ghost_pending_since)
                if pending_age < GHOST_BUY_PENDING_RECHECK_SECONDS:
                    log.info(
                        "[GHOST_RECON] mint=%s in GHOST_PENDING (age=%.0fs) — deferring to buy-verify recheck",
                        tr.mint, pending_age,
                    )
                    continue
            wallet_raw = await self._fetch_live_token_balance(tr.mint)
            if wallet_raw == self._BALANCE_FETCH_ERROR:
                # RPC uncertain — skip; do not terminalize on fetch failure
                log.warning(
                    "[GHOST_RECON] mint=%s wallet_raw=FETCH_ERROR — RPC uncertain, skipping terminalization",
                    tr.mint,
                )
                continue
            if wallet_raw > 0:
                log.info("[GHOST_RECON] mint=%s wallet_raw=%d — real position confirmed, skip", tr.mint, wallet_raw)
                continue
            # wallet_raw == 0: confirmed zero balance → ghost
            log.warning(
                "[GHOST_RECON] mint=%s wallet_raw=0 — confirmed no wallet backing, terminalizing ghost",
                tr.mint,
            )
            self._ghost_reconciliation_terminalize(tr)
            resolved += 1

        if resolved > 0:
            self._state_sync()
            append_telemetry_event(
                "GHOST_RECONCILIATION_COMPLETE",
                detail=f"resolved={resolved} open_checked={len(open_trades)}",
                severity="info",
            )
            log.warning("[GHOST_RECON] complete — resolved=%d ghost position(s), state synced", resolved)
        else:
            log.info("[GHOST_RECON] startup scan complete — all %d open position(s) have wallet backing", len(open_trades))

    def _jito_tip_account(self) -> str:
        pinned = os.getenv("JITO_TIP_ACCOUNT", "").strip()
        if pinned:
            return pinned
        if JITO_TIP_ROTATE:
            global _jito_tip_rr_idx
            acct = JITO_TIP_ACCOUNTS_ALL[_jito_tip_rr_idx % len(JITO_TIP_ACCOUNTS_ALL)]
            _jito_tip_rr_idx = (_jito_tip_rr_idx + 1) % len(JITO_TIP_ACCOUNTS_ALL)
            return acct
        return JITO_TIP_ACCOUNTS.get(JITO_REGION, JITO_TIP_ACCOUNTS["ny"])

    def _trades_snapshot(self) -> list[PaperTrade]:
        return list(self.trades.values())

    def _get_sell_lock(self, mint: str) -> asyncio.Lock:
        """P2.2: lazily create a per-mint sell lock. Safe because sync creation under
        asyncio's single-threaded event loop is atomic — no async yield between the
        check and the assignment."""
        lock = self._sell_locks.get(mint)
        if lock is None:
            lock = asyncio.Lock()
            self._sell_locks[mint] = lock
        return lock

    async def _atomic_add_trade(self, mint: str, tr: "PaperTrade") -> bool:
        """P2.1: atomically check-and-insert into self.trades under _trades_lock.
        Returns True if inserted, False if a trade already exists for this mint
        (caller should treat this as a duplicate-signal race and abort its PnL
        tracking). Must be called from async context; sync call-sites (state
        restore at startup) bypass via direct assignment — no race there."""
        async with self._trades_lock:
            if mint in self.trades:
                return False
            self.trades[mint] = tr
            return True

    def _paper_positions_snapshot(self) -> list[dict]:
        rows = []
        for tr in self._trades_snapshot():
            mark_px = float(tr.last_price or tr.current_px or tr.entry_px or 0.0)
            market_value_usd = max(0.0, float(tr.qty_remaining or 0.0) * mark_px) if mark_px > 0 else 0.0
            remaining_fee_reserve_usd = max(0.0, float(tr.fees_usd or 0.0) - float(tr.realized_fees_usd or 0.0))
            unrealized = market_value_usd - float(tr.remaining_basis_usd or 0.0) - remaining_fee_reserve_usd
            unrealized_denom = float(tr.remaining_basis_usd or 0.0) + remaining_fee_reserve_usd
            tr.unrealized_roi_pct = (unrealized / unrealized_denom) * 100.0 if unrealized_denom > 0 else 0.0
            # Realized ROI: only recompute for open/partial positions.
            # For closed trades it is finalized by _finalize_trade_close (wallet-
            # aligned formula) and must not be overwritten by a stale snapshot pass.
            if str(tr.status) != "closed":
                _cost = float(tr.cost_basis_total_usd or tr.size_usd)
                _proceeds = float(tr.proceeds_usd or 0.0)
                if _proceeds > 0 and _cost > 0:
                    tr.realized_roi_pct = (_proceeds / _cost - 1.0) * 100.0
                elif _cost > 0:
                    tr.realized_roi_pct = (float(tr.realized_pnl or 0.0) / _cost) * 100.0
            qty_recon_error = float(tr.qty_total or 0.0) - float(tr.qty_closed or 0.0) - float(tr.qty_remaining or 0.0)
            basis_recon_error = float(tr.cost_basis_total_usd or 0.0) - float(tr.realized_basis_usd or 0.0) - float(tr.remaining_basis_usd or 0.0)
            fee_recon_error = float(tr.fees_usd or 0.0) - float(tr.realized_fees_usd or 0.0) - remaining_fee_reserve_usd

            # Stale policy:
            # - open / partial_exit positions: stale once last live mark age exceeds threshold
            # - closed positions: stale does not apply; exit price is final and not expected to refresh
            if str(tr.status) == "closed":
                stale_secs = 0.0
                tr.mark_stale = False
                tr.mark_staleness_seconds = 0.0
            else:
                _stale_ref = tr.last_price_update_at or tr.entry_ts or tr.ts
                stale_secs = max(0.0, time.time() - float(_stale_ref)) if _stale_ref else 0.0
                tr.mark_staleness_seconds = stale_secs
                recent_grace = bool(tr.last_price_update_at and stale_secs < MARK_PRICE_RECENT_GRACE_SECONDS)
                tr.mark_stale = (not tr.last_price_update_at) or (stale_secs >= MARK_PRICE_STALE_AFTER_SECONDS and not recent_grace)
                if tr.mark_stale and str(tr.current_px_source).startswith("live_quote"):
                    tr.current_px_source = "stale_hold"
                elif recent_grace and str(tr.current_px_source) == "stale_hold":
                    tr.current_px_source = "live_quote_recent"

            # ROI truth classification — controls HUD display of ROI quality
            _tr_status = str(tr.status or "open")
            if _tr_status == "closed":
                _roi_truth_source = "realized"
                _roi_truth_quality = "HIGH"
                _roi_truth_age_s = 0.0
            elif tr.mark_stale:
                _roi_truth_source = "stale_mark"
                _roi_truth_quality = "DEGRADED"
                _roi_truth_age_s = round(stale_secs, 1)
            else:
                _roi_truth_source = str(tr.current_px_source or tr.mark_price_source or "mark")
                _roi_truth_quality = str(tr.mark_confidence_level or "MED")
                _roi_truth_age_s = round(stale_secs, 1)
            # Position truth state: use the dataclass field, keeping it in sync
            if _tr_status == "closed":
                tr.position_truth_state = "CLOSED"
            elif tr.position_truth_state not in ("GHOST_UNRESOLVED", "TERMINALIZED"):
                tr.position_truth_state = "PARTIAL_EXIT" if _tr_status == "partial_exit" else "OPEN"

            rows.append(
                {
                    "mint": tr.mint,
                    "entry_ts": int(tr.entry_ts or tr.ts or 0),
                    "entry_price": round(tr.entry_price or tr.entry_px, 10),
                    "size_usd": round(tr.size_usd, 4),
                    "status": tr.status,
                    "last_price": round(mark_px, 10) if mark_px else None,
                    "last_price_update_at": int(tr.last_price_update_at or 0),
                    "unrealized_roi_pct": round(tr.unrealized_roi_pct, 4),
                    "realized_roi_pct": round(tr.realized_roi_pct, 4),
                    "roi_truth_source": _roi_truth_source,
                    "roi_truth_quality": _roi_truth_quality,
                    "roi_truth_age_s": _roi_truth_age_s,
                    "position_truth_state": tr.position_truth_state,
                    "forced_exit_unresolved": bool(tr.forced_exit_unresolved),
                    "forced_exit_attempt_count": int(tr.forced_exit_attempt_count),
                    "exit_ts": int(tr.exit_ts or 0),
                    "exit_price": round(tr.exit_price, 10) if tr.exit_price else None,
                    "close_reason": tr.close_reason or None,
                    "qty_closed": round(tr.qty_closed, 6),
                    "qty_remaining": round(tr.qty_remaining, 6),
                    "proceeds_usd": round(tr.proceeds_usd, 6),
                    "cost_basis_total_usd": round(tr.cost_basis_total_usd, 6),
                    "remaining_basis_usd": round(tr.remaining_basis_usd, 6),
                    "realized_basis_usd": round(tr.realized_basis_usd, 6),
                    "realized_fees_usd": round(tr.realized_fees_usd, 6),
                    "remaining_fee_reserve_usd": round(remaining_fee_reserve_usd, 6),
                    "market_value_usd": round(market_value_usd, 6),
                    "accounting": {
                        "qty_reconciliation_error": round(qty_recon_error, 10),
                        "basis_reconciliation_error_usd": round(basis_recon_error, 10),
                        "fee_reconciliation_error_usd": round(fee_recon_error, 10),
                        "ok": abs(qty_recon_error) < 1e-9 and abs(basis_recon_error) < 1e-6 and abs(fee_recon_error) < 1e-6,
                    },
                    # compatibility/debug fields
                    "reason": tr.reason,
                    "qty_total": round(tr.qty_total, 6),
                    "entry_px": round(tr.entry_px, 10),
                    "current_px": round(mark_px, 10) if mark_px else None,
                    "current_px_source": tr.current_px_source,
                    "mark_price_source": tr.mark_price_source,
                    "mark_notional_usd_used": round(float(tr.mark_notional_usd_used or 0.0), 6),
                    "mark_notional_size_used": round(float(tr.mark_notional_size_used or 0.0), 6),
                    "mark_confidence_level": str(tr.mark_confidence_level or "LOW"),
                    "mark_vs_full_liq_diff_pct": round(float(tr.mark_vs_full_liq_diff_pct), 4) if tr.mark_vs_full_liq_diff_pct is not None else None,
                    "mark_stale": bool(tr.mark_stale),
                    "mark_staleness_seconds": round(tr.mark_staleness_seconds, 3),
                    "last_mark_ts": int(tr.last_mark_ts or 0),
                    "unrealized_pnl_usd": round(unrealized, 6),
                    "realized_pnl_usd": round(tr.realized_pnl, 6),
                    "fees_usd": round(tr.fees_usd, 6),
                    "stop_px": round(tr.stop_px, 10),
                    "high_px": round(tr.high_px, 10),
                    "low_px": round(tr.low_px, 10),
                    "mfe_pct": round(tr.mfe_pct, 4),
                    "mae_pct": round(tr.mae_pct, 4),
                    "stage1_done": bool(tr.stage1_done),
                    "stage2_done": bool(tr.stage2_done),
                    "restored_from_state_ts": int(tr.restored_from_state_ts or 0),
                    "restore_mark_age_seconds": round(float(tr.restore_mark_age_seconds or 0.0), 3),
                    "fresh_mark_seen_since_restore": bool(tr.fresh_mark_seen_since_restore),
                    "canonical_close_emitted": bool(tr.canonical_close_emitted or tr.close_recorded),
                    "close_event_emitted_ts": int(tr.close_event_emitted_ts or 0),
                    "stop_trigger_ts": float(tr.stop_trigger_ts or 0.0),
                    "sell_attempt_ts": float(tr.sell_attempt_ts or 0.0),
                    "sell_success_ts": float(tr.sell_success_ts or 0.0),
                    "first_executable_mark_ts": float(tr.first_executable_mark_ts or 0.0),
                    "executable_mark_observations": int(tr.executable_mark_observations or 0),
                    "total_mark_observations": int(tr.total_mark_observations or 0),
                    "first_mark_ts": float(tr.first_mark_ts or 0.0),
                    "first_mark_delay_ms": round((float(tr.first_mark_ts or 0.0) - float(tr.entry_ts or tr.ts or 0.0)) * 1000.0, 1) if float(tr.first_mark_ts or 0.0) > 0 else None,
                    "mark_source_sequence": str(tr.mark_source_sequence or ""),
                    "hold_time_seconds": round(max(0.0, ((tr.exit_ts or time.time()) - float(tr.entry_ts or tr.ts or time.time()))), 3),
                    "ts": int(tr.ts),
                }
            )
        rows.sort(key=lambda x: (int(x.get("entry_ts", 0) or 0), x.get("mint", "")))
        return rows[-200:]

    def _canonical_session_position_rows(self, rows: Optional[list[dict]] = None, include_stale: bool = True) -> list[dict]:
        base_rows = list(rows) if rows is not None else self._paper_positions_snapshot()
        canonical = []
        for row in base_rows:
            if str(row.get("status") or "") == "closed":
                continue
            if float(row.get("qty_remaining") or 0.0) <= 1e-9:
                continue
            if float(row.get("remaining_basis_usd") or 0.0) <= 1e-9 and float(row.get("market_value_usd") or 0.0) <= 1e-9:
                continue
            # NOTE: deliberately NOT filtering by entry_ts < start_ts.
            # Positions restored from a previous session must remain visible — hiding
            # them caused the -37% untracked loss incident. All managed positions must
            # appear in the UI regardless of which session created them.
            canonical.append(row)
        return canonical

    def _active_positions_snapshot(self) -> list[dict]:
        return self._canonical_session_position_rows()

    def _capital_open_position_rows(self, rows: Optional[list[dict]] = None, include_stale: bool = True) -> list[dict]:
        base_rows = list(rows) if rows is not None else self._paper_positions_snapshot()
        capital_rows = []
        for row in base_rows:
            if str(row.get("status") or "") == "closed":
                continue
            if float(row.get("qty_remaining") or 0.0) <= 1e-9:
                continue
            if float(row.get("remaining_basis_usd") or 0.0) <= 1e-9 and float(row.get("market_value_usd") or 0.0) <= 1e-9:
                continue
            if (not include_stale) and bool(row.get("mark_stale")):
                continue
            capital_rows.append(row)
        return capital_rows

    def _canonical_capital_seed_start_usd(self, rows: Optional[list[dict]] = None, cash_budget: Optional[float] = None) -> float:
        capital_rows = self._capital_open_position_rows(rows, include_stale=True)
        budget_cash = float(self.budget if cash_budget is None else cash_budget)
        open_invested_capital = sum(
            float(row.get("remaining_basis_usd") or 0.0) + float(row.get("remaining_fee_reserve_usd") or 0.0)
            for row in capital_rows
        )
        return round(budget_cash + open_invested_capital, 6)

    def _ensure_canonical_capital_fields(self, base: dict, rows: Optional[list[dict]] = None, cash_budget: Optional[float] = None) -> None:
        if base.get("canonical_start_capital_usd") is None:
            base["canonical_start_capital_usd"] = self._canonical_capital_seed_start_usd(rows=rows, cash_budget=cash_budget)
        if base.get("canonical_realized_usd") is None:
            base["canonical_realized_usd"] = 0.0

    def _bump_canonical_realized_usd(self, delta_usd: float) -> None:
        delta = float(delta_usd or 0.0)
        if abs(delta) < 1e-12:
            return

        current_rows = self._paper_positions_snapshot()
        current_budget = float(self.budget)
        current_capital_rows = self._capital_open_position_rows(current_rows, include_stale=True)
        current_unrealized_usd = round(sum(float(row.get("unrealized_pnl_usd") or 0.0) for row in current_capital_rows), 6)

        def _mut(base):
            self._ensure_canonical_capital_fields(base, rows=current_rows, cash_budget=current_budget)
            canonical_realized_usd = round(float(base.get("canonical_realized_usd") or 0.0) + delta, 6)
            canonical_start_capital_usd = round(float(base.get("canonical_start_capital_usd") or 0.0), 6)
            canonical_capital_usd = round(canonical_start_capital_usd + canonical_realized_usd + current_unrealized_usd, 6)
            base["canonical_realized_usd"] = canonical_realized_usd
            base["canonical_unrealized_usd"] = round(current_unrealized_usd, 6)
            base["canonical_capital_usd"] = canonical_capital_usd
            if self.execution_mode != "live":
                base["sim_budget_usd"] = canonical_capital_usd

        _locked_state_update(_mut, force_flush=True)

    def _restore_paper_positions_from_state(self, st: dict):
        rows = list(st.get("paper_positions") or st.get("active_positions") or [])
        if not rows:
            return
        restored_ts = time.time()
        for row in rows:
            status = str(row.get("status") or "open")
            if status == "closed":
                continue
            mint = str(row.get("mint") or "").strip()
            if not mint:
                continue
            entry_px = float(row.get("entry_price") or row.get("entry_px") or 0.0)
            size_usd = float(row.get("size_usd") or 0.0)
            qty_total = float(row.get("qty_total") or (size_usd / max(entry_px, 1e-9)) or 0.0)
            qty_remaining = float(row.get("qty_remaining") or qty_total)
            qty_closed = float(row.get("qty_closed") or max(0.0, qty_total - qty_remaining))
            stage1_done = bool(row.get("stage1_done", False)) or qty_closed > 1e-9
            stage2_done = bool(row.get("stage2_done", False)) or qty_closed >= (qty_total * 0.75 - 1e-6)
            last_mark_ts = float(row.get("last_price_update_at") or row.get("last_mark_ts") or 0.0)
            restore_mark_age_seconds = max(0.0, restored_ts - last_mark_ts) if last_mark_ts > 0 else max(0.0, restored_ts - float(row.get("entry_ts") or row.get("ts") or restored_ts))
            tr = PaperTrade(
                mint=mint,
                ts=float(row.get("entry_ts") or row.get("ts") or time.time()),
                size_usd=size_usd,
                reason=str(row.get("reason") or "paper_position_restore"),
                entry_px=entry_px,
                current_px=float(row.get("last_price") or row.get("current_px") or entry_px or 0.0),
                current_px_source=str(row.get("current_px_source") or "restored"),
                last_mark_ts=last_mark_ts,
                high_px=float(row.get("high_px") or entry_px or 0.0),
                low_px=float(row.get("low_px") or entry_px or 0.0),
                stop_px=float(row.get("stop_px") or (entry_px * (1 - DEFAULT_DOWNSIDE_STOP_PCT))),
                closed=False,
                realized_pnl=float(row.get("realized_pnl_usd") or 0.0),
                qty_total=qty_total,
                qty_remaining=qty_remaining,
                fees_usd=float(row.get("fees_usd") or 0.0),
                stage1_done=stage1_done,
                stage2_done=stage2_done,
                status=status,
                # P2.5: never fall back to time.time() on restore — that resets position age
                # to zero and corrupts HARD_CAP age guards, STOP_TIGHTEN ladder, and TTL.
                # Use entry_ts → ts → fail to 0 (which downstream code already handles).
                entry_ts=float(row.get("entry_ts") or row.get("ts") or 0.0),
                entry_price=entry_px,
                last_price=float(row.get("last_price") or row.get("current_px") or entry_px or 0.0),
                last_price_update_at=last_mark_ts,
                mark_price_source=str(row.get("mark_price_source") or row.get("current_px_source") or "fallback"),
                mark_notional_usd_used=float(row.get("mark_notional_usd_used") or 0.0),
                mark_notional_size_used=float(row.get("mark_notional_size_used") or 0.0),
                mark_confidence_level=str(row.get("mark_confidence_level") or "LOW"),
                mark_vs_full_liq_diff_pct=float(row.get("mark_vs_full_liq_diff_pct")) if row.get("mark_vs_full_liq_diff_pct") is not None else None,
                mark_stale=bool(row.get("mark_stale", False)),
                mark_staleness_seconds=float(row.get("mark_staleness_seconds") or 0.0),
                unrealized_roi_pct=float(row.get("unrealized_roi_pct") or 0.0),
                realized_roi_pct=float(row.get("realized_roi_pct") or 0.0),
                exit_ts=float(row.get("exit_ts") or 0.0),
                exit_price=float(row.get("exit_price") or 0.0),
                close_reason=str(row.get("close_reason") or ""),
                qty_closed=qty_closed,
                proceeds_usd=float(row.get("proceeds_usd") or 0.0),
                cost_basis_total_usd=float(row.get("cost_basis_total_usd") or size_usd),
                remaining_basis_usd=float(row.get("remaining_basis_usd") or size_usd * (qty_remaining / max(qty_total, 1e-9))),
                realized_basis_usd=float(row.get("realized_basis_usd") or max(0.0, size_usd - (size_usd * (qty_remaining / max(qty_total, 1e-9))))),
                realized_fees_usd=float(row.get("realized_fees_usd") or 0.0),
                mfe_pct=float(row.get("mfe_pct") or 0.0),
                mae_pct=float(row.get("mae_pct") or 0.0),
                close_recorded=bool(row.get("canonical_close_emitted") or (row.get("status") == "closed" and row.get("exit_ts"))),
                restored_from_state_ts=float(row.get("restored_from_state_ts") or restored_ts),
                restore_mark_age_seconds=float(row.get("restore_mark_age_seconds") or restore_mark_age_seconds),
                fresh_mark_seen_since_restore=bool(row.get("fresh_mark_seen_since_restore", False)),
                close_event_emitted_ts=float(row.get("close_event_emitted_ts") or 0.0),
                canonical_close_emitted=bool(row.get("canonical_close_emitted") or row.get("close_event_emitted_ts") or (row.get("status") == "closed" and row.get("exit_ts"))),
                stop_trigger_ts=float(row.get("stop_trigger_ts") or 0.0),
                sell_attempt_ts=float(row.get("sell_attempt_ts") or 0.0),
                sell_success_ts=float(row.get("sell_success_ts") or 0.0),
                first_executable_mark_ts=float(row.get("first_executable_mark_ts") or 0.0),
                executable_mark_observations=int(row.get("executable_mark_observations") or 0),
                total_mark_observations=int(row.get("total_mark_observations") or 0),
                first_mark_ts=float(row.get("first_mark_ts") or 0.0),
                mark_source_sequence=str(row.get("mark_source_sequence") or ""),
            )
            self.trades[mint] = tr

    def _persist_successful_entry(self, mint: str, size_usd: float, ts_now: int) -> bool:
        t_activity_start = time.perf_counter()
        _action_label = "LIVE BUY" if self.execution_mode == "live" else "PAPER BUY"
        append_recent_activity(action=_action_label, mint=mint, live_pnl=0.0, ts=ts_now)
        entry_activity_ms = round((time.perf_counter() - t_activity_start) * 1000, 4)
        t_snapshot_start = time.perf_counter()

        def _mut_entry(base):
            base["last_entry"] = {"mint": mint, "size_usd": size_usd, "reason": f"migration:{mint[:8]}", "ts": ts_now, "status": "entered"}
            base["last_trade"] = {"mint": mint, "size_usd": size_usd, "reason": f"migration:{mint[:8]}", "ts": ts_now, "closed": False}
            base["paper_positions"] = self._paper_positions_snapshot()
            base["active_positions"] = self._active_positions_snapshot()
            base["trace_entry"] = {"entry_activity_ms": entry_activity_ms, "ts": ts_now, "mint": mint}

        ok = _locked_state_update(_mut_entry, force_flush=True)
        queue_state_update(trace_entry={"entry_activity_ms": entry_activity_ms, "entry_snapshot_ms": round((time.perf_counter() - t_snapshot_start) * 1000, 4), "ts": ts_now, "mint": mint})
        return ok

    _priority_fee_cache: tuple[int, float] = (CU_PRICE_MIN, 0.0)  # (fee, monotonic_ts)
    _PRIORITY_FEE_CACHE_TTL_S = 10.0  # priority fees stable over ~10s window

    async def _fetch_priority_fee_p75(self) -> int:
        cached_fee, cached_ts = self._priority_fee_cache
        if cached_fee > 0 and time.monotonic() - cached_ts < self._PRIORITY_FEE_CACHE_TTL_S:
            return cached_fee
        req = {"jsonrpc": "2.0", "id": 1, "method": "getRecentPrioritizationFees", "params": []}
        try:
            status, data = await self._rpc_post(req, timeout=6)
            if status != 200:
                if status == 429:
                    self._metrics_bump({"rpc_429_count": 1}, {"rpc_429_count": 1})
                    append_telemetry_event("RPC_429", detail="priority_fee_probe", severity="warn")
                self.rotate_rpc()
                return CU_PRICE_MIN
            rows = data.get("result") or []
            fees = [int(x.get("prioritizationFee", 0) or 0) for x in rows][-150:]
            fees = [f for f in fees if f >= 0]
            if not fees:
                return CU_PRICE_MIN
            fees.sort()
            idx = min(len(fees) - 1, max(0, math.ceil(0.75 * len(fees)) - 1))
            result_fee = int(fees[idx])
            self._priority_fee_cache = (result_fee, time.monotonic())
            return result_fee
        except Exception:
            return CU_PRICE_MIN

    async def _build_execution_intent(self, mint: str, ts_detected: float, reference_price_usd: Optional[float] = None) -> ExecutionIntent:
        now = time.time()
        sol_usd = await self._sol_usd()
        size_usd = 10.0

        if self.execution_mode == "live":
            # First N live attempts are hard-capped to MAX_POSITION_SIZE_SOL
            if self.live_trade_attempts < LIVE_CANARY_TRADES:
                canary_cap_usd = MAX_POSITION_SIZE_SOL * max(sol_usd, 1.0)
                size_usd = min(LIVE_BASE_TRADE_USD, canary_cap_usd)
            else:
                size_usd = LIVE_BASE_TRADE_USD

        max_spend_lamports = int((size_usd / max(sol_usd, 1e-9)) * 1_000_000_000 * (1 + SLIPPAGE_PCT))
        decision_id = f"{int(now*1000)}-{mint[:8]}"

        return ExecutionIntent(
            decision_id=decision_id,
            mint=mint,
            size_usd=size_usd,
            max_slippage_pct=SLIPPAGE_PCT,
            max_cost_lamports=max_spend_lamports,
            ts_detected=ts_detected,
            ts_decided=now,
            priority_policy="p75-clamped",
            reference_price_usd=reference_price_usd,
        )

    def _with_rates(self, m: dict):
        closed = int(m.get("closed_wins", 0)) + int(m.get("closed_losses", 0))
        if closed > 0:
            m["win_rate_pct"] = round((int(m.get("closed_wins", 0)) / closed) * 100.0, 2)
            m["loss_rate_pct"] = round((int(m.get("closed_losses", 0)) / closed) * 100.0, 2)
        else:
            m["win_rate_pct"] = 0.0
            m["loss_rate_pct"] = 0.0

        dcount = int(m.get("decision_count", 0))
        dsum = float(m.get("decision_latency_sum_ms", 0.0) or 0.0)
        m["avg_decision_latency_ms"] = round(dsum / dcount, 3) if dcount > 0 else 0.0

        elapsed_h = max((time.time() - float(m.get("session_start_ts", self.start_ts))) / 3600.0, 1 / 3600)
        m["market_temp_mints_per_hour"] = round(float(m.get("unique_mints", 0)) / elapsed_h, 3)
        return m

    def _candidate_key(self, msg: dict) -> str:
        value = ((msg.get("params") or {}).get("result") or {}).get("value") or {}
        sig = str(value.get("signature") or "").strip()
        if sig:
            return f"sig:{sig}"
        slot = value.get("slot")
        logs = value.get("logs") or []
        basis = json.dumps({"slot": slot, "logs": logs[:12]}, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
        return f"log:{slot or 'na'}:{digest}"

    async def _remember_candidate_key(self, candidate_key: str) -> bool:
        async with self._seen_lock:
            if candidate_key in self.seen_candidate_keys:
                self.seen_candidate_keys.move_to_end(candidate_key)
                return False
            self.seen_candidate_keys[candidate_key] = int(time.time())
            while len(self.seen_candidate_keys) > EVENT_CANDIDATE_DEDUPE_MAX:
                self.seen_candidate_keys.popitem(last=False)
            return True

    def _queue_dependency_retry(self, mint: str, live_price: float, reason: str, attempts: int = 1):
        ts_now = int(time.time())
        cooldown_key = f"{mint}:{reason}"
        cooldown_until = float(self._dependency_retry_cooldown.get(cooldown_key, 0.0) or 0.0)
        if cooldown_until > time.time():
            queue_telemetry_event("DEPENDENCY_RETRY_SUPPRESSED", mint=mint, detail=f"reason={reason} attempts={attempts}", severity="info")
            return
        self._dependency_retry_cooldown[cooldown_key] = time.time() + min(90.0, 20.0 * max(1, attempts))
        next_retry_ts = ts_now + min(60, 8 * max(1, attempts))

        def _mut(base):
            rows = list(base.get("dependency_retry_queue") or [])
            replaced = False
            for row in rows:
                if str(row.get("mint") or "") == mint:
                    row.update({
                        "mint": mint,
                        "live_price": round(float(live_price or 0.0), 10),
                        "reason": reason,
                        "attempts": max(int(row.get("attempts", 0) or 0), attempts),
                        "next_retry_ts": next_retry_ts,
                        "updated_ts": ts_now,
                        "first_ts": int(row.get("first_ts", ts_now) or ts_now),
                    })
                    replaced = True
                    break
            if not replaced:
                rows.append({
                    "mint": mint,
                    "live_price": round(float(live_price or 0.0), 10),
                    "reason": reason,
                    "attempts": attempts,
                    "first_ts": ts_now,
                    "updated_ts": ts_now,
                    "next_retry_ts": next_retry_ts,
                })
            base["dependency_retry_queue"] = rows[-200:]

        _locked_state_update(_mut)
        queue_telemetry_event("DEPENDENCY_RETRY_QUEUED", mint=mint, detail=f"reason={reason} attempts={attempts}", severity="warn")

    def _pop_due_dependency_retries(self) -> list[dict]:
        ts_now = int(time.time())
        due: list[dict] = []

        def _mut(base):
            nonlocal due
            rows = list(base.get("dependency_retry_queue") or [])
            keep = []
            for row in rows:
                if int(row.get("next_retry_ts", 0) or 0) <= ts_now:
                    due.append(dict(row))
                else:
                    keep.append(row)
            base["dependency_retry_queue"] = keep[-200:]

        _locked_state_update(_mut)
        return due

    def _should_emit_dup_skip(self, mint: str) -> bool:
        now_ts = int(time.time())
        last_ts = int(self._dup_skip_last_emit.get(mint, 0) or 0)
        if now_ts - last_ts < DUP_SKIP_ACTIVITY_COOLDOWN_SECONDS:
            return False
        self._dup_skip_last_emit[mint] = now_ts
        return True

    def _record_parse_terminal_failure(self, reason: str, candidate_key: str = ""):
        t_record_start = time.perf_counter()
        metric_key = {
            "no_sig": "parse_fail_no_sig",
            "rpc_429": "parse_fail_rpc_429",
            "rpc_timeout": "parse_fail_rpc_timeout",
            "excluded_mint": "parse_fail_excluded_mint",
            "low_confidence": "parse_fail_low_confidence",
            "no_balances": "parse_fail_no_balances",
        }.get(reason, "parse_fail_no_balances")
        if reason == "rpc_timeout" and candidate_key.startswith("sig:"):
            sig = candidate_key.split("sig:", 1)[1].strip()
            if sig:
                self._terminal_timeout_suppression[sig] = time.time() + 120.0
        delta = {"parse_failures": 1, metric_key: 1}
        t_state_churn_start = time.perf_counter()
        self._metrics_bump(delta, delta)
        parse_fail_state_churn_ms = round((time.perf_counter() - t_state_churn_start) * 1000, 4)
        detail = f"reason={reason} parse_fail_state_churn_ms={parse_fail_state_churn_ms}"
        if candidate_key:
            detail += f" candidate_key={candidate_key}"
        detail += f" parse_terminal_record_ms={round((time.perf_counter() - t_record_start) * 1000, 4)}"
        log.warning("[PARSE_DROP] reason=%s candidate=%s rpc=%s", reason, candidate_key or "n/a", self.rpc_url)
        queue_telemetry_event("PARSE_TERMINAL", detail=detail, severity="info")

    def _metrics_bump(self, daily_delta: dict | None = None, all_delta: dict | None = None):
        day = utc_day()

        def _mut(base):
            dm = base.get("daily_metrics") or default_metrics(day)
            at = base.get("all_time_metrics") or default_metrics("all-time")

            if dm.get("day_utc") != day:
                append_history({"type": "daily_metrics_rollover", "ts": int(time.time()), "day_utc": dm.get("day_utc"), "metrics": dm})
                dm = default_metrics(day)
                self.daily_seen_mints = set()

            if daily_delta:
                for k, v in daily_delta.items():
                    dm[k] = (dm.get(k, 0) or 0) + v

            if all_delta:
                for k, v in all_delta.items():
                    at[k] = (at.get(k, 0) or 0) + v

            dm = self._with_rates(dm)
            at = self._with_rates(at)

            base["daily_metrics"] = dm
            base["all_time_metrics"] = at
            base["min_liquidity_usd_active"] = self.current_liquidity_usd
            base["paper_buys_under_lowered_threshold"] = self.paper_buys_under_lowered_threshold

        _locked_state_update(_mut)

    _sol_usd_cache: tuple[float, float] = (0.0, 0.0)  # (price, monotonic_ts)
    _SOL_USD_CACHE_TTL_S = 30.0

    async def _sol_usd(self) -> float:
        now = time.monotonic()
        cached_price, cached_ts = self._sol_usd_cache
        if cached_price > 0 and (now - cached_ts) < self._SOL_USD_CACHE_TTL_S:
            return cached_price
        try:
            s = await self._get_http_session()
            async with s.get("https://price.jup.ag/v4/price?ids=SOL", timeout=5) as r:
                if r.status == 200:
                    d = await r.json()
                    price = float(d["data"]["SOL"]["price"])
                    self._sol_usd_cache = (price, time.monotonic())
                    return price
        except Exception:
            pass
        # Return cached value if available, else fallback
        if cached_price > 0:
            return cached_price
        return 150.0

    async def _token_price(self, mint: str) -> Optional[float]:
        if mint.startswith("SIM_"):
            return None
        try:
            s = await self._get_http_session()
            # Fire DexScreener concurrently so the cross-check adds no serial latency.
            ds_task = asyncio.create_task(self._dexscreener_price(mint))
            jup_px: Optional[float] = None
            try:
                async with s.get(f"https://api.jup.ag/price/v3?ids={mint}", timeout=1.0) as r:
                    if r.status == 200:
                        d = await r.json()
                        row = (d or {}).get(mint)
                        if isinstance(row, dict) and row.get("usdPrice") is not None:
                            jup_px = float(row["usdPrice"])
            except Exception:
                pass
            if jup_px is not None and jup_px > 0:
                # Cross-check: Jupiter Price API v3 aggregates from ALL sources, including the
                # pump.fun bonding curve, which can report the pre-dump completion price after
                # migration — significantly higher than the actual Raydium pool price.
                # If Jupiter > 3× DexScreener's Raydium-preferred price, discard Jupiter.
                try:
                    ds_px = await asyncio.wait_for(ds_task, timeout=1.0)
                    if ds_px is not None and ds_px > 0 and jup_px > ds_px * 3.0:
                        log.warning(
                            "[MARK] jup price/v3 %.10g >3x dexscreener %.10g mint=%s — "
                            "discarding Jupiter (pump curve contamination), using DexScreener",
                            jup_px, ds_px, mint,
                        )
                        return ds_px
                except Exception:
                    pass
                finally:
                    if not ds_task.done():
                        ds_task.cancel()
                return jup_px
            # Jupiter failed — try to harvest DexScreener result
            try:
                ds_px = await asyncio.wait_for(ds_task, timeout=1.0)
                return ds_px
            except Exception:
                pass
        except Exception:
            pass
        return None

    async def _dexscreener_price(self, mint: str) -> Optional[float]:
        """Fast DexScreener price lookup using shared session. Bypasses Jupiter lock."""
        try:
            s = await self._get_http_session()
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with s.get(url, timeout=1.0) as r:
                if r.status != 200:
                    return None
                d = await r.json()
            pairs = d.get("pairs") or []
            if not pairs:
                return None
            # Prefer Raydium, then any non-pump pair, sorted by liquidity
            pairs.sort(key=lambda p: float(((p.get("liquidity") or {}).get("usd") or 0)), reverse=True)
            for p in pairs:
                dex = str(p.get("dexId") or "").lower()
                if "raydium" in dex:
                    px = p.get("priceUsd")
                    if px is not None:
                        return float(px)
            for p in pairs:
                if "pump" in str(p.get("dexId") or "").lower():
                    continue
                px = p.get("priceUsd")
                if px is not None:
                    return float(px)
        except Exception:
            pass
        return None

    async def _price_race(self, mint: str) -> Optional[float]:
        """Race DexScreener vs Jupiter price API — collect both, then apply 3x sanity check.

        Fires both concurrently. Waits up to 1.5s for both results, then:
        - If Jupiter > 3× DexScreener, returns DexScreener (guards pump.fun bonding curve
          contamination in Jupiter Price API v2 for recently migrated tokens).
        - Otherwise returns the first valid result.
        Note: _token_price() already does this cross-check internally; _price_race collects
        both independently so it can apply the check even when _token_price's ds_task is slow.
        """
        ds_task = asyncio.create_task(self._dexscreener_price(mint))
        jup_task = asyncio.create_task(self._token_price(mint))
        all_tasks = {ds_task, jup_task}
        jup_px: Optional[float] = None
        ds_px: Optional[float] = None
        try:
            done, pending = await asyncio.wait(all_tasks, timeout=1.5, return_when=asyncio.ALL_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                try:
                    result = t.result()
                    if t is jup_task:
                        jup_px = result
                    else:
                        ds_px = result
                except Exception:
                    pass
        except Exception:
            for t in all_tasks:
                t.cancel()
        # Cross-check: if Jupiter > 3× DexScreener, prefer DexScreener
        if jup_px is not None and jup_px > 0 and ds_px is not None and ds_px > 0:
            if jup_px > ds_px * 3.0:
                log.warning(
                    "[PRICE_RACE] jup %.10g >3x dex %.10g mint=%s — using DexScreener",
                    jup_px, ds_px, mint,
                )
                return ds_px
            return jup_px  # both valid and close — Jupiter wins (faster path)
        if ds_px is not None and ds_px > 0:
            return ds_px
        if jup_px is not None and jup_px > 0:
            return jup_px
        return None

    async def _executable_mark_price(self, mint: str, qty_tokens: int) -> Optional[float]:
        """
        Executable mark price: ask Jupiter what we'd actually receive selling qty_tokens right now.
        outAmount (lamports SOL) / sol_usd → position_value_usd → price_per_token = value / qty.
        Returns None on any failure; caller falls back to non-executable price paths.

        qty_tokens is in WHOLE tokens (how positions are stored internally).
        Jupiter's `amount` param requires BASE UNITS (×10^6 for 6-decimal SPL mints).
        """
        if not EXECUTABLE_MARK_ENABLED or qty_tokens <= 0:
            return None
        try:
            from live_canary import get_jupiter_quote, WSOL_MINT as _WSOL
            _TOKEN_DECIMALS = 6  # standard for pump.fun SPL mints
            amount_base_units = qty_tokens * (10 ** _TOKEN_DECIMALS)
            quote = await get_jupiter_quote(
                input_mint=mint,
                output_mint=_WSOL,
                amount=amount_base_units,
                slippage_bps=EXECUTABLE_MARK_SLIPPAGE_BPS,
            )
            if not quote:
                return None
            out_lamports = int(quote.get("outAmount", 0))
            if out_lamports <= 0:
                return None
            sol_usd = await self._sol_usd()
            if not sol_usd or sol_usd <= 0:
                return None
            position_value_usd = (out_lamports / 1e9) * sol_usd
            # Divide by whole-token count (not base units) → price per whole token
            return position_value_usd / qty_tokens
        except Exception:
            return None

    async def _pre_entry_swap_route_check(self, mint: str, reference_price_usd: float) -> bool:
        """Verify Jupiter can route a sell for this token BEFORE we buy it.

        Returns True if a swap route exists (safe to enter), False if not.
        This prevents entering tokens still on the pump.fun bonding curve
        with no Raydium pool — which caused 100% QUOTEABILITY_GUARD_EXIT
        in the 7-trade canary.

        Uses a direct HTTP call (no Jupiter lock) so route probes never
        block behind mark-refresh calls that hold the shared lock.
        """
        if not ENTRY_SWAP_ROUTE_CHECK_ENABLED:
            return True
        try:
            from live_canary import WSOL_MINT as _WSOL, JUPITER_QUOTE_API, JUPITER_API_KEY
            # Compute probe amount in TOKEN BASE UNITS (pump.fun tokens = 6 decimals).
            # Jupiter's `amount` param is always in smallest units, never whole tokens.
            _TOKEN_DECIMALS = 6  # standard for pump.fun SPL mints
            if reference_price_usd and reference_price_usd > 0:
                # ~$0.10 worth in base units
                probe_amount = max(10 ** _TOKEN_DECIMALS, int(0.10 / reference_price_usd * 10 ** _TOKEN_DECIMALS))
            else:
                # fallback: 10 whole tokens
                probe_amount = 10 * 10 ** _TOKEN_DECIMALS
            params = {
                "inputMint": mint,
                "outputMint": _WSOL,
                "amount": str(probe_amount),
                "slippageBps": "500",
                "onlyDirectRoutes": "false",
                "asLegacyTransaction": "false",
            }
            headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
            timeout = aiohttp.ClientTimeout(total=ENTRY_SWAP_ROUTE_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(JUPITER_QUOTE_API, params=params) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning("[SWAP_ROUTE_CHECK] HTTP %s for mint=%s body=%s",
                                    resp.status, mint, body[:120])
                        return False
                    data = await resp.json(content_type=None)
                    out_amount = int(data.get("outAmount", 0))
                    log.info("[SWAP_ROUTE_CHECK] mint=%s probe_amount=%d outAmount=%d routable=%s",
                             mint, probe_amount, out_amount, out_amount > 0)
                    return out_amount > 0
        except asyncio.TimeoutError:
            log.warning("[SWAP_ROUTE_CHECK] timeout for mint=%s", mint)
            return False
        except Exception as e:
            log.warning("[SWAP_ROUTE_CHECK] error for mint=%s: %s", mint, e)
            return False

    def _mark_quote_qty_tokens(self, tr: PaperTrade) -> tuple[int, float]:
        qty_remaining = max(0.0, float(tr.qty_remaining or 0.0))
        if qty_remaining <= 0.0:
            return 0, 0.0
        ref_px = max(
            float(tr.last_price or 0.0),
            float(tr.current_px or 0.0),
            float(tr.entry_px or 0.0),
            0.0,
        )
        fraction_qty = qty_remaining * max(0.0, EXECUTABLE_MARK_POSITION_FRACTION)
        notional_qty = (EXECUTABLE_MARK_NOTIONAL_USD / ref_px) if ref_px > 0 else 0.0
        candidates = [q for q in (fraction_qty, notional_qty) if q and q > 0]
        if candidates:
            quote_qty = min(candidates)
        else:
            quote_qty = qty_remaining
        qty_tokens = max(EXECUTABLE_MARK_MIN_TOKENS, int(quote_qty))
        qty_tokens = min(int(qty_remaining), qty_tokens) if qty_remaining >= 1.0 else max(1, qty_tokens)
        if qty_tokens <= 0 and qty_remaining > 0:
            qty_tokens = 1
        mark_notional_usd_used = float(qty_tokens) * ref_px if ref_px > 0 else 0.0
        return qty_tokens, mark_notional_usd_used

    async def _price_from_pool_reserves(self, mint: str) -> Optional[float]:
        # Fallback when Jupiter is null: derive from live pool data feed.
        # Uses DexScreener pool snapshot (Raydium/other pools) as reserve-backed proxy.
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            s = await self._get_http_session()
            async with s.get(url, timeout=6) as r:
                    if r.status != 200:
                        return None
                    d = await r.json()
            pairs = d.get("pairs") or []
            if not pairs:
                return None

            # Prefer Raydium + highest liquidity.
            pairs.sort(key=lambda p: float(((p.get("liquidity") or {}).get("usd") or 0)), reverse=True)
            best = None
            for p in pairs:
                dex = str(p.get("dexId") or "").lower()
                if "raydium" in dex:
                    best = p
                    break
            if best is None:
                # Exclude pump.fun pairs — they represent a closed bonding curve
                # and show a stale pre-migration price that is not a live market.
                # For freshly-migrated tokens the Raydium pair may not be indexed
                # yet; returning None lets the mark stay stale rather than firing
                # a false stop on a pump.fun price.
                non_pump = [p for p in pairs if "pump" not in str(p.get("dexId") or "").lower()]
                best = non_pump[0] if non_pump else None
            if best is None:
                return None

            p_usd = (best.get("priceUsd") if isinstance(best, dict) else None)
            if p_usd is not None:
                return float(p_usd)
        except Exception:
            return None
        return None

    async def _price_from_rpc_reserves(self, mint: str) -> Optional[float]:
        """
        True on-chain reserve price: queries known Raydium authority addresses directly
        via getTokenAccountsByOwner without requiring any DEX indexer (DexScreener/Jupiter).

        Reads token and WSOL balances held by known Raydium pool authority accounts.
        Works immediately after pool creation — no indexing lag.

        The key known authority for pump.fun migrations is:
          39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg (Pump migrator Raydium authority)
        Plus any configured RAYDIUM_AUTHORITY / RAYDIUM_CPMM_AUTHORITY env vars.
        """
        try:
            # ── Reserve-miss negative cache ───────────────────────────────────
            # If this mint exhausted all owners recently, skip the full fan-out
            # to avoid event-loop spam. Base TTL = RESERVE_MISS_SUPPRESS_SECONDS.
            # Adaptive backoff: miss_count >= 3 → 60s, miss_count >= 6 → 120s, miss_count >= 10 → 300s.
            # Hard cap: RESERVE_MISS_TOTAL_CAP (default 8) → permanently skip this session.
            _now = time.monotonic()
            _miss_entry = self._reserve_miss_cache.get(mint)
            if _miss_entry is not None:
                _miss_ts, _miss_count = _miss_entry
                # Permanent suppression: mint has missed far too many times
                if _miss_count >= RESERVE_MISS_TOTAL_CAP:
                    self._funnel["reserve_miss_suppressed"] = self._funnel.get("reserve_miss_suppressed", 0) + 1
                    return None  # permanently suppressed for this session
                if _miss_count >= 10:
                    _effective_ttl = 300.0
                elif _miss_count >= 6:
                    _effective_ttl = 120.0
                elif _miss_count >= 3:
                    _effective_ttl = 60.0
                else:
                    _effective_ttl = RESERVE_MISS_SUPPRESS_SECONDS
                if _now - _miss_ts < _effective_ttl:
                    self._funnel["reserve_miss_suppressed"] = self._funnel.get("reserve_miss_suppressed", 0) + 1
                    return None  # suppress without RPC work
                else:
                    # TTL expired — remove stale entry and allow a fresh attempt
                    del self._reserve_miss_cache[mint]
            # ─────────────────────────────────────────────────────────────────

            sol_usd = await self._sol_usd()
            # No DexScreener prerequisite — attempt on-chain read directly.
            pair_owners = [o for o in RAYDIUM_OWNER_CANDIDATES if o]

            async def _sum_balance_for_owner(owner: str, target_mint: str) -> float:
                req = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [owner, {"mint": target_mint}, {"encoding": "jsonParsed", "commitment": "processed"}],
                }
                status, data = await self._rpc_post(req, timeout=6)
                if status != 200:
                    return 0.0
                vals = ((data or {}).get("result") or {}).get("value") or []
                total = 0.0
                for row in vals:
                    amt = ((((row.get("account") or {}).get("data") or {}).get("parsed") or {})
                           .get("info", {}).get("tokenAmount", {}).get("uiAmount"))
                    try:
                        total += float(amt or 0.0)
                    except Exception:
                        pass
                return total

            # Parallel reserve queries across all candidate owners (was sequential — 8x speedup)
            async def _price_for_owner(owner: str) -> Optional[float]:
                token_reserve = await _sum_balance_for_owner(owner, mint)
                if token_reserve <= 0:
                    return None
                sol_reserve = await _sum_balance_for_owner(owner, WSOL_MINT)
                if sol_reserve > 0:
                    return (sol_reserve / token_reserve) * sol_usd
                usdc_reserve = await _sum_balance_for_owner(owner, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
                if usdc_reserve > 0:
                    return usdc_reserve / token_reserve
                return None

            results = await asyncio.gather(
                *[_price_for_owner(o) for o in pair_owners[:8]],
                return_exceptions=True,
            )

            best_price = None
            owner_hits = 0
            for px in results:
                if isinstance(px, Exception) or px is None:
                    continue
                owner_hits += 1
                if px > 0:
                    best_price = px if best_price is None else min(best_price, px)

            if owner_hits == 0:
                # Write / update negative cache entry
                _prior = self._reserve_miss_cache.get(mint)
                if _prior is not None and (time.monotonic() - _prior[0]) < RESERVE_MISS_SUPPRESS_SECONDS:
                    _new_count = _prior[1] + 1
                    self._reserve_miss_cache[mint] = (_prior[0], _new_count)
                else:
                    _new_count = 1
                    self._reserve_miss_cache[mint] = (time.monotonic(), 1)
                    self._funnel["reserve_miss_unique_mints"] = self._funnel.get("reserve_miss_unique_mints", 0) + 1
                self._funnel["reserve_miss_total"] = self._funnel.get("reserve_miss_total", 0) + 1
                # Only emit telemetry on first miss per suppress window AND under session cap
                _session_miss_total = self._funnel.get("reserve_miss_telemetry_total", 0)
                if _new_count <= RESERVE_MISS_CAP_PER_WINDOW and _session_miss_total < RESERVE_MISS_TELEMETRY_SESSION_CAP:
                    self._funnel["reserve_miss_telemetry_total"] = _session_miss_total + 1
                    append_telemetry_event(
                        "RESERVE_ALL_OWNERS_MISS", mint=mint,
                        detail=f"raydium owners exhausted (miss #{_new_count} in window)",
                        severity="warn",
                    )
                # else: suppress — window cap or session cap reached
            return best_price
        except Exception:
            return None

    async def _price_from_pump_curve(self, mint: str) -> Optional[float]:
        # Pump.fun migration fallback estimate: fixed curve terminal market cap / total supply.
        try:
            m = str(mint or "").strip()
            if not m.endswith("pump"):
                return None
            if PUMP_TOTAL_SUPPLY <= 0:
                return None
            est = PUMP_CURVE_MARKET_CAP_USD / PUMP_TOTAL_SUPPLY
            return est if est > 0 else None
        except Exception:
            return None

    async def _price_from_graduation_curve(self) -> Optional[float]:
        """
        Compute pump.fun graduation price from the bonding curve invariant.

        Pump.fun uses a constant-product AMM bonding curve:
          - initial_virtual_sol_reserves  = 30 SOL  (30_000_000_000 lamports)
          - initial_virtual_token_reserves = 1_073_000_191 tokens
          - K = virtual_sol * virtual_tokens (constant product)
          - graduation fires when 85 SOL of real SOL has been raised

        At graduation:
          virtual_sol   = 30 + 85 = 115 SOL
          virtual_tokens = K / virtual_sol  ≈ 279_913_093 tokens
          price_lamports_per_token = virtual_sol_lamports / virtual_tokens
          price_sol_per_token = price_lamports_per_token / 1e9
          price_usd_per_token = price_sol_per_token * sol_usd

        This is the same math that governs the Raydium pool initialization price,
        since the pool is seeded directly from the bonding curve terminal state.

        Only call this for CONFIRMED migration candidates — not generic candidates,
        where using this price would mask tokens still on the bonding curve.
        """
        try:
            sol_usd = await self._sol_usd()
            if not sol_usd or sol_usd <= 0:
                return None
            VIRTUAL_SOL_LAMPORTS   = 30_000_000_000   # 30 SOL
            VIRTUAL_TOKENS_INITIAL = 1_073_000_191
            GRADUATION_SOL_LAMPORTS = 85_000_000_000  # 85 SOL raised
            K = VIRTUAL_SOL_LAMPORTS * VIRTUAL_TOKENS_INITIAL
            virtual_sol_at_graduation    = VIRTUAL_SOL_LAMPORTS + GRADUATION_SOL_LAMPORTS  # 115e9
            virtual_tokens_at_graduation = K / virtual_sol_at_graduation
            if virtual_tokens_at_graduation <= 0:
                return None
            price_lamports_per_token = virtual_sol_at_graduation / virtual_tokens_at_graduation
            price_usd = (price_lamports_per_token / 1e9) * sol_usd
            return price_usd if price_usd > 0 else None
        except Exception:
            return None

    async def _entry_reference_price_with_retries(
        self, mint: str, confirmed_migration: bool = False
    ) -> Optional[float]:
        """
        Multi-source entry price lookup with explicit source tracking.

        Hot path: DexScreener + Jupiter race concurrently (first valid wins, 1.5s).
        Fallback: RPC pool reserves (on-chain, no indexer dependency).
        Last resort: Graduation curve (confirmed-migration only).
        """
        for i in range(PRICE_RETRY_ATTEMPTS):
            # Sources 1+2 concurrently: DexScreener races Jupiter price v2.
            # New Raydium pools appear on DexScreener in 5-30s and Jupiter in 30-120s;
            # racing them means whichever is first wins rather than always waiting for both.
            p = await self._price_race(mint)
            if p is not None and p > 0:
                log.info("[PRICE_SOURCE] mint=%s source=race price=%.10g attempt=%d", mint, p, i + 1)
                return p

            # Source 3: True on-chain reserve price via getTokenAccountsByOwner
            p_rpc = await self._price_from_rpc_reserves(mint)
            if p_rpc is not None and p_rpc > 0:
                log.info("[PRICE_SOURCE] mint=%s source=rpc_reserves price=%.10g attempt=%d", mint, p_rpc, i + 1)
                return p_rpc

            if i < PRICE_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(PRICE_RETRY_DELAY_SECONDS)

        # Source 4: Graduation curve fallback — ONLY for confirmed Raydium migrations.
        # Pump.fun bonding curve terminal price ≈ initial Raydium pool price.
        # Used as reference for alive-checks (15% drop threshold) when all indexers are still lagging.
        # Do NOT use for non-migration candidates — it would mask bonding-curve tokens as quoteable.
        if confirmed_migration:
            p_grad = await self._price_from_graduation_curve()
            if p_grad is not None and p_grad > 0:
                log.info(
                    "[PRICE_SOURCE] mint=%s source=graduation_curve price=%.10g "
                    "(bonding-curve estimate — all DEX indexers still lagging)",
                    mint, p_grad,
                )
                return p_grad

        # All sources exhausted — caller receives None → price_unavailable veto or requeue.
        return None

    async def _mark_price_with_retries(self, mint: str, qty_tokens: int = 0) -> tuple[Optional[float], str, str, Optional[float]]:
        # MTM path must stay responsive: prefer fast quote APIs first, use only shallow retries,
        # and treat slow reserve-RPC pricing as a last resort so one bad quote path does not stale the book.

        # Check mark price cache first (prevents API hammering with N concurrent positions)
        now_ms = time.time() * 1000
        if mint in self._mark_price_cache:
            cached_px, cached_ts_ms = self._mark_price_cache[mint]
            if (now_ms - cached_ts_ms) <= MARK_PRICE_CACHE_TTL_MS:
                return cached_px, "mark_cache_hit", "HIGH", None

        # Executable mark: use shallow executable quote qty for mark/ROI only.
        if qty_tokens > 0:
            p_exec = await self._executable_mark_price(mint, qty_tokens)
            if p_exec is not None and p_exec > 0:
                self._mark_price_cache[mint] = (p_exec, now_ms)
                return p_exec, "executable_notional_quote", "HIGH", p_exec
        debug = bool(MARK_DEBUG_MINT and mint == MARK_DEBUG_MINT)
        for i in range(MARK_PRICE_RETRY_ATTEMPTS):
            t0 = time.perf_counter()
            p = await self._token_price(mint)
            if debug:
                log.warning(f"[MARK_DEBUG] mint={mint} source=jup attempt={i+1} latency_ms={(time.perf_counter()-t0)*1000:.1f} result={'ok' if p else 'null'} price={p}")
            if p is not None and p > 0:
                self._mark_price_cache[mint] = (p, now_ms)
                return p, "mid_price", "MED", None

            # DexScreener direct price — fast, doesn't go through Jupiter lock,
            # catches newly-listed tokens that Jupiter hasn't indexed yet.
            t_ds = time.perf_counter()
            p_ds = await self._dexscreener_price(mint)
            if debug:
                log.warning(f"[MARK_DEBUG] mint={mint} source=dexscreener attempt={i+1} latency_ms={(time.perf_counter()-t_ds)*1000:.1f} result={'ok' if p_ds else 'null'} price={p_ds}")
            if p_ds is not None and p_ds > 0:
                self._mark_price_cache[mint] = (p_ds, now_ms)
                return p_ds, "dexscreener", "MED", None
            # NOTE: _price_from_rpc_reserves is intentionally omitted from the mark path.
            # It fans out to getTokenAccountsByOwner across multiple Raydium authorities —
            # slow, almost never succeeds for active positions (which DexScreener/Jupiter
            # already cover), and generates RESERVE_ALL_OWNERS_MISS spam every mark cycle
            # when the authority list doesn't match the token's pool layout.

            if i < MARK_PRICE_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(MARK_PRICE_RETRY_DELAY_SECONDS)
        return None, "stale", "LOW", None

    async def _token_price_with_retries(self, mint: str) -> Optional[float]:
        return await self._entry_reference_price_with_retries(mint)

    async def _refresh_graveyard(self):
        if time.time() - self._last_graveyard_refresh < 60:
            return
        self._last_graveyard_refresh = time.time()
        rows = []
        if VETO_LOG_FILE.exists():
            try:
                lines = VETO_LOG_FILE.read_text().splitlines()
                for ln in lines[-50:]:
                    try:
                        rows.append(json.loads(ln))
                    except Exception:
                        pass
            except Exception:
                pass
        last5 = rows[-5:]
        out = []
        for r in last5:
            p = await self._token_price(r.get("mint", ""))
            out.append({
                "mint": r.get("mint"),
                "reason": r.get("reason"),
                "ts": r.get("ts"),
                "current_price": p,
                "moon_candidate": p is not None and p > 0,
            })
        write_state(graveyard=out)

    async def _record_shadow_watch(
        self,
        mint: str,
        veto_reason: str,
        veto_price: Optional[float],
        historical_threshold_usd: Optional[float] = None,
        current_policy_threshold_usd: Optional[float] = None,
    ):
        st = read_state()
        rows = st.get("shadow_watch") or []
        ts_now = int(time.time())
        effective_current_policy = float(current_policy_threshold_usd if current_policy_threshold_usd is not None else self.current_liquidity_usd)
        effective_historical = float(historical_threshold_usd if historical_threshold_usd is not None else effective_current_policy)
        rows.append(
            {
                "mint": mint,
                "reason": veto_reason,
                "veto_ts": ts_now,
                "veto_price": veto_price,
                "historical_threshold_usd": effective_historical,
                "current_policy_threshold_usd": effective_current_policy,
                "historical_threshold_context_only": False,
                "t5_due": ts_now + 300,
                "t30_due": ts_now + 1800,
                "t5_price": None,
                "t30_price": None,
            }
        )
        write_state(shadow_watch=rows[-500:])

    async def _shadow_watch_loop(self):
        # Max retries per shadow-watch price slot. After this many failed lookups
        # (spread over backoff intervals) the slot is abandoned (price set to 0.0).
        # 0.0 is falsy → missed_roi stays None; no spam past this point.
        _SW_MAX_TRIES = 6
        _SW_BACKOFF_SECONDS = 60  # push due-time forward by this amount on each miss
        while True:
            st = read_state()
            rows = st.get("shadow_watch") or []
            changed = False
            now = int(time.time())
            for row in rows:
                mint = row.get("mint")
                if not mint:
                    continue
                if row.get("t5_price") is None and now >= int(row.get("t5_due", 0) or 0):
                    _t5_tries = int(row.get("t5_tries", 0) or 0)
                    if _t5_tries >= _SW_MAX_TRIES:
                        row["t5_price"] = 0.0  # exhausted — stop retrying
                    else:
                        p = await self._token_price_with_retries(mint)
                        row["t5_tries"] = _t5_tries + 1
                        if p is not None:
                            row["t5_price"] = p
                        else:
                            row["t5_due"] = now + _SW_BACKOFF_SECONDS
                    changed = True
                if row.get("t30_price") is None and now >= int(row.get("t30_due", 0) or 0):
                    _t30_tries = int(row.get("t30_tries", 0) or 0)
                    if _t30_tries >= _SW_MAX_TRIES:
                        row["t30_price"] = 0.0  # exhausted — stop retrying
                    else:
                        p = await self._token_price_with_retries(mint)
                        row["t30_tries"] = _t30_tries + 1
                        if p is not None:
                            row["t30_price"] = p
                        else:
                            row["t30_due"] = now + _SW_BACKOFF_SECONDS
                    changed = True

            if changed:
                graveyard = []
                for row in rows[-50:]:
                    vp = row.get("veto_price")
                    p5 = row.get("t5_price")
                    p30 = row.get("t30_price")
                    # 0.0 is the exhausted-sentinel (>= _SW_MAX_TRIES failed lookups);
                    # treat it as "no data" for all downstream calculations.
                    _p5_real = p5 if (p5 and p5 > 0) else None
                    _p30_real = p30 if (p30 and p30 > 0) else None
                    missed_roi = None
                    if vp and _p30_real:
                        missed_roi = ((_p30_real - vp) / vp) * 100.0
                    graveyard.append(
                        {
                            "mint": row.get("mint"),
                            "reason": row.get("reason"),
                            "ts": row.get("veto_ts"),
                            "historical_threshold_usd": row.get("historical_threshold_usd"),
                            "current_policy_threshold_usd": row.get("current_policy_threshold_usd"),
                            "historical_threshold_context_only": bool(row.get("historical_threshold_context_only", False)),
                            "current_price": _p30_real if _p30_real is not None else _p5_real,
                            "missed_roi_pct": round(missed_roi, 2) if missed_roi is not None else None,
                            "moon_candidate": bool(missed_roi is not None and missed_roi >= 100.0),
                        }
                    )
                write_state(shadow_watch=rows, graveyard=graveyard)
            await asyncio.sleep(10)

    async def _refresh_trade_mark_bounded(self, tr: PaperTrade) -> bool:
        async with self._mtm_semaphore:
            try:
                return await asyncio.wait_for(self._refresh_trade_mark(tr), timeout=4.0)
            except Exception:
                return False

    async def live_monitor_loop(self):
        # Legacy background loop kept only as a lightweight state publisher.
        while True:
            write_state(active_positions=self._active_positions_snapshot())
            await asyncio.sleep(10)

    async def open_position_refresh_loop(self):
        while True:
            now = time.time()
            open_trades = []
            for tr in self._trades_snapshot():
                if tr.closed:
                    continue
                if self._terminalize_trade(tr):
                    continue
                open_trades.append(tr)
            for mint in list(self._open_position_next_due.keys()):
                if mint not in {tr.mint for tr in open_trades}:
                    self._open_position_next_due.pop(mint, None)
            due_trades = []
            for tr in open_trades:
                due_at = float(self._open_position_next_due.get(tr.mint, 0.0) or 0.0)
                if due_at <= now:
                    due_trades.append(tr)
            if due_trades:
                results = await asyncio.gather(*(self._refresh_trade_mark_bounded(tr) for tr in due_trades), return_exceptions=False)
                for tr, updated in zip(due_trades, results):
                    if tr.current_px > tr.high_px:
                        tr.high_px = tr.current_px
                    self._open_position_next_due[tr.mint] = time.time() + OPEN_POSITION_REFRESH_INTERVAL_SECONDS
                write_state(active_positions=self._active_positions_snapshot())
            await asyncio.sleep(1)

    async def helius_mutable_veto(self, mint: str) -> tuple[bool, str]:
        if mint.startswith("SIM_"):
            return False, "sim_bypass"
        if not HELIUS_RPC_URL:
            return False, "helius_api_key_missing"
        body = {"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": {"id": mint}}
        try:
            s = await self._get_http_session()
            _hmv_ct = aiohttp.ClientTimeout(total=HELIUS_MUTABLE_TIMEOUT_SECONDS, sock_read=HELIUS_MUTABLE_TIMEOUT_SECONDS)
            async with s.post(HELIUS_RPC_URL, json=body, timeout=_hmv_ct) as r:
                if r.status != 200:
                    # Non-200 = RPC error; fail-open with warning (not veto)
                    log.warning("[HELIUS_MUTABLE] HTTP %d for mint=%s — fail-open (non-DAS RPC?)", r.status, mint)
                    return False, f"helius_http_{r.status}_fail_open"
                d = await r.json()
            # Check if RPC supports DAS (getAsset) — non-DAS returns result=null
            result = d.get("result")
            if result is None:
                # Non-DAS RPC: getAsset not supported. Fail-open with warning.
                log.warning("[HELIUS_MUTABLE] getAsset returned null for mint=%s — RPC is not DAS-capable, fail-open", mint)
                return False, "helius_not_das_fail_open"
            mutable = ((result.get("content") or {}).get("metadata") or {}).get("isMutable")
            if mutable is True:
                return True, "Mutable Metadata"
            return False, "ok"
        except Exception as e:
            # Network error: fail-open with warning (was: veto, which blocked all entries)
            log.warning("[HELIUS_MUTABLE] error for mint=%s: %s — fail-open", mint, e)
            return False, f"helius_error_fail_open:{e.__class__.__name__}"

    async def _onchain_fast_veto(self, mint: str) -> tuple[bool, str]:
        """Fast on-chain authority check via getAccountInfo.
        Returns (veto, reason). veto=True means BLOCK the trade.
        Used as a fallback when rugcheck.xyz is unavailable — if mintAuthority
        and freezeAuthority are both revoked on-chain, the token is safe to buy.
        """
        if not ONCHAIN_VETO_ENABLED:
            return True, "onchain_veto_disabled"
        if mint.startswith("SIM_"):
            return False, "sim_bypass"
        body = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        }
        try:
            s = await self._get_http_session()
            _oc_ct = aiohttp.ClientTimeout(total=ONCHAIN_VETO_TIMEOUT_SECONDS, sock_read=ONCHAIN_VETO_TIMEOUT_SECONDS)
            async with s.post(self.rpc_url, json=body, timeout=_oc_ct) as r:
                if r.status != 200:
                    return True, f"onchain_http_{r.status}"
                d = await r.json()
            parsed = (((d.get("result") or {}).get("value") or {}).get("data") or {}).get("parsed") or {}
            info = parsed.get("info") or {}
            mint_authority = info.get("mintAuthority")
            freeze_authority = info.get("freezeAuthority")
            if mint_authority is not None:
                return True, f"onchain_mint_authority_set:{mint_authority}"
            if freeze_authority is not None:
                return True, f"onchain_freeze_authority_set:{freeze_authority}"
            return False, "onchain_authorities_revoked"
        except Exception as e:
            return True, f"onchain_error:{e.__class__.__name__}"

    async def _holder_concentration_veto(self, mint: str) -> tuple[bool, str]:
        """Check top-5 holder concentration via Helius getTokenLargestAccounts.
        Returns (veto, reason). veto=True means BLOCK the trade.
        Enabled by HOLDER_CHECK_ENABLED=true (off by default to avoid latency).
        """
        if not HOLDER_CHECK_ENABLED:
            return False, "holder_check_disabled"
        if mint.startswith("SIM_"):
            return False, "sim_bypass"
        body = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [mint, {"commitment": "confirmed"}],
        }
        try:
            s = await self._get_http_session()
            _hc_ct = aiohttp.ClientTimeout(total=HOLDER_CHECK_TIMEOUT_SECONDS, sock_read=HOLDER_CHECK_TIMEOUT_SECONDS)
            _hc_rpc = _next_read_rpc_url() or self.rpc_url
            async with s.post(_hc_rpc, json=body, timeout=_hc_ct) as r:
                if r.status != 200:
                    # P3.6: on 429 (or other rate-limit codes), mark the endpoint
                    # so the pool skips it for READ_RPC_COOLDOWN_SECONDS instead
                    # of round-robining back to it on the very next call.
                    if r.status in (429, 503):
                        _mark_read_rpc_throttled(_hc_rpc)
                    return False, f"holder_check_http_{r.status}"
                d = await r.json()
            accounts = ((d.get("result") or {}).get("value") or [])
            if not accounts:
                return False, "holder_check_no_data"
            total = sum(int(a.get("amount") or 0) for a in accounts)
            if total <= 0:
                return False, "holder_check_zero_supply"
            top5_amount = sum(int(a.get("amount") or 0) for a in accounts[:5])
            top5_pct = (top5_amount / total) * 100.0
            if top5_pct > TOP5_HOLDER_PCT_MAX:
                return True, f"holder_concentration_{top5_pct:.0f}pct_top5"
            return False, f"holder_ok_{top5_pct:.0f}pct_top5"
        except Exception as e:
            return False, f"holder_check_error:{e.__class__.__name__}"

    async def _score_migration_candidate(self, mint: str, ts_detected: float) -> dict:
        """Pre-fetch score components via a single DexScreener token-pairs call.
        Runs asynchronously at T=0 (migration enqueued) during the delayed-entry
        wait. Consumer adds the remaining components (DexScreener price, holder)
        via _finalize_entry_score — total added latency to execution path is 0ms.

        Returns a partial score dict. Keys:
          time_since_detection_s, time_to_graduate_pts, social_pts, fetch_error.
        Scoring uses wall-clock time between first detection (ts_detected) and
        now as a reliable proxy for bonding-curve age — DexScreener pairCreatedAt
        had ~1% coverage and is no longer consulted for the time component.
        The DexScreener HTTP call is retained purely to harvest info.socials for
        social_pts. Neutral 20 pts preserved on fetch failure.
        """
        # NOTE: time_since_detection_s / time_to_graduate_pts intentionally left as
        # neutral/None here. This task runs at T=0 (enqueue time), so `time.time() -
        # ts_detected` is ~0 — computing it here yields a 0-pt graduation score for
        # every entry. Both fields are recomputed in _finalize_entry_score at the
        # consumer (T+30) using the queue item's first_ts as the delay proxy.
        partial = {
            "time_since_detection_s": None,
            "time_to_graduate_pts": 20,  # neutral — overwritten in _finalize_entry_score
            "social_pts": 0,
            "fetch_error": None,
        }
        if mint.startswith("SIM_"):
            partial["fetch_error"] = "sim_bypass"
            return partial
        url = f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}"
        _dbg_should_log = self._score_debug_logged < 3
        _dbg_status: Optional[int] = None
        _dbg_resp_len: Optional[int] = None
        _dbg_pairs_count: Optional[int] = None
        try:
            s = await self._get_http_session()
            ct = aiohttp.ClientTimeout(
                total=ENTRY_SCORE_FETCH_TIMEOUT_SECONDS,
                sock_read=ENTRY_SCORE_FETCH_TIMEOUT_SECONDS,
            )
            async with s.get(url, timeout=ct) as r:
                _dbg_status = r.status
                if r.status != 200:
                    partial["fetch_error"] = f"http_{r.status}"
                    if _dbg_should_log:
                        self._score_debug_logged += 1
                        log.warning(
                            "[SCORE_DEBUG] mint=%s status=%s pairs_count=None fetch_err=http_%s",
                            mint, r.status, r.status,
                        )
                    return partial
                d = await r.json()
                try:
                    _dbg_resp_len = len(d) if hasattr(d, "__len__") else None
                except Exception:
                    _dbg_resp_len = None
            # DexScreener token-pairs v1 returns a bare list of pair objects.
            # Tolerate the older {"pairs": [...]} shape defensively.
            if isinstance(d, list):
                pairs = d
            elif isinstance(d, dict):
                pairs = d.get("pairs") or []
            else:
                pairs = []
            _dbg_pairs_count = len(pairs)
            if not pairs:
                partial["fetch_error"] = "no_pairs"
                if _dbg_should_log:
                    self._score_debug_logged += 1
                    log.warning(
                        "[SCORE_DEBUG] mint=%s status=%s pairs_count=0 fetch_err=no_pairs resp_len=%s",
                        mint, _dbg_status, _dbg_resp_len,
                    )
                return partial
            pair = pairs[0] or {}

            # Component 4: socials — same DexScreener response exposes info.socials
            # and info.websites. Extracting them here is a free ride on the same call.
            info = pair.get("info") or {}
            socials = info.get("socials") or []
            websites = info.get("websites") or []
            _needles = ("twitter.com", "x.com", "t.me", "telegram")
            social_present = False
            for entry in list(socials) + list(websites):
                if not isinstance(entry, dict):
                    continue
                u = str(entry.get("url") or "").lower()
                if any(n in u for n in _needles):
                    social_present = True
                    break
                t = str(entry.get("type") or "").lower()
                if t in ("twitter", "telegram"):
                    social_present = True
                    break
            if social_present:
                partial["social_pts"] = 10
            if _dbg_should_log:
                self._score_debug_logged += 1
                log.warning(
                    "[SCORE_DEBUG] mint=%s status=%s pairs_count=%s fetch_err=%s resp_len=%s",
                    mint, _dbg_status, _dbg_pairs_count,
                    partial.get("fetch_error"), _dbg_resp_len,
                )
            return partial
        except asyncio.TimeoutError:
            partial["fetch_error"] = "timeout"
            if _dbg_should_log:
                self._score_debug_logged += 1
                log.warning(
                    "[SCORE_DEBUG] mint=%s status=%s pairs_count=%s fetch_err=timeout resp_len=%s",
                    mint, _dbg_status, _dbg_pairs_count, _dbg_resp_len,
                )
            return partial
        except Exception as e:
            partial["fetch_error"] = f"{e.__class__.__name__}:{e}"
            if _dbg_should_log:
                self._score_debug_logged += 1
                log.warning(
                    "[SCORE_DEBUG] mint=%s status=%s pairs_count=%s fetch_err=%s resp_len=%s",
                    mint, _dbg_status, _dbg_pairs_count,
                    partial["fetch_error"], _dbg_resp_len,
                )
            return partial

    @staticmethod
    def _finalize_entry_score(
        partial: dict,
        dex_price_verified: bool,
        holder_reason: Optional[str],
        delay_s: float = 0.0,
    ) -> dict:
        """Combine the pre-fetched partial score with runtime values (DexScreener
        price availability and holder_reason) known only at T+30. Returns a dict
        safe to serialise into signal_accept history events.

        delay_s is time.time() - item["first_ts"] measured at the consumer, i.e.
        how long the mint has sat in the queue. It replaces the T=0 `age` computed
        inside _score_migration_candidate (which was always ~0 and scored 0 pts).
        """
        out = dict(partial or {})
        age = max(0.0, float(delay_s or 0.0))
        out["time_since_detection_s"] = round(age, 1)
        # Flipped: advisor feedback + data review showed older tokens underperform
        # (winners cluster at 0-10s, losers at 10-15s+; beyond 15s WR collapses).
        # Previous inverted weighting rewarded older tokens with up to 40 pts.
        # Now the score rewards FRESH detections while still gating out pre-migration
        # (<5s) entries that can't settle.
        if age < 5:
            graduation_pts = 0
        elif age <= 30:
            graduation_pts = 40
        elif age <= 60:
            graduation_pts = 20
        elif age <= 120:
            graduation_pts = 10
        else:
            graduation_pts = 0
        out["time_to_graduate_pts"] = graduation_pts
        out["dexscreener_pts"] = 30 if dex_price_verified else 0

        # Holder component: parse top-5 pct from holder_reason, apply thresholds.
        # holder_reason produced by _holder_concentration_veto is one of:
        #   holder_ok_{pct}pct_top5 / holder_concentration_{pct}pct_top5
        #   holder_check_disabled / sim_bypass / holder_check_no_data
        #   holder_check_error:... / holder_check_http_{status}
        holder_pct: Optional[float] = None
        if holder_reason:
            import re as _re_score
            m = _re_score.search(r"(\d+(?:\.\d+)?)pct_top5", holder_reason)
            if m:
                try:
                    holder_pct = float(m.group(1))
                except ValueError:
                    holder_pct = None
        # No data = 0 pts (previously 10 pts "neutral fallback" silently rubber-stamped
        # trades that lacked holder data — drove the observed score=50 default that
        # matched the threshold exactly, so the gate was effectively dead).
        if holder_pct is None:
            holder_pts = 0
        elif holder_pct < 30.0:
            holder_pts = 20
        elif holder_pct <= 45.0:
            holder_pts = 15
        elif holder_pct <= 55.0:
            holder_pts = 5
        else:
            holder_pts = 0  # >55% is penalized; >60% is vetoed upstream by TOP5_HOLDER_PCT_MAX
        out["holder_pct_top5"] = holder_pct
        out["holder_pts"] = holder_pts

        out["total"] = int(
            (out.get("time_to_graduate_pts") or 0)
            + (out.get("dexscreener_pts") or 0)
            + holder_pts
            + (out.get("social_pts") or 0)
        )
        return out

    def _kick_entry_score_task(self, mint: str, ts_detected: float) -> None:
        """Start the pre-fetch scoring coroutine at T=0 if not already running.
        Idempotent: repeated enqueues of the same mint reuse the in-flight task."""
        if not ENTRY_SCORE_ENABLED:
            return
        existing = self._migration_scores.get(mint)
        if existing is not None and not existing.done():
            return
        try:
            self._migration_scores[mint] = asyncio.create_task(
                self._score_migration_candidate(mint, ts_detected)
            )
        except RuntimeError:
            # No running loop — skip scoring (safe: finalize treats missing score as neutral)
            pass

    @staticmethod
    def _extract_lp_lock_data(d: dict) -> tuple[float, int, float, bool]:
        """Extract LP lock data from rugcheck markets[] response.
        Used by the lp_pct_threshold research lock policy.
        Returns (lpLockedPct, lpUnlocked, lpLockedUSD, is_pumpfun).
        Safe against missing/malformed fields — returns conservative defaults on error.
        """
        try:
            launchpad = d.get("launchpad") or {}
            is_pumpfun = str(launchpad.get("platform", "")).lower() == "pump_fun"
            markets = d.get("markets") or []
            if not markets:
                return 0.0, 999, 0.0, is_pumpfun
            # Pick the market with the highest lpLockedUSD
            best_lp: dict = max(
                (m.get("lp") or {} for m in markets),
                key=lambda lp: float(lp.get("lpLockedUSD") or 0),
                default={},
            )
            lp_locked_pct = float(best_lp.get("lpLockedPct") or 0)
            lp_unlocked = int(best_lp.get("lpUnlocked") or 0)
            lp_locked_usd = float(best_lp.get("lpLockedUSD") or 0)
            return lp_locked_pct, lp_unlocked, lp_locked_usd, is_pumpfun
        except Exception:
            return 0.0, 999, 0.0, False

    async def rug_check(self, mint: str) -> tuple[bool, str]:
        if mint.startswith("SIM_"):
            return True, "sim_bypass"
        if is_excluded_mint(mint):
            return False, "excluded_mint"

        try:
            sol_usd = await self._sol_usd()
            s = await self._get_http_session()
            async with s.get(f"{RUGCHECK_API_BASE}/tokens/{mint}/report", timeout=aiohttp.ClientTimeout(total=4, sock_read=4)) as r:
                if r.status == 200:
                    d = await r.json()
                    mint_ok = d.get("token", {}).get("mintAuthority") is None
                    freeze_ok = d.get("token", {}).get("freezeAuthority") is None
                    liq = d.get("liquidity", {}) or {}
                    liq_safe = bool(liq.get("locked", False) or liq.get("burned", False))

                    # Rugcheck schema is inconsistent across token types:
                    # prefer explicit USD totals when present, then fall back to sol-denominated liquidity.
                    liq_usd = 0.0
                    try:
                        liq_usd = float(d.get("totalMarketLiquidity") or 0.0)
                    except Exception:
                        liq_usd = 0.0
                    if liq_usd <= 0:
                        try:
                            liq_usd = float(d.get("totalStableLiquidity") or 0.0)
                        except Exception:
                            liq_usd = 0.0
                    if liq_usd <= 0:
                        try:
                            liq_usd = float(liq.get("usd") or 0.0)
                        except Exception:
                            liq_usd = 0.0
                    if liq_usd <= 0:
                        try:
                            liq_sol = float(liq.get("sol", 0) or 0)
                            liq_usd = liq_sol * sol_usd
                        except Exception:
                            liq_usd = 0.0
                    if liq_usd <= 0:
                        try:
                            markets = d.get("markets") or []
                            liq_usd = max(float((((m.get("lp") or {}).get("lpLockedUSD") or 0.0))) for m in markets)
                        except Exception:
                            liq_usd = 0.0

                    if not mint_ok or not freeze_ok:
                        return False, "Mint/Freeze authority unsafe"
                    # Lock gate — behavior determined by LOCK_POLICY:
                    # paper         : skip lock check entirely (simulation visibility)
                    # standard      : require locked=True OR burned=True (original safe behavior)
                    # lp_pct_threshold: RESEARCH — additionally accept pump_fun tokens where
                    #   launchpad.platform=pump_fun AND lpLockedPct>=RESEARCH_LP_LOCK_PCT_MIN
                    #   AND lpUnlocked=0 AND lpLockedUSD>=MIN_LIQUIDITY_USD.
                    #   Addresses rugcheck returning liquidity:{} for pump.fun migrations.
                    if not liq_safe:  # was: EXECUTION_MODE != "paper" bypass — paper must match live filters for valid P&L
                        if LOCK_POLICY == "lp_pct_threshold":
                            lp_pct, lp_unlocked, lp_locked_usd_rc, is_pumpfun = self._extract_lp_lock_data(d)
                            # Detect RugCheck indexing lag: when markets[] is empty all LP
                            # fields come back as zero/default.  This is NOT a genuine rug
                            # signal — RugCheck simply hasn't indexed the pool yet (30-120s).
                            _all_lp_default = (lp_pct == 0.0 and lp_unlocked == 999 and lp_locked_usd_rc == 0.0)
                            research_safe = (
                                is_pumpfun
                                and lp_pct >= RESEARCH_LP_LOCK_PCT_MIN
                                and lp_unlocked == 0
                                and lp_locked_usd_rc >= self.current_liquidity_usd
                            )
                            if not research_safe:
                                if _all_lp_default:
                                    # Indexing lag: return a special reason so the caller can
                                    # re-queue with a 30s delay instead of hard-vetoing.
                                    return False, f"liquidity_lock_indexing_lag (pumpfun={is_pumpfun} pct={lp_pct:.0f} unlocked={lp_unlocked} usd={lp_locked_usd_rc:.0f})"
                                return False, f"Liquidity not locked (research_fail: pumpfun={is_pumpfun} pct={lp_pct:.0f} unlocked={lp_unlocked} usd={lp_locked_usd_rc:.0f})"
                            log.info(
                                "[LOCK_POLICY:lp_pct_threshold] PASS mint=%s pumpfun=%s lpLockedPct=%.0f lpUnlocked=%d lpLockedUSD=%.0f",
                                mint, is_pumpfun, lp_pct, lp_unlocked, lp_locked_usd_rc,
                            )
                            # falls through to USD threshold check below
                        else:
                            return False, "Liquidity not locked"
                    if liq_usd < self.current_liquidity_usd:
                        return False, f"Liquidity < ${self.current_liquidity_usd:.0f}"
                    return True, "ok"
        except Exception:
            pass

        try:
            s = await self._get_http_session()
            async with s.get(f"{SOLSNIFFER_API_BASE}/token/{mint}", timeout=aiohttp.ClientTimeout(total=4, sock_read=4)) as r:
                if r.status == 200:
                    d = await r.json()
                    ok = bool(d.get("is_safe", False))
                    return (ok, "ok" if ok else "Solsniffer unsafe")
        except Exception:
            pass

        return False, "rug_check_unavailable"

    async def _dependency_retry_loop(self):
        while True:
            rows = self._pop_due_dependency_retries()
            for row in rows:
                mint = str(row.get("mint") or "").strip()
                if not mint or mint in self.trades:
                    continue
                attempts = int(row.get("attempts", 0) or 0)
                live_price = float(row.get("live_price") or 0.0)
                first_ts = int(row.get("first_ts", int(time.time())) or int(time.time()))
                candidate_age_ms = max(0, int((time.time() - float(first_ts)) * 1000))
                if candidate_age_ms > DEPENDENCY_RETRY_MAX_AGE_MS:
                    queue_telemetry_event("DEPENDENCY_RETRY_STALE_BLOCKED", mint=mint, detail=f"attempts={attempts} age_ms={candidate_age_ms}", severity="warn")
                    append_veto(
                        mint,
                        f"dependency_retry_stale age_ms={candidate_age_ms}",
                        source="dependency_retry",
                        historical_threshold_usd=self.current_liquidity_usd,
                        current_policy_threshold_usd=self.current_liquidity_usd,
                        historical_threshold_context_only=False,
                    )
                    continue
                ok, reason = await self.rug_check(mint)
                if ok:
                    queue_telemetry_event("DEPENDENCY_FALLBACK_ALLOWED", mint=mint, detail=f"attempts={attempts+1}", severity="info")
                    signal_ts = int(time.time())
                    append_history({
                        "type": "signal_accept", "ts": signal_ts, "mint": mint,
                        "price_usd": round(live_price, 10), "mode": self.execution_mode,
                        "source": "dependency_retry", "candidate_age_ms": candidate_age_ms,
                        # Schema-consistent telemetry nulls (dependency_retry path
                        # does not run the pre-entry DexScreener liq fetch).
                        "liquidity_usd": None, "market_cap_usd": None, "fdv_usd": None,
                        "price_usd_at_signal": round(live_price, 10),
                        "price_usd_at_entry_quote": None,
                        "price_change_m5_pct": None, "price_change_h1_pct": None,
                        "volume_m5_usd": None, "volume_h1_usd": None,
                        "buy_count_m5": None, "sell_count_m5": None,
                        "buy_count_h1": None, "sell_count_h1": None,
                        "pair_created_at_ms": None,
                        "quote_to_fill_slippage_pct": None,
                        "route_available_at_signal": None,
                        "route_available_at_entry": None,
                        "pre_entry_structure": "unknown",
                        "local_pullback_depth_pct": None,
                        "local_spike_height_pct": None,
                        "reference_price_is_real": None,
                        "entry_price_source": None,
                        "price_change_10s_pct_pre_entry": None,
                        "price_change_30s_pct_pre_entry": None,
                        "volume_10s_pre_entry": None,
                        "volume_30s_pre_entry": None,
                        "volatility_10s_pre_entry": None,
                        "volatility_30s_pre_entry": None,
                        "dev_holder_pct": None,
                        "sniper_holder_pct": None,
                        "unique_holders_count": None,
                        "dev_sells_seen_pre_entry": None,
                        "early_sell_pressure_ratio": None,
                        "buy_count_30s_pre_entry": None,
                        "sell_count_30s_pre_entry": None,
                        "buy_usd_30s_pre_entry": None,
                        "sell_usd_30s_pre_entry": None,
                        "holder_pct_top5": None,
                        "holder_pct_top10": None,
                    })
                    queue_state_update(last_signal={"mint": mint, "ts": signal_ts, "price_usd": round(live_price, 10), "mode": self.execution_mode, "status": "ready", "source": "dependency_retry"})
                    intent = await self._build_execution_intent(mint, ts_detected=time.time(), reference_price_usd=live_price)
                    result = await (self.live_adapter.execute(intent) if self.execution_mode == "live" else self.paper_adapter.execute(intent))
                    await self._post_execution(mint, intent, result, t0=time.perf_counter(), source="dependency_retry", decision_breakdown={"dependency_wait_ms": 0.0, "price_meta_rug_ms": 0.0})
                    continue
                if reason == "rug_check_unavailable" and attempts < 3:
                    self._queue_dependency_retry(mint, live_price, reason, attempts=attempts + 1)
                    queue_telemetry_event("DEPENDENCY_RETRY_DEFERRED", mint=mint, detail=f"attempts={attempts+1}", severity="warn")
                    continue
                queue_telemetry_event("DEPENDENCY_FALLBACK_DENIED", mint=mint, detail=f"reason={reason} attempts={attempts+1}", severity="warn")
                stv = read_state()
                write_state(total_vetoed=int(stv.get("total_vetoed", 0)) + 1, last_veto={"ts": int(time.time()), "mint": mint, "reason": reason, "type": "VETO"})
                append_veto(
                    mint,
                    reason,
                    source="dependency_retry",
                    historical_threshold_usd=self.current_liquidity_usd,
                    current_policy_threshold_usd=self.current_liquidity_usd,
                    historical_threshold_context_only=False,
                )
                append_recent_activity(action="VETO", mint=mint, live_pnl=0.0)
                await self._record_shadow_watch(
                    mint,
                    reason,
                    live_price,
                    historical_threshold_usd=self.current_liquidity_usd,
                    current_policy_threshold_usd=self.current_liquidity_usd,
                )
            await asyncio.sleep(5)

    async def _migration_retry_worker(self):
        """Retry price lookup for confirmed migrations that failed on first pass.

        When a token passes is_migration_signature (confirmed Raydium program ID in logs)
        but fails price lookup (Jupiter/DexScreener indexing lag), we requeue it here
        instead of discarding it.  Retries at 30s, 60s, 90s — by then Jupiter should
        have indexed the pool.
        """
        while True:
            try:
                item = await self._migration_retry_queue.get()
            except Exception:
                await asyncio.sleep(5)
                continue

            mint = str(item.get("mint", ""))
            attempt = int(item.get("attempt", 0))
            retry_at = float(item.get("retry_at", 0))
            is_delayed_entry = str(item.get("source", "")) == "delayed_entry"
            # Task 2: read as None (not 0) — migration_retry items never have reference_price;
            # only delayed_entry items store a real reference price captured at enqueue.
            _raw_ref = item.get("reference_price")
            reference_price: Optional[float] = float(_raw_ref) if (_raw_ref is not None and float(_raw_ref) > 0) else None
            already_vetted = bool(item.get("vetted", False))
            # Entry-score inputs — populated below as each component becomes available.
            _holder_reason: Optional[str] = None
            _score_ds_verified: bool = False  # set True if T+30 has any real pool price
            _score_is_sentinel: bool = False

            # Sleep until retry time
            wait = retry_at - time.time()
            if wait > 0:
                log.info(
                    "[ENTRY_FLOW] mint=%s source=%s attempt=%d waiting=%.1fs until_retry_at=%.0f",
                    mint, item.get("source", "migration_retry"), attempt, wait, retry_at,
                )
                await asyncio.sleep(wait)

            # Skip if already trading this mint or it was seen again
            if not mint or mint in self.trades or not MIGRATION_RETRY_ENABLED:
                log.info(
                    "[ENTRY_FLOW] mint=%s GATE=skip_pre_check reason=%s",
                    mint,
                    "no_mint" if not mint else ("already_trading" if mint in self.trades else "retry_disabled"),
                )
                self._migration_retry_queue.task_done()
                continue

            # Skip if daily budget exhausted
            min_trade_usd = LIVE_BASE_TRADE_USD if self.execution_mode == "live" else 10.0
            if self.budget < min_trade_usd:
                log.warning(
                    "[ENTRY_FLOW] mint=%s GATE=budget_exhausted budget=%.2f min_required=%.2f",
                    mint, self.budget, min_trade_usd,
                )
                self._migration_retry_queue.task_done()
                continue

            log.info(
                "[ENTRY_FLOW] mint=%s source=%s attempt=%d budget=%.2f ref_price=%s vetted=%s — entering pipeline",
                mint, item.get("source", "migration_retry"), attempt, self.budget,
                f"${reference_price:.10g}" if reference_price is not None else "none",
                already_vetted,
            )

            source_label = "DELAYED_ENTRY" if is_delayed_entry else "MIGRATION_RETRY"
            self._funnel["migration_retry_attempts_total"] = self._funnel.get("migration_retry_attempts_total", 0) + 1
            if reference_price is None and not is_delayed_entry:
                self._funnel["migration_retry_missing_ref_price_count"] = self._funnel.get("migration_retry_missing_ref_price_count", 0) + 1
            ref_price_log = f"${reference_price:.10g}" if reference_price is not None else "missing_at_enqueue"
            queue_telemetry_event(
                f"{source_label}_ATTEMPT", mint=mint,
                detail=f"attempt={attempt+1}/{len(MIGRATION_RETRY_DELAYS_SECONDS)} ref_price={ref_price_log}",
                severity="info",
            )

            # Re-attempt price lookup — each attempt is a fully fresh call (no cache reuse)
            # Migration retry always processes confirmed migration candidates.
            log.info("[ENTRY_FLOW] mint=%s GATE=price_fetch attempt=%d", mint, attempt)
            live_price = await self._entry_reference_price_with_retries(mint, confirmed_migration=True)
            # Task 8: Prevent zero/negative prices from being used
            if live_price is None or live_price <= 0:
                log.warning(
                    "[ENTRY_FLOW] mint=%s GATE=price_fetch_failed attempt=%d — no valid price, %s",
                    mint, attempt,
                    "requeueing" if attempt + 1 < len(MIGRATION_RETRY_DELAYS_SECONDS) else "exhausted",
                )
                # Still no price — requeue if more retries available
                if attempt + 1 < len(MIGRATION_RETRY_DELAYS_SECONDS):
                    next_delay = MIGRATION_RETRY_DELAYS_SECONDS[attempt + 1]
                    try:
                        # Carry source/vetted flags; reference_price omitted for migration_retry
                        # (it was never set), preserved for delayed_entry.
                        requeue_item: dict = {
                            "mint": mint,
                            "attempt": attempt + 1,
                            "retry_at": time.time() + next_delay,
                            "first_ts": item.get("first_ts", time.time()),
                        }
                        if item.get("source"):
                            requeue_item["source"] = item["source"]
                        if item.get("vetted"):
                            requeue_item["vetted"] = item["vetted"]
                        if reference_price is not None:
                            requeue_item["reference_price"] = reference_price
                        self._migration_retry_queue.put_nowait(requeue_item)
                    except asyncio.QueueFull:
                        # P3.8: silent drop here used to hide pressure spikes.
                        # Emit MIGRATION_RETRY_QUEUE_FULL so dashboards can track
                        # how often the queue is saturating (lost retries mean
                        # lost potential entries).
                        queue_telemetry_event(
                            "MIGRATION_RETRY_QUEUE_FULL", mint=mint,
                            detail=f"attempt={attempt+1} next_delay={next_delay}s path=no_price",
                            severity="warn",
                        )
                        self._funnel["migration_retry_queue_full"] = self._funnel.get("migration_retry_queue_full", 0) + 1
                    queue_telemetry_event(
                        "MIGRATION_RETRY_STILL_NO_PRICE", mint=mint,
                        detail=f"attempt={attempt+1} next_delay={next_delay}s ref_price={ref_price_log}",
                        severity="info",
                    )
                else:
                    self._funnel["migration_retry_exhausted_count"] = self._funnel.get("migration_retry_exhausted_count", 0) + 1
                    queue_telemetry_event(
                        "MIGRATION_RETRY_EXHAUSTED", mint=mint,
                        detail=f"attempts={attempt+1} — all retries failed, no_valid_price_source ref_price={ref_price_log}",
                        severity="warn",
                    )
                self._migration_retry_queue.task_done()
                continue

            # Price found! Run the remaining entry pipeline.
            # Task 3: Track which source resolved — log for observability.
            log.info("[%s] price found for %s at attempt %d: $%.10g (ref was %s)", source_label, mint, attempt + 1, live_price, ref_price_log)
            self._funnel["migration_retry_price_found"] = self._funnel.get("migration_retry_price_found", 0) + 1
            queue_telemetry_event(
                f"{source_label}_PRICE_RESOLVED", mint=mint,
                detail=f"attempt={attempt+1} live_price=${live_price:.10g} ref_price={ref_price_log}",
                severity="info",
            )

            # ── PRICE-STILL-ALIVE CHECK (delayed entry) ──────────────────────
            # Compare current price to the reference price captured at migration
            # detection. If the token has already dumped, skip it — don't buy
            # someone else's exit. reference_price is None for migration_retry — skip check.
            log.info(
                "[ENTRY_FLOW] mint=%s GATE=price_alive is_delayed=%s ref_price=%s live_price=$%.10g",
                mint, is_delayed_entry,
                f"${reference_price:.10g}" if reference_price is not None else "none",
                live_price,
            )
            if is_delayed_entry and reference_price is not None and reference_price > 0 and live_price > 0:
                drop_pct = (reference_price - live_price) / reference_price
                if drop_pct >= DELAYED_ENTRY_MAX_DROP_PCT:
                    log.warning(
                        "[DELAYED_ENTRY] DISCARDED mint=%s — price dumped %.1f%% (ref=$%.10g now=$%.10g threshold=%.0f%%)",
                        mint, drop_pct * 100, reference_price, live_price, DELAYED_ENTRY_MAX_DROP_PCT * 100,
                    )
                    queue_telemetry_event(
                        "DELAYED_ENTRY_PRICE_DUMP_DISCARD", mint=mint,
                        detail=f"ref={reference_price:.10g} now={live_price:.10g} drop={drop_pct*100:.1f}% threshold={DELAYED_ENTRY_MAX_DROP_PCT*100:.0f}%",
                        severity="info",
                    )
                    self._funnel["delayed_entry_dump_discarded"] = self._funnel.get("delayed_entry_dump_discarded", 0) + 1
                    self._migration_retry_queue.task_done()
                    continue
                else:
                    log.info(
                        "[DELAYED_ENTRY] PRICE ALIVE mint=%s — change=%.1f%% (ref=$%.10g now=$%.10g)",
                        mint, -drop_pct * 100, reference_price, live_price,
                    )
                    self._funnel["delayed_entry_price_alive"] = self._funnel.get("delayed_entry_price_alive", 0) + 1

                    # ── GRADUATION CURVE SENTINEL GUARD ──────────────────────
                    # If both live_price and ref_price equal the graduation curve
                    # constant (6.162627048e-05 ± 0.01%), the price feed has not
                    # indexed the real Raydium pool yet — change=0% is spurious.
                    # Attempt a DexScreener lookup to get the real pool price.
                    _GRAD_CURVE_CONST = 6.162627048e-05
                    _is_sentinel = (
                        reference_price is not None
                        and abs(reference_price - _GRAD_CURVE_CONST) / _GRAD_CURVE_CONST < 0.0001
                        and abs(live_price - _GRAD_CURVE_CONST) / _GRAD_CURVE_CONST < 0.0001
                    )
                    _score_is_sentinel = _is_sentinel
                    if not _is_sentinel:
                        # Jupiter returned a verified non-sentinel price → price-verified for scoring.
                        _score_ds_verified = True
                    if _is_sentinel:
                        log.warning(
                            "[DELAYED_ENTRY] SENTINEL_PRICE_DETECTED mint=%s — "
                            "ref=%.10g live=%.10g both match graduation curve; "
                            "fetching DexScreener + Jupiter v2 in parallel",
                            mint, reference_price, live_price,
                        )

                        async def _fetch_jup_px_v2(_m: str) -> Optional[float]:
                            try:
                                _s = await self._get_http_session()
                                _url = f"https://api.jup.ag/price/v3?ids={_m}"
                                async with _s.get(_url, timeout=aiohttp.ClientTimeout(total=1.5)) as _r:
                                    if _r.status != 200:
                                        return None
                                    _j = await _r.json()
                                _entry = (_j or {}).get(_m) or {}
                                _px = _entry.get("usdPrice")
                                if _px is None:
                                    return None
                                try:
                                    return float(_px)
                                except (TypeError, ValueError):
                                    return None
                            except Exception:
                                return None

                        _GRAD_CURVE_CONST_2 = 1.3e-05

                        def _px_is_sentinel(_p: Optional[float]) -> bool:
                            if _p is None or _p <= 0:
                                return True
                            for _c in (_GRAD_CURVE_CONST, _GRAD_CURVE_CONST_2):
                                if abs(_p - _c) / _c < 0.0001:
                                    return True
                            return False

                        try:
                            _ds_price, _jup_price = await asyncio.gather(
                                self._dexscreener_price(mint),
                                _fetch_jup_px_v2(mint),
                                return_exceptions=False,
                            )
                        except Exception as _e:
                            _ds_price, _jup_price = None, None
                            log.warning("[DELAYED_ENTRY] parallel price fetch failed mint=%s: %s", mint, _e)

                        # Jupiter wins if it returns a valid non-sentinel price —
                        # skip DexScreener pump-ratio check and proceed to rugcheck.
                        if _jup_price is not None and not _px_is_sentinel(_jup_price):
                            log.info(
                                "[DELAYED_ENTRY] JUPITER_PRICE_VERIFIED mint=%s price=%.10g",
                                mint, _jup_price,
                            )
                            queue_telemetry_event(
                                "DELAYED_ENTRY_JUPITER_PRICE_VERIFIED", mint=mint,
                                detail=f"jup_price={_jup_price:.10g}",
                                severity="info",
                            )
                            live_price = _jup_price
                            _score_ds_verified = True
                        elif _ds_price is not None and _ds_price > 0:
                            _pump_ratio = _ds_price / _GRAD_CURVE_CONST
                            if _pump_ratio > 2.0:
                                # Token has already pumped — entry would be chasing distribution.
                                log.warning(
                                    "[DELAYED_ENTRY] post_pump_entry_rejected mint=%s — "
                                    "DexScreener=%.10g is %.1fx graduation curve (ref=%.10g); skipping",
                                    mint, _ds_price, _pump_ratio, _GRAD_CURVE_CONST,
                                )
                                queue_telemetry_event(
                                    "DELAYED_ENTRY_POST_PUMP_REJECTED", mint=mint,
                                    detail=f"ds_price={_ds_price:.10g} ref={_GRAD_CURVE_CONST:.10g} pump_ratio={_pump_ratio:.2f}x",
                                    severity="warn",
                                )
                                self._funnel["delayed_entry_post_pump_rejected"] = self._funnel.get("delayed_entry_post_pump_rejected", 0) + 1
                                self._migration_retry_queue.task_done()
                                continue
                            else:
                                # DexScreener price is close to curve — not pumped yet, allow but log.
                                log.info(
                                    "[DELAYED_ENTRY] price_unverified_entry mint=%s — "
                                    "DexScreener=%.10g (%.1fx curve); price not pumped, proceeding",
                                    mint, _ds_price, _pump_ratio,
                                )
                                queue_telemetry_event(
                                    "DELAYED_ENTRY_PRICE_UNVERIFIED", mint=mint,
                                    detail=f"ds_price={_ds_price:.10g} pump_ratio={_pump_ratio:.2f}x — sentinel but not pumped",
                                    severity="info",
                                )
                                # Use the real DexScreener price going forward
                                live_price = _ds_price
                                _score_ds_verified = True
                        else:
                            # DexScreener returned nothing — price still unindexed.
                            # Don't proceed on an unverified graduation-curve sentinel:
                            # re-enqueue with a short delay so the pool has time to index.
                            _pu_attempt = int(item.get("price_unverified_attempt", 0))
                            _PU_MAX_RETRIES = int(os.getenv("MAX_ROUTE_RETRIES", "3"))
                            _pu_display = min(_pu_attempt + 1, _PU_MAX_RETRIES)
                            if _pu_attempt < _PU_MAX_RETRIES:
                                # Exponential backoff: 8s, 16s, 32s per attempt. Gives
                                # DexScreener more time to index without adding retries
                                # (total window ~56s vs prior flat 36s).
                                _PU_BACKOFF_SECONDS = (8.0, 16.0, 32.0)
                                _pu_delay = _PU_BACKOFF_SECONDS[min(_pu_attempt, len(_PU_BACKOFF_SECONDS) - 1)]
                                log.warning(
                                    "[DELAYED_ENTRY] DELAYED_ENTRY_PRICE_UNVERIFIED_SKIP mint=%s — "
                                    "DexScreener returned no price on graduation-curve sentinel "
                                    "(attempt %d/%d); re-enqueueing for pool-index retry in %.0fs",
                                    mint, _pu_display, _PU_MAX_RETRIES, _pu_delay,
                                )
                                try:
                                    _pu_requeue: dict = {
                                        "mint": mint,
                                        "attempt": 0,
                                        "retry_at": time.time() + _pu_delay,
                                        "first_ts": item.get("first_ts", time.time()),
                                        "source": "delayed_entry",
                                        "vetted": True,
                                        "price_unverified_attempt": _pu_attempt + 1,
                                    }
                                    if reference_price is not None:
                                        _pu_requeue["reference_price"] = reference_price
                                    if item.get("route_attempt"):
                                        _pu_requeue["route_attempt"] = item["route_attempt"]
                                    self._migration_retry_queue.put_nowait(_pu_requeue)
                                    queue_telemetry_event(
                                        "DELAYED_ENTRY_PRICE_UNVERIFIED_SKIP", mint=mint,
                                        detail=f"attempt={_pu_display}/{_PU_MAX_RETRIES} dexscreener_no_price — re-enqueued for {_pu_delay:.0f}s",
                                        severity="warn",
                                    )
                                except asyncio.QueueFull:
                                    queue_telemetry_event(
                                        "DELAYED_ENTRY_PRICE_UNVERIFIED_QUEUE_FULL", mint=mint,
                                        detail="dexscreener_no_price — requeue failed", severity="warn",
                                    )
                            else:
                                # Retries exhausted — DexScreener never indexed. Final
                                # fallback: query Jupiter price v3 directly. Jupiter
                                # indexes Raydium pools ~5-15s after graduation, often
                                # well before DexScreener. PRE_ENTRY_SURGE_RATIO_MAX
                                # in the delayed_entry consumer catches bad fills.
                                _jup_final_px: Optional[float] = None
                                try:
                                    _jf_s = await self._get_http_session()
                                    _jf_url = f"https://api.jup.ag/price/v3?ids={mint}"
                                    async with _jf_s.get(
                                        _jf_url,
                                        timeout=aiohttp.ClientTimeout(total=2.5),
                                    ) as _jf_r:
                                        if _jf_r.status == 200:
                                            _jf_j = await _jf_r.json()
                                            _jf_entry = (_jf_j or {}).get(mint) or {}
                                            _jf_raw = _jf_entry.get("usdPrice")
                                            if _jf_raw is not None:
                                                try:
                                                    _jf_val = float(_jf_raw)
                                                    if _jf_val > 0 and not _px_is_sentinel(_jf_val):
                                                        _jup_final_px = _jf_val
                                                except (TypeError, ValueError):
                                                    pass
                                except Exception as _jf_exc:
                                    log.warning("[DELAYED_ENTRY] jupiter_final_fallback_error mint=%s: %s", mint, _jf_exc)

                                if _jup_final_px is not None:
                                    log.warning(
                                        "[DELAYED_ENTRY] JUPITER_PRICE_VERIFIED mint=%s price=%.10g "
                                        "(final fallback after %d DexScreener attempts)",
                                        mint, _jup_final_px, _PU_MAX_RETRIES,
                                    )
                                    queue_telemetry_event(
                                        "DELAYED_ENTRY_JUPITER_PRICE_VERIFIED_FALLBACK", mint=mint,
                                        detail=f"jup_price={_jup_final_px:.10g} ds_attempts={_PU_MAX_RETRIES}",
                                        severity="info",
                                    )
                                    live_price = _jup_final_px
                                    _score_ds_verified = True
                                else:
                                    # DexScreener + Jupiter both refused — token genuinely
                                    # has no routable market. Discard.
                                    log.warning(
                                        "[DELAYED_ENTRY] DELAYED_ENTRY_PRICE_UNVERIFIED_EXHAUSTED mint=%s — "
                                        "DexScreener no price after %d attempts AND Jupiter fallback empty; discarding",
                                        mint, _PU_MAX_RETRIES,
                                    )
                                    self._funnel["delayed_entry_price_unverified_exhausted"] = self._funnel.get("delayed_entry_price_unverified_exhausted", 0) + 1
                                    queue_telemetry_event(
                                        "DELAYED_ENTRY_PRICE_UNVERIFIED_EXHAUSTED", mint=mint,
                                        detail=f"attempts={_PU_MAX_RETRIES} jupiter_fallback_empty all_sources_failed",
                                        severity="warn",
                                    )
                                    self._migration_scores.pop(mint, None)
                                    self._migration_retry_queue.task_done()
                                    continue
                            # On requeue paths (non-exhausted), continue to end of iteration.
                            if _pu_attempt < _PU_MAX_RETRIES:
                                self._migration_retry_queue.task_done()
                                continue
                    # ── END GRADUATION CURVE SENTINEL GUARD ──────────────────
            # ── END PRICE-STILL-ALIVE CHECK ──────────────────────────────────

            # Rugcheck — skip if already vetted (delayed_entry passed all checks pre-queue)
            log.info("[ENTRY_FLOW] mint=%s GATE=rugcheck already_vetted=%s", mint, already_vetted)
            if already_vetted:
                log.info("[%s] mint=%s already vetted, skipping rugcheck/mutable", source_label, mint)
            else:
                onchain_task = asyncio.create_task(self._onchain_fast_veto(mint))
                try:
                    ok, reason = await asyncio.wait_for(self.rug_check(mint), timeout=DEPENDENCY_PRIMARY_BUDGET_SECONDS)
                except asyncio.TimeoutError:
                    ok, reason = False, "rug_check_unavailable"
                if not ok:
                    if reason == "rug_check_unavailable":
                        try:
                            onchain_veto, onchain_reason = await asyncio.wait_for(onchain_task, timeout=ONCHAIN_VETO_TIMEOUT_SECONDS)
                        except (asyncio.TimeoutError, Exception):
                            onchain_veto, onchain_reason = True, "onchain_timeout"
                        if not onchain_veto:
                            ok, reason = True, f"onchain_fast_pass:{onchain_reason}"
                    if not ok:
                        log.warning("[EXECUTION_GATE] mint=%s reason=vetoed stage=rugcheck veto_reason=%s", mint, reason)
                        if not onchain_task.done():
                            onchain_task.cancel()
                        queue_telemetry_event("MIGRATION_RETRY_VETO", mint=mint, detail=f"reason={reason}", severity="info")
                        self._migration_retry_queue.task_done()
                        continue
                if not onchain_task.done():
                    onchain_task.cancel()

                # Mutable check
                veto_mutable, mutable_reason = await self.helius_mutable_veto(mint)
                if veto_mutable:
                    log.warning("[EXECUTION_GATE] mint=%s reason=vetoed stage=mutable_check veto_reason=%s", mint, mutable_reason)
                    queue_telemetry_event("MIGRATION_RETRY_VETO", mint=mint, detail=f"reason={mutable_reason}", severity="info")
                    self._migration_retry_queue.task_done()
                    continue

            # Holder concentration check — runs regardless of already_vetted
            # because holder distribution can change between initial scan (T=0)
            # and delayed entry (T+30s).
            if HOLDER_CHECK_ENABLED:
                try:
                    _holder_veto, _holder_reason = await asyncio.wait_for(
                        self._holder_concentration_veto(mint),
                        timeout=HOLDER_CHECK_TIMEOUT_SECONDS,
                    )
                    if _holder_veto:
                        log.warning(
                            "[DELAYED_ENTRY_HOLDER_VETO] mint=%s reason=%s — skipping",
                            mint, _holder_reason,
                        )
                        queue_telemetry_event(
                            "DELAYED_ENTRY_HOLDER_VETO", mint=mint,
                            detail=_holder_reason, severity="warn",
                        )
                        self._migration_scores.pop(mint, None)
                        self._migration_retry_queue.task_done()
                        continue
                    else:
                        log.info(
                            "[DELAYED_ENTRY_HOLDER_OK] mint=%s reason=%s",
                            mint, _holder_reason,
                        )
                except asyncio.TimeoutError:
                    log.warning(
                        "[DELAYED_ENTRY_HOLDER_TIMEOUT] mint=%s — check timed out, proceeding",
                        mint,
                    )
                    _holder_reason = "holder_check_timeout"
                except Exception as _hc_exc:
                    log.warning(
                        "[DELAYED_ENTRY_HOLDER_ERROR] mint=%s — %s, proceeding",
                        mint, _hc_exc,
                    )
                    _holder_reason = f"holder_check_error:{_hc_exc.__class__.__name__}"

            # ── ENTRY SCORE GATE ──────────────────────────────────────────────
            # Consume the score task kicked off at T=0. Fetch is bounded by
            # ENTRY_SCORE_AWAIT_TIMEOUT_SECONDS (0.5s default) so it adds at most
            # 500ms if something went wrong with the pre-fetch; in the common path
            # the task has completed long before this point and awaiting is 0ms.
            entry_score_dict: Optional[dict] = None
            if ENTRY_SCORE_ENABLED and is_delayed_entry:
                _score_task = self._migration_scores.get(mint)
                _partial: Optional[dict] = None
                if _score_task is not None:
                    try:
                        _partial = await asyncio.wait_for(
                            asyncio.shield(_score_task),
                            timeout=ENTRY_SCORE_AWAIT_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        log.info(
                            "[ENTRY_SCORE] mint=%s — pre-fetch still running at T+30; "
                            "proceeding unscored",
                            mint,
                        )
                    except Exception as _se:
                        log.warning(
                            "[ENTRY_SCORE] mint=%s — score fetch exception: %s; proceeding unscored",
                            mint, _se,
                        )
                if _partial is not None:
                    # Real delay between first detection (enqueue) and this point
                    # (T+30 for delayed_entry). _finalize_entry_score uses it to
                    # score the graduation component — _score_migration_candidate
                    # cannot compute it because it runs at T=0.
                    _delay_s = max(0.0, time.time() - float(item.get("first_ts", time.time())))
                    entry_score_dict = self._finalize_entry_score(
                        _partial,
                        dex_price_verified=_score_ds_verified,
                        holder_reason=_holder_reason,
                        delay_s=_delay_s,
                    )
                    entry_score_dict["threshold"] = ENTRY_SCORE_THRESHOLD
                    entry_score_dict["is_sentinel"] = _score_is_sentinel
                    _total = int(entry_score_dict.get("total") or 0)
                    _breakdown_str = (
                        f"total={_total}/{ENTRY_SCORE_THRESHOLD} "
                        f"graduation={entry_score_dict.get('time_to_graduate_pts')} "
                        f"dex={entry_score_dict.get('dexscreener_pts')} "
                        f"holder={entry_score_dict.get('holder_pts')} "
                        f"social={entry_score_dict.get('social_pts')} "
                        f"age_s={entry_score_dict.get('time_since_detection_s')} "
                        f"fetch_err={entry_score_dict.get('fetch_error')}"
                    )
                    # Sentinel + fresh-detection veto: when the entry is priced off the
                    # graduation-curve sentinel (no real DEX price yet) AND the signal is
                    # fresh (<30s), the pool is almost certainly unquotable — historical
                    # UNSET/QUOTEABILITY_GUARD cohort shows mfe=0, mae=0 in this regime.
                    _age_s = entry_score_dict.get("time_since_detection_s")
                    try:
                        _age_f = float(_age_s) if _age_s is not None else None
                    except (TypeError, ValueError):
                        _age_f = None
                    if _score_is_sentinel and _age_f is not None and _age_f < 30.0:
                        log.warning(
                            "[ENTRY_SCORE_VETO_SENTINEL_FRESH] mint=%s is_sentinel=True age_s=%.1f %s",
                            mint, _age_f, _breakdown_str,
                        )
                        queue_telemetry_event(
                            "ENTRY_SCORE_VETO_SENTINEL_FRESH", mint=mint,
                            detail=f"age_s={_age_f:.1f} {_breakdown_str}",
                            severity="warn",
                        )
                        self._funnel["entry_score_vetoed_sentinel_fresh"] = self._funnel.get("entry_score_vetoed_sentinel_fresh", 0) + 1
                        self._migration_scores.pop(mint, None)
                        self._migration_retry_queue.task_done()
                        continue
                    if _total < ENTRY_SCORE_THRESHOLD:
                        log.warning(
                            "[ENTRY_SCORE_VETO] mint=%s %s",
                            mint, _breakdown_str,
                        )
                        queue_telemetry_event(
                            "ENTRY_SCORE_VETO", mint=mint,
                            detail=_breakdown_str, severity="warn",
                        )
                        self._funnel["entry_score_vetoed"] = self._funnel.get("entry_score_vetoed", 0) + 1
                        self._migration_scores.pop(mint, None)
                        self._migration_retry_queue.task_done()
                        continue
                    else:
                        log.info(
                            "[ENTRY_SCORE_PASS] mint=%s %s",
                            mint, _breakdown_str,
                        )
                        queue_telemetry_event(
                            "ENTRY_SCORE_PASS", mint=mint,
                            detail=_breakdown_str, severity="info",
                        )
                        self._funnel["entry_score_passed"] = self._funnel.get("entry_score_passed", 0) + 1
            # ── END ENTRY SCORE GATE ──────────────────────────────────────────

            # ── PRE-ENTRY SETTLE WINDOW ─────────────────────────────────────
            # Final gate: re-sample price after a short wait. If it dropped
            # beyond SETTLE_MAX_DROP_PCT we're catching an active dump — reject.
            # Paired with drift_guard below (drift_guard is a one-shot refresh;
            # settle_window is an explicit wait-then-resample). Rationale in
            # SETTLE_WINDOW_* block near top of file.
            if SETTLE_WINDOW_ENABLED and is_delayed_entry and live_price > 0:
                _sw_px_a = live_price
                _sw_t0 = time.perf_counter()
                try:
                    await asyncio.sleep(SETTLE_WINDOW_SECONDS)
                    _sw_px_b = await self._token_price(mint)
                except asyncio.CancelledError:
                    raise
                except Exception as _sw_exc:
                    log.warning("[SETTLE_WINDOW] mint=%s fetch_error=%s — proceeding (fail-open)", mint, _sw_exc)
                    _sw_px_b = None
                _sw_ms = round((time.perf_counter() - _sw_t0) * 1000, 1)
                if _sw_px_b is not None and _sw_px_b > 0:
                    _sw_drop = (_sw_px_a - _sw_px_b) / _sw_px_a
                    if _sw_drop >= SETTLE_MAX_DROP_PCT:
                        log.warning(
                            "[SETTLE_WINDOW] mint=%s REJECT drop=%.2f%% threshold=%.2f%% "
                            "px_a=%.10g px_b=%.10g window_ms=%.0f",
                            mint, _sw_drop * 100, SETTLE_MAX_DROP_PCT * 100,
                            _sw_px_a, _sw_px_b, _sw_ms,
                        )
                        queue_telemetry_event(
                            "SETTLE_WINDOW_REJECT", mint=mint,
                            detail=f"drop_pct={_sw_drop*100:.2f} threshold_pct={SETTLE_MAX_DROP_PCT*100:.2f} "
                                   f"px_a={_sw_px_a:.10g} px_b={_sw_px_b:.10g} window_ms={_sw_ms}",
                            severity="info",
                        )
                        self._funnel["settle_window_rejected"] = self._funnel.get("settle_window_rejected", 0) + 1
                        self._migration_retry_queue.task_done()
                        continue
                    log.info(
                        "[SETTLE_WINDOW] mint=%s PASS drop=%.2f%% px_a=%.10g px_b=%.10g window_ms=%.0f",
                        mint, _sw_drop * 100, _sw_px_a, _sw_px_b, _sw_ms,
                    )
                    # Use fresher sample as reference going forward.
                    live_price = _sw_px_b
                else:
                    log.info(
                        "[SETTLE_WINDOW] mint=%s NO_FRESH_PRICE window_ms=%.0f — proceeding (fail-open)",
                        mint, _sw_ms,
                    )
            # ── END PRE-ENTRY SETTLE WINDOW ─────────────────────────────────

            # Entry drift guard
            log.info("[ENTRY_FLOW] mint=%s GATE=drift_guard drift_enabled=%s live_price=$%.10g", mint, ENTRY_DRIFT_ENABLED, live_price)
            intent = await self._build_execution_intent(mint, ts_detected=time.time(), reference_price_usd=live_price)
            if ENTRY_DRIFT_ENABLED and live_price > 0:
                fresh_px = await self._token_price(mint)
                if fresh_px is not None and fresh_px > 0:
                    drift_pct = (live_price - fresh_px) / live_price
                    if drift_pct >= ENTRY_DRIFT_MAX_DROP_PCT:
                        log.warning(
                            "[EXECUTION_GATE] mint=%s reason=blocked stage=drift_guard drift_pct=%.1f%% threshold=%.1f%%",
                            mint, drift_pct * 100, ENTRY_DRIFT_MAX_DROP_PCT * 100,
                        )
                        queue_telemetry_event("MIGRATION_RETRY_DRIFT_REJECTED", mint=mint, detail=f"drift={drift_pct*100:.1f}%", severity="warn")
                        self._migration_retry_queue.task_done()
                        continue
                    intent.reference_price_usd = fresh_px
                    intent.reference_price_is_real = True  # real DEX price, not graduation curve

            # ── PRE-ENTRY SWAP ROUTE CHECK (delayed_entry path) ─────────────────
            # Verify Jupiter can route a sell BEFORE buying. New Raydium pools take
            # 30-90s to be indexed by Jupiter. If the route isn't ready at T+30s,
            # retry at 15s intervals (T+45s, T+60s, T+75s) before discarding.
            if ENTRY_SWAP_ROUTE_CHECK_ENABLED and is_delayed_entry:
                _t_route = time.perf_counter()
                route_ok = await self._pre_entry_swap_route_check(mint, intent.reference_price_usd)
                _route_ms = round((time.perf_counter() - _t_route) * 1000, 1)
                if not route_ok:
                    _route_attempt = int(item.get("route_attempt", 0))
                    _MAX_ROUTE_RETRIES = int(os.getenv("MAX_ROUTE_RETRIES", "3"))  # T+30s + 3×15s = T+75s max
                    # Clamp display to max so log never shows "attempt 4/3" on the exhausted path
                    _display_attempt = min(_route_attempt + 1, _MAX_ROUTE_RETRIES)
                    log.warning(
                        "[ROUTE_RETRY] mint=%s pool not routable (attempt %d/%d, route_ms=%.0f) — %s",
                        mint, _display_attempt, _MAX_ROUTE_RETRIES,
                        _route_ms, "requeueing" if _route_attempt < _MAX_ROUTE_RETRIES else "exhausted",
                    )
                    if _route_attempt < _MAX_ROUTE_RETRIES:
                        try:
                            # P3.7: exponential backoff so a token whose pool genuinely
                            # isn't indexing yet isn't hammered every 15s. Gives Raydium
                            # + Jupiter time to propagate between checks while the total
                            # wait window (15+30+60 = 105s) is still bounded.
                            _RT_BACKOFF_SECONDS = (15.0, 30.0, 60.0)
                            _rt_delay = _RT_BACKOFF_SECONDS[min(_route_attempt, len(_RT_BACKOFF_SECONDS) - 1)]
                            _requeue: dict = {
                                "mint": mint, "attempt": 0,
                                "retry_at": time.time() + _rt_delay,
                                "first_ts": item.get("first_ts", time.time()),
                                "source": "delayed_entry",
                                "vetted": True,
                                "route_attempt": _route_attempt + 1,
                            }
                            if reference_price is not None:
                                _requeue["reference_price"] = reference_price
                            self._migration_retry_queue.put_nowait(_requeue)
                            queue_telemetry_event(
                                "ROUTE_RETRY_ENQUEUED", mint=mint,
                                detail=f"attempt={_display_attempt}/{_MAX_ROUTE_RETRIES} route_ms={_route_ms} delay_s={_rt_delay:.0f}",
                                severity="info",
                            )
                        except asyncio.QueueFull:
                            queue_telemetry_event("ROUTE_RETRY_QUEUE_FULL", mint=mint, severity="warn")
                    else:
                        self._funnel["entry_no_swap_route"] = self._funnel.get("entry_no_swap_route", 0) + 1
                        queue_telemetry_event(
                            "ROUTE_RETRY_EXHAUSTED", mint=mint,
                            detail=f"route_ms={_route_ms} all_retries_failed",
                            severity="warn",
                        )
                    self._migration_retry_queue.task_done()
                    continue
                log.info("[ROUTE_RETRY] mint=%s route OK (route_ms=%.0f) — proceeding to execution", mint, _route_ms)
            # ── END PRE-ENTRY SWAP ROUTE CHECK ──────────────────────────────────

            # ── LIQUIDITY DEPTH CHECK ─────────────────────────────────────────
            # Verify the Raydium pool has enough liquidity to absorb an entry
            # without catastrophic price impact.
            # P3.3: threshold is MIN_LIQUIDITY_USD (canonical) rather than the
            # prior hardcoded 5000.0 — having two liquidity floors meant signals
            # that pass the entry gate at 25k could still reach here and clear a
            # 5k bar, producing positions on shallow pools.
            # Source: DexScreener (Raydium-preferred pair by liquidity, same
            # as the mark price path). Missing data = proceed (don't block).
            # Retries: up to 2 requeues with 15s delay before giving up.
            _liq_attempt = int(item.get("liq_attempt", 0))
            _MAX_LIQ_RETRIES = 2
            _LIQ_THRESHOLD_USD = float(MIN_LIQUIDITY_USD)
            _raydium_liq_usd: Optional[float] = None
            _entry_pair: Optional[dict] = None  # telemetry: cache selected pair for signal_accept
            try:
                _liq_s = await self._get_http_session()
                async with _liq_s.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                    timeout=2.0,
                ) as _liq_r:
                    if _liq_r.status == 200:
                        _liq_d = await _liq_r.json()
                        _liq_pairs = (_liq_d.get("pairs") or [])
                        # Best Raydium pair by liquidity (mirrors _dexscreener_price logic)
                        _liq_pairs.sort(
                            key=lambda p: float(((p.get("liquidity") or {}).get("usd") or 0)),
                            reverse=True,
                        )
                        for _lp in _liq_pairs:
                            if "raydium" in str(_lp.get("dexId") or "").lower():
                                _raydium_liq_usd = float((((_lp.get("liquidity") or {}).get("usd") or 0)))
                                _entry_pair = _lp  # telemetry only
                                break
            except Exception as _liq_exc:
                log.warning("[LIQ_CHECK] mint=%s DexScreener error=%s — proceeding anyway", mint, _liq_exc)

            if _raydium_liq_usd is not None and _raydium_liq_usd < _LIQ_THRESHOLD_USD:
                log.warning(
                    "[LIQ_CHECK] mint=%s raydium_liq_usd=%.0f < threshold=%.0f — %s",
                    mint, _raydium_liq_usd, _LIQ_THRESHOLD_USD,
                    "requeueing" if _liq_attempt < _MAX_LIQ_RETRIES else "exhausted",
                )
                queue_telemetry_event(
                    "ENTRY_LIQ_DEPTH_FAIL", mint=mint,
                    detail=f"liq_usd={_raydium_liq_usd:.0f} threshold={_LIQ_THRESHOLD_USD:.0f} liq_attempt={_liq_attempt+1}",
                    severity="warn",
                )
                if _liq_attempt < _MAX_LIQ_RETRIES:
                    try:
                        _liq_requeue: dict = {
                            "mint": mint,
                            "attempt": attempt,
                            "retry_at": time.time() + 15.0,
                            "first_ts": item.get("first_ts", time.time()),
                            "liq_attempt": _liq_attempt + 1,
                        }
                        if item.get("source"):
                            _liq_requeue["source"] = item["source"]
                        if item.get("vetted"):
                            _liq_requeue["vetted"] = item["vetted"]
                        if reference_price is not None:
                            _liq_requeue["reference_price"] = reference_price
                        if item.get("route_attempt") is not None:
                            _liq_requeue["route_attempt"] = item["route_attempt"]
                        self._migration_retry_queue.put_nowait(_liq_requeue)
                        queue_telemetry_event(
                            "ENTRY_LIQ_REQUEUED", mint=mint,
                            detail=f"liq_usd={_raydium_liq_usd:.0f} liq_attempt={_liq_attempt+1}/{_MAX_LIQ_RETRIES} recheck_in=15s",
                            severity="info",
                        )
                    except asyncio.QueueFull:
                        queue_telemetry_event("ENTRY_LIQ_REQUEUE_FULL", mint=mint, severity="warn")
                else:
                    self._funnel["entry_liq_depth_rejected"] = self._funnel.get("entry_liq_depth_rejected", 0) + 1
                    queue_telemetry_event(
                        "ENTRY_LIQ_EXHAUSTED", mint=mint,
                        detail=f"liq_usd={_raydium_liq_usd:.0f} — liq retries exhausted, not entering",
                        severity="warn",
                    )
                self._migration_retry_queue.task_done()
                continue
            else:
                _liq_log = f"{_raydium_liq_usd:.0f}" if _raydium_liq_usd is not None else "no_data_proceed"
                log.info("[LIQ_CHECK] mint=%s raydium_liq_usd=%s — OK, proceeding to execution", mint, _liq_log)
                if _raydium_liq_usd is not None:
                    queue_telemetry_event(
                        "ENTRY_LIQ_DEPTH_PASS", mint=mint,
                        detail=f"liq_usd={_raydium_liq_usd:.0f} threshold={_LIQ_THRESHOLD_USD:.0f}",
                        severity="info",
                    )
            # ── END LIQUIDITY DEPTH CHECK ─────────────────────────────────────

            # ── PRE-ENTRY FILL PRICE SANITY CHECK ────────────────────────────
            # Before submitting the swap, do a read-only Jupiter quote to verify
            # the actual fill price is within 100% of reference_price_usd
            # (configurable via PRE_ENTRY_SURGE_RATIO_MAX).
            # This catches post-pump price surges and rug dumps that slip through
            # the drift guard (which uses DEX/RPC prices, not Jupiter routing).
            # If the quote is within range, we cache it on intent so live_adapter
            # can reuse it instead of double-quoting.
            # Telemetry-only: pre-declare so signal_accept can read them safely
            # regardless of which inner branch ran. No effect on execution logic.
            _pre_fill_px: Optional[float] = None
            _pre_ref_px: float = 0.0
            _pre_ratio: Optional[float] = None
            if self.execution_mode == "live":
                try:
                    from live_canary import get_jupiter_quote, WSOL_MINT as _PRE_WSOL
                    _pre_sol_usd = await self._sol_usd()
                    _pre_ref_px = float(intent.reference_price_usd or 0.0)
                    # Sentinel detection: graduation curve constants produce a
                    # meaningless ratio — skip the sanity check entirely.
                    _PRE_SENTINEL_PRICES = (6.162627048e-05, 1.3e-05)
                    _PRE_SENTINEL_TOL = 1e-4  # 0.01% relative
                    if not intent.reference_price_is_real or any(
                        abs(_pre_ref_px - sp) / sp <= _PRE_SENTINEL_TOL
                        for sp in _PRE_SENTINEL_PRICES
                        if sp > 0 and _pre_ref_px > 0
                    ):
                        if _pre_ref_px > 0:
                            log.info("[PRE_ENTRY_PRICE_CHECK] mint=%s ref_px=%.6g — sentinel_skipped (not real price)", mint, _pre_ref_px)
                        _pre_ref_px = 0.0
                    _pre_lamports = int(intent.max_cost_lamports)
                    # D2: dynamic token decimals — pump.fun mints are always 6,
                    # others may differ; read from quote response if available.
                    _PRE_TOKEN_DECIMALS = 6  # default; updated from quote below
                    _pre_quote = None
                    if _pre_ref_px > 0 and _pre_lamports > 0 and _pre_sol_usd > 0:
                        _pre_quote = await get_jupiter_quote(
                            input_mint=_PRE_WSOL,
                            output_mint=mint,
                            amount=_pre_lamports,
                            slippage_bps=int(intent.max_slippage_pct * 10_000),
                            priority=True,
                            skip_throttle=True,
                        )
                    if _pre_quote is not None:
                        # D2: resolve token decimals from quote metadata or mint suffix
                        if not mint.lower().endswith("pump"):
                            # Try to read decimals from Jupiter quote response
                            _quote_decimals = None
                            try:
                                # Jupiter v1 returns routePlan[].swapInfo.outputMint with decimals on token info
                                for _rp in (_pre_quote.get("routePlan") or []):
                                    _si = _rp.get("swapInfo") or {}
                                    if _si.get("outputMint") == mint:
                                        _quote_decimals = _si.get("outputMintDecimals")
                                        break
                                # Also check top-level field (some Jupiter versions)
                                if _quote_decimals is None:
                                    _quote_decimals = _pre_quote.get("outputMintDecimals")
                            except Exception:
                                pass
                            if _quote_decimals is not None and int(_quote_decimals) != 6:
                                _PRE_TOKEN_DECIMALS = int(_quote_decimals)
                                log.info("[PRE_ENTRY_DECIMALS] mint=%s decimals=%d (non-standard)", mint, _PRE_TOKEN_DECIMALS)
                        _pre_out = int(_pre_quote.get("outAmount", 0))
                        if _pre_out > 0:
                            _pre_fill_px = (_pre_lamports / 1e9 * _pre_sol_usd) / (_pre_out / 10 ** _PRE_TOKEN_DECIMALS)
                            _pre_ratio = _pre_fill_px / _pre_ref_px if _pre_ref_px > 0 else 1.0
                            if _pre_ratio > PRE_ENTRY_SURGE_RATIO_MAX:
                                log.warning(
                                    "[PRE_ENTRY_PRICE_SURGE] mint=%s fill_px=%.10g ref_px=%.10g ratio=%.2fx — rejecting",
                                    mint, _pre_fill_px, _pre_ref_px, _pre_ratio,
                                )
                                queue_telemetry_event(
                                    "PRE_ENTRY_PRICE_SURGE", mint=mint,
                                    detail=f"fill_px={_pre_fill_px:.6g} ref_px={_pre_ref_px:.6g} ratio={_pre_ratio:.2f}x",
                                    severity="warn",
                                )
                                self._migration_retry_queue.task_done()
                                continue
                            elif _pre_ratio < 0.4:
                                log.warning(
                                    "[PRE_ENTRY_PRICE_DUMP] mint=%s fill_px=%.10g ref_px=%.10g ratio=%.2fx — rejecting",
                                    mint, _pre_fill_px, _pre_ref_px, _pre_ratio,
                                )
                                queue_telemetry_event(
                                    "PRE_ENTRY_PRICE_DUMP", mint=mint,
                                    detail=f"fill_px={_pre_fill_px:.6g} ref_px={_pre_ref_px:.6g} ratio={_pre_ratio:.2f}x",
                                    severity="warn",
                                )
                                self._migration_retry_queue.task_done()
                                continue
                            else:
                                log.info(
                                    "[PRE_ENTRY_PRICE_OK] mint=%s fill_px=%.10g ref_px=%.10g ratio=%.2fx — proceeding",
                                    mint, _pre_fill_px, _pre_ref_px, _pre_ratio,
                                )
                                # Cache quote on intent so live_adapter can reuse it (avoids double-quote)
                                intent.cached_quote = _pre_quote
                        else:
                            log.warning("[PRE_ENTRY_PRICE_CHECK] mint=%s outAmount=0 — quote returned empty, proceeding without sanity check", mint)
                    else:
                        log.info("[PRE_ENTRY_PRICE_CHECK] mint=%s — quote unavailable (ref_px=%.6g), proceeding without sanity check", mint, _pre_ref_px)
                except Exception as _pre_exc:
                    log.warning("[PRE_ENTRY_PRICE_CHECK] mint=%s — exception during pre-entry quote: %s; proceeding", mint, _pre_exc)
            # ── END PRE-ENTRY FILL PRICE SANITY CHECK ────────────────────────

            # Execute — keep funnel AND audit in sync (both paths must increment both)
            log.info(
                "[ENTRY_FLOW] mint=%s GATE=pre_submit ALL_PASSED — submitting execution mode=%s",
                mint, self.execution_mode,
            )
            self._funnel["execution_attempted"] += 1
            self._exec_audit["execution_attempts"] += 1
            if self.execution_mode == "live":
                self._exec_audit["live_submit_attempts"] += 1
            entry_source = "delayed_entry" if is_delayed_entry else "migration_retry"
            signal_ts = int(time.time())
            delay_actual_s = round(time.time() - float(item.get("first_ts", time.time())), 1)

            # ── TELEMETRY-ONLY PRE-ENTRY FEATURE HARVEST ──────────────────
            # Pull already-fetched DexScreener pair fields + pre-entry quote
            # results into the signal_accept event so the research pipeline
            # can correlate rug vs. winner on real pre-entry state.
            # NO new HTTP calls, NO logic change — pure reads of in-scope locals.
            _ep = _entry_pair or {}
            _ep_liq = (_ep.get("liquidity") or {}) if isinstance(_ep, dict) else {}
            _ep_vol = (_ep.get("volume") or {}) if isinstance(_ep, dict) else {}
            _ep_chg = (_ep.get("priceChange") or {}) if isinstance(_ep, dict) else {}
            _ep_txn = (_ep.get("txns") or {}) if isinstance(_ep, dict) else {}
            _ep_txn_m5 = (_ep_txn.get("m5") or {})
            _ep_txn_h1 = (_ep_txn.get("h1") or {})
            _ep_info = (_ep.get("info") or {}) if isinstance(_ep, dict) else {}
            _tel_price_change_m5 = _ep_chg.get("m5")
            _tel_volume_m5 = _ep_vol.get("m5")
            _tel_buys_m5 = _ep_txn_m5.get("buys")
            _tel_sells_m5 = _ep_txn_m5.get("sells")
            # Deterministic structure label from m5 price change (simple heuristic).
            if _tel_price_change_m5 is None:
                _tel_structure = "unknown"
            elif _tel_price_change_m5 > 15:
                _tel_structure = "breakout"
            elif _tel_price_change_m5 < -10:
                _tel_structure = "rebound"
            elif -10 <= _tel_price_change_m5 <= 0:
                _tel_structure = "pullback"
            else:
                _tel_structure = "flat"
            # Quote-to-reference slippage (if pre-entry quote ran).
            _tel_quote_slip_pct: Optional[float] = None
            if _pre_fill_px is not None and _pre_ref_px and _pre_ref_px > 0:
                try:
                    _tel_quote_slip_pct = round(((_pre_fill_px / _pre_ref_px) - 1.0) * 100.0, 4)
                except Exception:
                    _tel_quote_slip_pct = None
            # At this point the swap-route check + liquidity check + price check
            # have all passed — so route is provably available at entry time.
            _tel_route_available_at_entry = True

            _signal_accept_payload: dict = {
                "type": "signal_accept", "ts": signal_ts, "mint": mint,
                "price_usd": round(live_price, 10), "mode": self.execution_mode,
                "source": entry_source, "delay_s": delay_actual_s,
                "reference_price": reference_price,
                # --- telemetry: market / token state at entry ---
                "liquidity_usd": _raydium_liq_usd,
                "market_cap_usd": _ep.get("marketCap") if isinstance(_ep, dict) else None,
                "fdv_usd": _ep.get("fdv") if isinstance(_ep, dict) else None,
                "price_usd_at_signal": round(live_price, 10),
                "price_usd_at_entry_quote": round(_pre_fill_px, 10) if _pre_fill_px is not None else None,
                "price_change_m5_pct": _tel_price_change_m5,
                "price_change_h1_pct": _ep_chg.get("h1"),
                "volume_m5_usd": _tel_volume_m5,
                "volume_h1_usd": _ep_vol.get("h1"),
                "buy_count_m5": _tel_buys_m5,
                "sell_count_m5": _tel_sells_m5,
                "buy_count_h1": _ep_txn_h1.get("buys"),
                "sell_count_h1": _ep_txn_h1.get("sells"),
                "pair_created_at_ms": _ep.get("pairCreatedAt") if isinstance(_ep, dict) else None,
                # Fields not tracked by runtime — explicit null for schema clarity.
                "price_change_10s_pct_pre_entry": None,
                "price_change_30s_pct_pre_entry": None,
                "volume_10s_pre_entry": None,
                "volume_30s_pre_entry": None,
                "volatility_10s_pre_entry": None,
                "volatility_30s_pre_entry": None,
                "dev_holder_pct": None,
                "sniper_holder_pct": None,
                "unique_holders_count": None,
                "dev_sells_seen_pre_entry": None,
                "early_sell_pressure_ratio": None,
                "buy_count_30s_pre_entry": None,
                "sell_count_30s_pre_entry": None,
                "buy_usd_30s_pre_entry": None,
                "sell_usd_30s_pre_entry": None,
                "holder_pct_top10": None,
                # --- execution quality ---
                "quote_to_fill_slippage_pct": _tel_quote_slip_pct,
                "route_available_at_signal": True,
                "route_available_at_entry": _tel_route_available_at_entry,
                "reference_price_is_real": bool(getattr(intent, "reference_price_is_real", False)),
                "entry_price_source": "jupiter_quote" if _pre_fill_px is not None else "reference",
                # --- structure heuristic ---
                "pre_entry_structure": _tel_structure,
                "local_pullback_depth_pct": _tel_price_change_m5 if (_tel_price_change_m5 is not None and _tel_price_change_m5 < 0) else None,
                "local_spike_height_pct": _tel_price_change_m5 if (_tel_price_change_m5 is not None and _tel_price_change_m5 > 0) else None,
            }
            if entry_score_dict is not None:
                _signal_accept_payload["entry_score"] = entry_score_dict
                # Expose holder_pct_top5 at top level for easier extraction.
                _signal_accept_payload["holder_pct_top5"] = entry_score_dict.get("holder_pct_top5")
            append_history(_signal_accept_payload)
            # Pre-fetch task consumed — drop reference so the dict doesn't grow unbounded.
            self._migration_scores.pop(mint, None)

            if self.execution_mode == "live":
                self.live_trade_attempts += 1
                result = await self.live_adapter.execute(intent)
            else:
                result = await self.paper_adapter.execute(intent)

            await self._post_execution(
                mint, intent, result, t0=time.perf_counter(),
                source=entry_source,
                decision_breakdown={
                    "migration_retry_attempt": attempt + 1,
                    "migration_retry_delay_s": MIGRATION_RETRY_DELAYS_SECONDS[attempt] if attempt < len(MIGRATION_RETRY_DELAYS_SECONDS) else DELAYED_ENTRY_SECONDS,
                    "delayed_entry": is_delayed_entry,
                    "reference_price": reference_price,
                    "delay_actual_s": delay_actual_s,
                },
            )
            self._flush_diagnostics()  # flush funnel (execution_attempted) to state file
            self._migration_retry_queue.task_done()

    async def _mint_from_signature_with_source(self, sig: str, logs: Optional[list[str]] = None) -> tuple[Optional[str], str, str]:
        t_parse_start = time.perf_counter()
        if not sig:
            if self.parser_debug_events_left > 0:
                log.warning("[PARSER_DEBUG] missing signature in log event")
            return None, "none", "no_sig"

        timeout_cooldown_until = float(self._sig_timeout_cooldown.get(sig, 0.0) or 0.0)
        now_ts = time.time()
        if timeout_cooldown_until > now_ts:
            queue_telemetry_event("RPC_TIMEOUT_COOLDOWN_SKIP", detail=f"sig={sig}", severity="info")
            return None, "none", "rpc_timeout"

        cached = self._sig_parse_cache.get(sig)
        if cached is not None:
            self._sig_parse_cache.move_to_end(sig)
            self._metrics_bump({"mint_parse_cache_hits": 1, "mint_parse_successes": 1, "mint_parse_latency_sum_ms": (time.perf_counter() - t_parse_start) * 1000}, {"mint_parse_cache_hits": 1, "mint_parse_successes": 1, "mint_parse_latency_sum_ms": (time.perf_counter() - t_parse_start) * 1000})
            return cached

        log_mint, log_source = _extract_mint_from_candidate_logs(logs or [])
        if log_mint:
            result = (log_mint, log_source, "ok")
            self._sig_parse_cache[sig] = result
            while len(self._sig_parse_cache) > SIG_PARSE_CACHE_MAX:
                self._sig_parse_cache.popitem(last=False)
            self._metrics_bump({"mint_parse_successes": 1, "mint_parse_latency_sum_ms": (time.perf_counter() - t_parse_start) * 1000}, {"mint_parse_successes": 1, "mint_parse_latency_sum_ms": (time.perf_counter() - t_parse_start) * 1000})
            return result

        # P5-4: brief yield before first RPC call — reduces Helius burst and lets the event
        # loop process other coroutines (open positions, heartbeat) before blocking on getTransaction.
        await asyncio.sleep(0.2)

        # Primary RPC fallback: lighter raw json payload, then local extraction.
        tx = {}
        saw_rpc_429 = False
        saw_fetch_timeout = False
        tx_seen_but_unusable = False
        pinned_rpc_idx = self.rpc_idx
        rpc_wait_sum_ms = 0.0
        for attempt in range(max(1, MINT_PARSE_RETRIES)):
            if len(RPC_URLS) > 1 and attempt < max(0, MINT_PARSE_PIN_RPC_ATTEMPTS):
                self.rpc_idx = pinned_rpc_idx
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    sig,
                    {"encoding": "json", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"},
                ],
            }
            try:
                t_rpc_wait = time.perf_counter()
                async with _GET_TX_SEMAPHORE:
                    status, d = await self._rpc_post(body, timeout=MINT_PARSE_FETCH_TIMEOUT_SECONDS)
                rpc_wait_sum_ms += (time.perf_counter() - t_rpc_wait) * 1000
                if status != 200:
                    if status == 429:
                        saw_rpc_429 = True
                        self.rpc_429_streak += 1
                        self._metrics_bump({"rpc_429_count": 1}, {"rpc_429_count": 1})
                        append_telemetry_event("RPC_429", detail=f"sig={sig} rpc={self.rpc_url} single_rpc={len(RPC_URLS)==1}", severity="warn")
                        if self.rpc_429_streak >= 1 and len(RPC_URLS) > 1:
                            self.rotate_rpc()
                            pinned_rpc_idx = self.rpc_idx
                            self.rpc_429_streak = 0
                        # Task 5: 429-specific minimum backoff — 0.5s if single RPC (rotation is no-op),
                        # 0.2s if alternate endpoint is available (rotation just happened).
                        _min_429_delay = 0.5 if len(RPC_URLS) <= 1 else 0.2
                    else:
                        _min_429_delay = 0.0
                        self.rpc_429_streak = 0
                        if len(RPC_URLS) > 1 and attempt >= max(0, MINT_PARSE_PIN_RPC_ATTEMPTS - 1):
                            self.rotate_rpc()
                            pinned_rpc_idx = self.rpc_idx
                    if self.parser_debug_events_left > 0:
                        log.warning(f"[PARSER_DEBUG] getTransaction status={status} sig={sig} attempt={attempt+1} rpc={self.rpc_url}")
                    delay = max(_min_429_delay, MINT_PARSE_RETRY_DELAY_SECONDS * (attempt + 1) + random.uniform(0, 0.1))
                    await asyncio.sleep(delay)
                    continue

                self.rpc_429_streak = 0
                queue_telemetry_event("RPC_OK", detail=f"method=getTransaction rpc={self.rpc_url}", severity="info")
                rpc_error = (d or {}).get("error") or {}
                if rpc_error:
                    if len(RPC_URLS) > 1:
                        self.rotate_rpc()
                        pinned_rpc_idx = self.rpc_idx
                    if self.parser_debug_events_left > 0:
                        log.warning(
                            f"[PARSER_DEBUG] getTransaction rpc_error sig={sig} attempt={attempt+1} code={rpc_error.get('code')} msg={rpc_error.get('message')}"
                        )
                    delay = MINT_PARSE_RETRY_DELAY_SECONDS * (attempt + 1) + random.uniform(0, 0.1)
                    await asyncio.sleep(delay)
                    continue

                tx = (d or {}).get("result") or {}
                mint, source, terminal_reason = _extract_mint_from_rpc_tx(tx)
                if self.parser_debug_events_left > 0 and tx:
                    meta = tx.get("meta") or {}
                    post = meta.get("postTokenBalances") or []
                    pre = meta.get("preTokenBalances") or []
                    log.warning(f"[PARSER_DEBUG] sig={sig} status=200 post={len(post)} pre={len(pre)} source={source}")
                    log.warning(f"[PARSER_DEBUG] postTokenBalances={json.dumps(post)[:1200]}")
                    log.warning(f"[PARSER_DEBUG] preTokenBalances={json.dumps(pre)[:1200]}")
                if mint:
                    result = (mint, source, "ok")
                    self._sig_parse_cache[sig] = result
                    while len(self._sig_parse_cache) > SIG_PARSE_CACHE_MAX:
                        self._sig_parse_cache.popitem(last=False)
                    self._metrics_bump({"mint_parse_successes": 1, "mint_parse_retry_count": attempt, "mint_parse_latency_sum_ms": (time.perf_counter() - t_parse_start) * 1000, "mint_parse_gettx_wait_sum_ms": rpc_wait_sum_ms}, {"mint_parse_successes": 1, "mint_parse_retry_count": attempt, "mint_parse_latency_sum_ms": (time.perf_counter() - t_parse_start) * 1000, "mint_parse_gettx_wait_sum_ms": rpc_wait_sum_ms})
                    return result
                if terminal_reason == "excluded_mint":
                    return None, "none", "excluded_mint"
                if tx:
                    tx_seen_but_unusable = True
                    self._metrics_bump({"tx_seen_but_unusable": 1}, {"tx_seen_but_unusable": 1})
                    queue_telemetry_event("TX_SEEN_BUT_UNUSABLE", detail=f"sig={sig} rpc={self.rpc_url} attempt={attempt+1}", severity="info")
                    if len(RPC_URLS) > 1:
                        self.rotate_rpc()
                        pinned_rpc_idx = self.rpc_idx
                    if self.parser_debug_events_left > 0:
                        log.warning(f"[PARSER_DEBUG] tx seen but unusable sig={sig} attempt={attempt+1} rpc={self.rpc_url}")
                    delay = max(MINT_PARSE_LATE_SETTLE_DELAY_SECONDS, MINT_PARSE_RETRY_DELAY_SECONDS * (attempt + 1) + random.uniform(0, 0.1))
                    await asyncio.sleep(delay)
                    continue
                if len(RPC_URLS) > 1 and attempt >= max(0, MINT_PARSE_PIN_RPC_ATTEMPTS - 1):
                    self.rotate_rpc()
                    pinned_rpc_idx = self.rpc_idx
                if self.parser_debug_events_left > 0:
                    log.warning(f"[PARSER_DEBUG] getTransaction result=None sig={sig} attempt={attempt+1}")
                delay = MINT_PARSE_RETRY_DELAY_SECONDS * (attempt + 1) + random.uniform(0, 0.1)
                await asyncio.sleep(delay)
            except Exception as e:
                if isinstance(e, asyncio.TimeoutError) or e.__class__.__name__ == "TimeoutError":
                    saw_fetch_timeout = True
                    elapsed_ms = round((time.perf_counter() - t_rpc_wait) * 1000, 1)
                    self._sig_timeout_cooldown[sig] = time.time() + 10.0
                    self._rpc_timeout_sig_counts[sig] = int(self._rpc_timeout_sig_counts.get(sig, 0) or 0) + 1
                    log.warning(
                        "[RPC_TIMEOUT] getTransaction elapsed_ms=%.0f attempt=%d exc=%s cfg_timeout=%.1fs parse_elapsed_ms=%.0f rpc=%s sig=%s",
                        elapsed_ms, attempt + 1, type(e).__name__, MINT_PARSE_FETCH_TIMEOUT_SECONDS,
                        (time.perf_counter() - t_parse_start) * 1000, self.rpc_url, sig,
                    )
                    queue_telemetry_event("RPC_TIMEOUT", detail=f"sig={sig} rpc={self.rpc_url} elapsed_ms={elapsed_ms} attempt={attempt+1} timeout_cfg_s={MINT_PARSE_FETCH_TIMEOUT_SECONDS} exc={type(e).__name__} rpc_timeout_sig_count={self._rpc_timeout_sig_counts[sig]}", severity="warn")
                    if len(RPC_URLS) > 1:
                        self.rotate_rpc()
                        pinned_rpc_idx = self.rpc_idx
                    break
                if self.parser_debug_events_left > 0:
                    log.warning(f"[PARSER_DEBUG] getTransaction exception sig={sig} attempt={attempt+1} err={e.__class__.__name__}:{e}")
                delay = MINT_PARSE_RETRY_DELAY_SECONDS * (attempt + 1) + random.uniform(0, 0.1)
                await asyncio.sleep(delay)

        if not tx:
            if saw_rpc_429:
                return None, "none", "rpc_429"
            if saw_fetch_timeout:
                return None, "none", "rpc_timeout"
            return None, "none", "no_balances"
        mint, source, terminal = _extract_mint_from_rpc_tx(tx)
        if mint:
            result = (mint, source, terminal)
            self._sig_parse_cache[sig] = result
            while len(self._sig_parse_cache) > SIG_PARSE_CACHE_MAX:
                self._sig_parse_cache.popitem(last=False)
            self._metrics_bump({"mint_parse_successes": 1, "mint_parse_retry_count": max(0, MINT_PARSE_RETRIES - 1), "mint_parse_latency_sum_ms": (time.perf_counter() - t_parse_start) * 1000, "mint_parse_gettx_wait_sum_ms": rpc_wait_sum_ms}, {"mint_parse_successes": 1, "mint_parse_retry_count": max(0, MINT_PARSE_RETRIES - 1), "mint_parse_latency_sum_ms": (time.perf_counter() - t_parse_start) * 1000, "mint_parse_gettx_wait_sum_ms": rpc_wait_sum_ms})
            return result
        return mint, source, terminal

    async def _mint_from_signature(self, sig: str, logs: Optional[list[str]] = None) -> Optional[str]:
        mint, _source, _terminal_reason = await self._mint_from_signature_with_source(sig, logs=logs)
        return mint

    async def parse_candidate_mint(self, msg: dict) -> tuple[Optional[str], str, str]:
        value = ((msg.get("params") or {}).get("result") or {}).get("value") or {}
        logs = value.get("logs") or []
        sig = value.get("signature")
        terminal_reason = "no_sig" if not sig else "no_balances"
        low_confidence_rejected = False

        if self.parser_debug_events_left > 0:
            log.warning(f"[PARSER_DEBUG] parse start sig={sig} logs={len(logs)}")

        local_tx_blacklist = set()
        for line in logs:
            mprog = re.search(r"(?i)program\s+([1-9A-HJ-NP-Za-km-z]{32,44})\s+invoke", str(line))
            if mprog:
                local_tx_blacklist.add(mprog.group(1).strip())

        for line in logs:
            for token in re.findall(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b", str(line)):
                if token in local_tx_blacklist:
                    continue
                if is_valid_pump_mint(token):
                    if self.parser_debug_events_left > 0:
                        log.warning(f"[PARSER_DEBUG] pump-lane mint candidate={token}")
                    self.parser_debug_events_left -= 1 if self.parser_debug_events_left > 0 else 0
                    return token, "pump_log", "ok"

        # P1-2: For Raydium pool creations, try extracting base/quote mint from
        # Initialize2 log lines BEFORE falling through to the slow RPC getTransaction.
        # The CONTEXTUAL_MINT_PATTERNS already match initialize2/baseMint/quoteMint/etc.
        if self.is_migration_signature(logs):
            for line in logs:
                cand = _extract_contextual_mint_from_line(str(line))
                if cand and cand not in local_tx_blacklist and is_valid_dual_lane_mint(cand):
                    if self.parser_debug_events_left > 0:
                        log.warning(f"[PARSER_DEBUG] raydium-contextual mint candidate={cand}")
                    self.parser_debug_events_left -= 1 if self.parser_debug_events_left > 0 else 0
                    return cand, "raydium_log_context", "ok"

        mint_from_tx, mint_source, tx_terminal_reason = await self._mint_from_signature_with_source(sig, logs=logs) if sig else (None, "none", terminal_reason)
        terminal_reason = tx_terminal_reason or terminal_reason
        if mint_from_tx and is_high_confidence_mint_source(mint_from_tx, mint_source):
            if self.parser_debug_events_left > 0:
                log.warning(f"[PARSER_DEBUG] tx mint candidate={mint_from_tx} source={mint_source}")
            self.parser_debug_events_left -= 1 if self.parser_debug_events_left > 0 else 0
            return mint_from_tx, mint_source, "ok"
        if mint_from_tx:
            low_confidence_rejected = True
            queue_telemetry_event("LOW_CONFIDENCE_MINT_REJECTED", mint=mint_from_tx, detail=f"source={mint_source}", severity="info")

        for line in logs:
            cand = _extract_contextual_mint_from_line(line)
            if cand and cand not in local_tx_blacklist and is_valid_dual_lane_mint(cand):
                if is_high_confidence_mint_source(cand, "contextual_log"):
                    if self.parser_debug_events_left > 0:
                        log.warning(f"[PARSER_DEBUG] contextual mint candidate={cand}")
                    self.parser_debug_events_left -= 1 if self.parser_debug_events_left > 0 else 0
                    return cand, "contextual_log", "ok"
                low_confidence_rejected = True
                queue_telemetry_event("LOW_CONFIDENCE_MINT_REJECTED", mint=cand, detail="source=contextual_log", severity="info")
                break

        for line in logs:
            for token in re.findall(r"\b([1-9A-HJ-NP-Za-km-z]{43,44})\b", str(line)):
                if token in local_tx_blacklist:
                    continue
                if ("AAAA" in token) or (not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{43,44}", token)):
                    continue
                if not is_valid_dual_lane_mint(token):
                    continue
                if is_high_confidence_mint_source(token, "loose_log"):
                    if self.parser_debug_events_left > 0:
                        log.warning(f"[PARSER_DEBUG] loose-log mint candidate={token}")
                    self.parser_debug_events_left -= 1 if self.parser_debug_events_left > 0 else 0
                    return token, "loose_log", "ok"
                low_confidence_rejected = True
                queue_telemetry_event("LOW_CONFIDENCE_MINT_REJECTED", mint=token, detail="source=loose_log", severity="info")
                break

        if self.parser_debug_events_left > 0:
            log.warning(f"[PARSER_DEBUG] no mint extracted sig={sig}")
            self.parser_debug_events_left -= 1
        if low_confidence_rejected:
            return None, "none", "low_confidence"
        return None, "none", terminal_reason

    # Exact Raydium program IDs — used as primary signal in transaction logs.
    _RAYDIUM_AMM_V4_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
    _RAYDIUM_CPMM_PROGRAM_ID   = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
    _PUMPSWAP_AMM_PROGRAM_ID   = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

    def is_migration_signature(self, logs: list[str]) -> bool:
        raw = " ".join(logs)
        t = raw.lower()

        # Primary: exact Raydium program ID in logs (Solana emits "Program <ID> invoke [N]").
        # Fallback: log-string signals for cases where CPI logs are truncated by RPC provider.
        # Market temp = 0 with tight program-ID-only check indicates some providers truncate
        # inner-instruction logs — so we keep pool-init strings as an explicit fallback.
        has_raydium_program = (
            self._RAYDIUM_AMM_V4_PROGRAM_ID in raw
            or self._RAYDIUM_CPMM_PROGRAM_ID in raw
        )
        has_pool_signal_fallback = (
            "initialize2" in t
            or "initialize_pool" in t
            or "raydium" in t
        )
        # PumpSwap: pump.fun's native DEX (95%+ of migrations since March 2025).
        # These logs contain "instruction: migrate" + pAMMBay program CPI but NO Raydium IDs.
        has_pumpswap_program = self._PUMPSWAP_AMM_PROGRAM_ID in raw

        # Must have a pump.fun migration instruction to confirm bonding→DEX flow.
        has_migration_signal = (
            "instruction: migrate" in t
            or ("bonding" in t and "complete" in t)
            or "migrate_to_amm" in t
        )

        has_token_context = (
            "tokenkeg" in t
            or "spl-token" in t
            or "mint" in t
            or "base mint" in t
            or "quote mint" in t
        )

        # Accept if migration signal + token context AND any of:
        # (a) exact Raydium program ID in logs (Raydium migration — now rare)
        # (b) pool-init log strings present (fallback for truncated CPI logs from some RPCs)
        # (c) PumpSwap AMM program ID in logs (pump.fun native DEX — dominant since Mar 2025)
        return has_migration_signal and (has_raydium_program or has_pool_signal_fallback or has_pumpswap_program) and has_token_context

    def is_raydium_pool_creation(self, logs: list[str]) -> bool:
        """Detect direct Raydium pool creation events (not necessarily from pump.fun).

        Used when subscribed to Raydium program IDs directly.  Catches pool
        creations from ANY launchpad, not just pump.fun migrations.
        Requires: Raydium program ID + pool initialization instruction + token context.
        """
        raw = " ".join(logs)
        t = raw.lower()

        has_raydium_program = (
            RAYDIUM_AMM_V4_PROGRAM_ID in raw
            or RAYDIUM_CPMM_PROGRAM_ID in raw
        )
        if not has_raydium_program:
            return False

        has_pool_init = (
            "initialize2" in t
            or "initialize_pool" in t
            or "create_pool" in t
        )

        has_token_context = (
            "tokenkeg" in t
            or "spl-token" in t
            or "mint" in t
        )

        return has_pool_init and has_token_context

    def _synthetic_price(self, tr: PaperTrade) -> float:
        dt = max(0.0, time.time() - tr.ts)
        growth = 2.4 * (1 - math.exp(-dt / 120.0))
        wiggle = 0.25 * math.sin(dt / 10.0)
        return max(0.1, tr.entry_px * (1 + growth + wiggle))

    async def _refresh_trade_mark(self, tr: PaperTrade) -> bool:
        debug = bool(MARK_DEBUG_MINT and tr.mint == MARK_DEBUG_MINT)
        prev_last_update = float(tr.last_price_update_at or 0.0)
        full_qty_tokens = max(0, int(float(tr.qty_remaining or 0.0)))
        mark_qty_tokens, mark_notional_usd_used = self._mark_quote_qty_tokens(tr)
        px, mark_source, mark_confidence_level, executable_px = await self._mark_price_with_retries(tr.mint, qty_tokens=mark_qty_tokens)
        now_ts = time.time()
        if px is not None and px > 0:
            tr.current_px = px
            tr.last_price = px
            used_exec_quote = (mark_source == "executable_notional_quote") and executable_px is not None and mark_qty_tokens > 0
            tr.mark_price_source = mark_source
            tr.current_px_source = mark_source
            tr.mark_confidence_level = mark_confidence_level
            tr.mark_notional_usd_used = mark_notional_usd_used if used_exec_quote else 0.0
            tr.mark_notional_size_used = float(mark_qty_tokens) if used_exec_quote else 0.0
            # Track ALL mark observations (any source) for quoteability
            tr.total_mark_observations = int(tr.total_mark_observations or 0) + 1
            if not tr.first_mark_ts:
                tr.first_mark_ts = now_ts
            # Append source to sequence (cap at 50 entries to prevent unbounded growth)
            prev_seq = str(tr.mark_source_sequence or "")
            seq_parts = prev_seq.split(",") if prev_seq else []
            seq_parts.append(mark_source)
            tr.mark_source_sequence = ",".join(seq_parts[-50:])
            if used_exec_quote:
                tr.executable_mark_observations = int(tr.executable_mark_observations or 0) + 1
                if not tr.first_executable_mark_ts:
                    tr.first_executable_mark_ts = now_ts
                # Sentinel entry_px cleanup: first executable mark rewrites entry_px to a real value
                # so exit math (TP/trail/HARD_CAP) fires against a valid baseline. qty_total still
                # reflects the original sentinel-derived value — sell path reads actual wallet
                # balance, so qty drift is tolerable here.
                if getattr(tr, "entry_px_sentinel", False):
                    tr.entry_px = float(px)
                    tr.entry_price = float(px)
                    tr.high_px = float(px)
                    tr.low_px = float(px)
                    tr.stop_px = float(px) * (1 - DEFAULT_DOWNSIDE_STOP_PCT)
                    tr.entry_px_sentinel = False
                    log.warning(
                        "[SENTINEL_CLEAR] mint=%s entry_px rewritten to first executable mark=%.10g; exits unblocked",
                        tr.mint, px,
                    )
            if used_exec_quote and full_qty_tokens > mark_qty_tokens and full_qty_tokens > 0:
                full_liq_px = await self._executable_mark_price(tr.mint, full_qty_tokens)
                if full_liq_px is not None and full_liq_px > 0:
                    tr.mark_vs_full_liq_diff_pct = ((px - full_liq_px) / full_liq_px) * 100.0
                else:
                    tr.mark_vs_full_liq_diff_pct = None
            else:
                tr.mark_vs_full_liq_diff_pct = None
            tr.last_mark_ts = now_ts
            tr.last_price_update_at = now_ts
            tr.mark_stale = False
            tr.mark_staleness_seconds = 0.0
            if tr.restored_from_state_ts:
                tr.fresh_mark_seen_since_restore = True
                tr.restore_mark_age_seconds = max(0.0, now_ts - float(tr.restored_from_state_ts or now_ts))
            tr.high_px = max(float(tr.high_px or tr.entry_px or px), px)
            tr.low_px = min(float(tr.low_px or tr.entry_px or px), px)
            if float(tr.entry_px or 0.0) > 0:
                tr.mfe_pct = max(float(tr.mfe_pct or 0.0), ((tr.high_px - tr.entry_px) / tr.entry_px) * 100.0)
                tr.mae_pct = min(float(tr.mae_pct or 0.0), ((tr.low_px - tr.entry_px) / tr.entry_px) * 100.0)
            if debug:
                log.warning(f"[MARK_DEBUG] mint={tr.mint} action=refresh_success price={px} last_price_update_at_changed={tr.last_price_update_at != prev_last_update}")
            return True

        # Keep the last good live mark if we have one, but expose staleness explicitly.
        if tr.last_price and tr.last_price > 0:
            # Bug fix: when last_price_update_at=0 (no mark ever confirmed), fall back to
            # entry_ts so stale_secs accumulates from entry instead of resetting to 0 every
            # 1.5s. Without this, STALE_MARK_TIMEOUT_EXIT never fires for tokens that return
            # a stale retained price without a recorded update timestamp.
            _stale_ref = float(tr.last_price_update_at or tr.entry_ts or tr.ts or 0.0)
            stale_secs = max(0.0, now_ts - _stale_ref) if _stale_ref else 0.0
            tr.current_px = tr.last_price
            tr.mark_notional_usd_used = float(tr.mark_notional_usd_used or 0.0)
            tr.mark_notional_size_used = float(tr.mark_notional_size_used or 0.0)
            tr.mark_staleness_seconds = stale_secs
            recent_grace = bool(tr.last_price_update_at and stale_secs < MARK_PRICE_RECENT_GRACE_SECONDS)
            tr.mark_stale = (not tr.last_price_update_at) or (stale_secs >= MARK_PRICE_STALE_AFTER_SECONDS and not recent_grace)
            tr.mark_price_source = "stale" if tr.mark_stale else str(tr.mark_price_source or "mid_price")
            tr.mark_confidence_level = "LOW" if tr.mark_stale else str(tr.mark_confidence_level or "MED")
            tr.current_px_source = "stale" if tr.mark_stale else str(tr.mark_price_source or "mid_price")
            if debug:
                log.warning(f"[MARK_DEBUG] mint={tr.mint} action=refresh_miss stale_secs={stale_secs:.3f} recent_grace={recent_grace} retained_source={tr.current_px_source} last_price_update_at_changed={tr.last_price_update_at != prev_last_update}")
            return False

        # No live mark has ever been observed: leave entry reference visible but clearly stale/unpriced.
        tr.current_px = tr.entry_px
        tr.last_price = tr.entry_px
        tr.mark_price_source = "fallback"
        tr.mark_notional_usd_used = 0.0
        tr.mark_notional_size_used = 0.0
        tr.mark_confidence_level = "LOW"
        tr.mark_vs_full_liq_diff_pct = None
        tr.current_px_source = "stale"
        tr.mark_stale = True
        tr.mark_staleness_seconds = max(0.0, now_ts - float(tr.entry_ts or tr.ts or now_ts))
        if debug:
            log.warning(f"[MARK_DEBUG] mint={tr.mint} action=no_live_mark entry_reference_retained")
        return False

    def _canonical_close_exists(self, tr: PaperTrade) -> bool:
        entry_ts = int(tr.entry_ts or tr.ts or 0)
        for row in reversed(load_history()[-500:]):
            if row.get("type") != "trade_close":
                continue
            if str(row.get("mint") or "") != tr.mint:
                continue
            if int(row.get("entry_ts") or 0) != entry_ts:
                continue
            return True
        return False

    def _ensure_trade_close_integrity(self, tr: PaperTrade, reason: str = "") -> bool:
        if float(tr.qty_remaining or 0.0) > 1e-9:
            return False
        tr.qty_remaining = 0.0
        tr.closed = True
        tr.status = "closed"
        if reason:
            tr.close_reason = reason
        if not tr.exit_ts:
            tr.exit_ts = time.time()
        if not tr.exit_price:
            tr.exit_price = tr.current_px
        tr.remaining_basis_usd = 0.0
        tr.realized_basis_usd = float(tr.cost_basis_total_usd or tr.size_usd)
        tr.realized_fees_usd = float(tr.fees_usd or 0.0)
        tr.qty_closed = max(float(tr.qty_total or 0.0), float(tr.qty_closed or 0.0))
        if tr.close_recorded or tr.canonical_close_emitted or self._canonical_close_exists(tr):
            tr.close_recorded = True
            tr.canonical_close_emitted = True
            if not tr.close_event_emitted_ts:
                tr.close_event_emitted_ts = float(tr.exit_ts or time.time())
            return False
        self._finalize_trade_close(tr, tr.close_reason or reason or "AUTO CLOSE")
        return True

    def _finalize_trade_close(self, tr: PaperTrade, close_reason: str):
        if tr.close_recorded or tr.canonical_close_emitted or self._canonical_close_exists(tr):
            tr.close_recorded = True
            tr.canonical_close_emitted = True
            if not tr.close_event_emitted_ts:
                tr.close_event_emitted_ts = float(tr.exit_ts or time.time())
            return
        tr.closed = True
        tr.status = "closed"
        tr.close_reason = close_reason
        if not tr.exit_ts:
            tr.exit_ts = time.time()
        if not tr.exit_price:
            tr.exit_price = tr.current_px
        # Wallet-aligned ROI: (proceeds - cost) / cost, matching on-chain SOL-in vs SOL-out.
        # Avoids fee-denominator inflation: fees are a cost baked into pnl, not a denominator.
        _finalize_cost = float(tr.cost_basis_total_usd or tr.size_usd)
        _finalize_proceeds = float(tr.proceeds_usd or 0.0)
        _is_ghost_close_finalize = str(tr.position_truth_state or "") == "GHOST_CLOSE_PENDING"
        if _finalize_proceeds > 0 and _finalize_cost > 0:
            tr.realized_roi_pct = (_finalize_proceeds / _finalize_cost - 1.0) * 100.0
        elif _is_ghost_close_finalize:
            # Ghost close: proceeds=0 because wallet_raw=0 at sell time (tokens not settled).
            # Do not write -106% ROI. Leave realized_roi_pct unresolved (0.0 default);
            # ghost reconciliation terminalize will handle cleanup without PnL mutation.
            pass
        elif _finalize_cost > 0:
            tr.realized_roi_pct = (float(tr.realized_pnl or 0.0) / _finalize_cost) * 100.0
        else:
            tr.realized_roi_pct = 0.0

        # Hard floor: a position cannot lose more than 100% of its cost.
        # Sub-100% ROI (e.g. -106%) indicates corrupted proceeds (unit mismatch, ghost close
        # path) — clamp to -100% so the signal is "full loss" not "mathematically impossible".
        if tr.realized_roi_pct is not None and tr.realized_roi_pct < -100.0:
            log.warning(
                "[FINALIZE] realized_roi_pct=%.2f%% < -100%% mint=%s — clamping to -100%% (corrupted proceeds suspected)",
                tr.realized_roi_pct, tr.mint,
            )
            tr.realized_roi_pct = -100.0

        # P3-2: Invariant checks — violations are logged and tagged but never suppress the close.
        _integrity_errors: list[str] = []
        if not close_reason:
            _integrity_errors.append("missing_close_reason")
        if not tr.mint:
            _integrity_errors.append("missing_mint")
        if tr.entry_ts and tr.exit_ts and float(tr.exit_ts) < float(tr.entry_ts):
            _integrity_errors.append(f"exit_before_entry:entry={tr.entry_ts:.0f}_exit={tr.exit_ts:.0f}")
        if tr.realized_roi_pct is None:
            _integrity_errors.append("missing_roi")
        if _integrity_errors:
            log.error(
                "[CLOSE_INTEGRITY] invariant failure mint=%s reason=%s errors=%s",
                tr.mint, close_reason, _integrity_errors,
            )

        # Telemetry-only: mark-quality stats (pure reads from PaperTrade).
        _tc_total_obs = int(getattr(tr, "total_mark_observations", 0) or 0)
        _tc_exec_obs = int(getattr(tr, "executable_mark_observations", 0) or 0)
        _tc_stale_ratio: Optional[float] = None
        if _tc_total_obs > 0:
            _tc_stale_ratio = round(1.0 - (_tc_exec_obs / _tc_total_obs), 4)

        close_event = {
            "type": "trade_close",
            "mint": tr.mint,
            "decision_id": tr.decision_id or "",
            "entry_ts": int(tr.entry_ts or tr.ts or 0),
            "ts": int(tr.exit_ts),
            "hold_time_seconds": round(max(0.0, float(tr.exit_ts or time.time()) - float(tr.entry_ts or tr.ts or time.time())), 3),
            "pnl_usd": round(tr.realized_pnl, 4),
            "roi_pct": round(tr.realized_roi_pct, 2),
            "exit_price": round(tr.exit_price, 10),
            "close_reason": close_reason,
            "qty_closed": round(tr.qty_closed, 6),
            "qty_remaining": round(tr.qty_remaining, 6),
            "proceeds_usd": round(tr.proceeds_usd, 6),
            "fees_usd": round(tr.fees_usd, 6),
            "realized_basis_usd": round(tr.realized_basis_usd, 6),
            "remaining_basis_usd": round(tr.remaining_basis_usd, 6),
            "realized_fees_usd": round(tr.realized_fees_usd, 6),
            "mfe_pct": round(tr.mfe_pct, 4),
            "mae_pct": round(tr.mae_pct, 4),
            "stop_trigger_ts": round(float(tr.stop_trigger_ts or 0.0), 3),
            "sell_attempt_ts": round(float(tr.sell_attempt_ts or 0.0), 3),
            "sell_success_ts": round(float(tr.sell_success_ts or 0.0), 3),
            # --- telemetry-only: mark-quality & lifecycle (pure reads, no logic change) ---
            "entry_price": round(float(getattr(tr, "entry_px", 0.0) or 0.0), 10),
            "cost_basis_total_usd": round(float(getattr(tr, "cost_basis_total_usd", 0.0) or 0.0), 6),
            "high_px": round(float(getattr(tr, "high_px", 0.0) or 0.0), 10),
            "low_px": round(float(getattr(tr, "low_px", 0.0) or 0.0), 10),
            "mark_price_source": str(getattr(tr, "mark_price_source", "") or ""),
            "current_px_source": str(getattr(tr, "current_px_source", "") or ""),
            "mark_stale_final": bool(getattr(tr, "mark_stale", False)),
            "mark_staleness_seconds": round(float(getattr(tr, "mark_staleness_seconds", 0.0) or 0.0), 3),
            "total_mark_observations": _tc_total_obs,
            "executable_mark_observations": _tc_exec_obs,
            "had_executable_mark": _tc_exec_obs > 0,
            "stale_mark_ratio": _tc_stale_ratio,
            "first_executable_mark_ts": round(float(getattr(tr, "first_executable_mark_ts", 0.0) or 0.0), 3),
            "first_mark_ts": round(float(getattr(tr, "first_mark_ts", 0.0) or 0.0), 3),
            "mark_source_sequence": str(getattr(tr, "mark_source_sequence", "") or ""),
            "position_truth_state": str(getattr(tr, "position_truth_state", "") or ""),
        }
        if _integrity_errors:
            close_event["close_integrity_error"] = True
            close_event["close_integrity_violations"] = _integrity_errors

        # P2-2 / P1-1: opportunity age — time from signal detection to entry (latency diagnostic)
        _detect_ts = float(tr.detect_ts_wall or 0.0)
        _entry_ts = float(tr.entry_ts or tr.ts or 0.0)
        if _detect_ts > 0 and _entry_ts >= _detect_ts:
            close_event["opportunity_age_ms"] = round((_entry_ts - _detect_ts) * 1000.0, 1)
            close_event["late_entry"] = close_event["opportunity_age_ms"] > 5000.0
        else:
            close_event["opportunity_age_ms"] = None
            close_event["late_entry"] = None

        self._finalize_hot_until = max(float(self._finalize_hot_until or 0.0), time.time() + 3.0)
        self.closed_trades.append(close_event)
        self.closed_trades = self.closed_trades[-200:]
        append_history(close_event)
        tr.close_recorded = True
        tr.canonical_close_emitted = True
        tr.close_event_emitted_ts = float(tr.exit_ts or time.time())

        if tr.realized_pnl >= 0:
            self._metrics_bump({"closed_wins": 1}, {"closed_wins": 1})
        else:
            self._metrics_bump({"closed_losses": 1}, {"closed_losses": 1})

        # A1 + P1.10: Feed realized P&L into daily loss tracker so CANARY_MAX_DAILY_LOSS_USD
        # cap is enforced in BOTH paper and live modes (paper contributes to daily_loss_usd
        # only — lifetime live totals are untouched). Book accumulated Jito tips (buy + every
        # TP/sell leg) against this mint in live mode so the cap reflects true net wallet impact.
        try:
            from live_canary import record_canary_trade, get_accumulated_tip_usd
            if self.execution_mode == "live":
                # Sync context: read cached SOL/USD directly (populated by periodic _sol_usd() calls).
                _cached_sol = float(self._sol_usd_cache[0]) if self._sol_usd_cache[0] > 0 else 150.0
                tip_usd = get_accumulated_tip_usd(tr.mint, _cached_sol) if tr.mint else 0.0
                record_canary_trade(tr.realized_pnl, jito_tip_usd=tip_usd, mint=tr.mint, mode="live")
            else:
                record_canary_trade(tr.realized_pnl, jito_tip_usd=0.0, mint=tr.mint, mode="paper")
        except Exception as _rec_err:
            log.warning("[CANARY_TRADE] record failed mint=%s mode=%s: %s", tr.mint, self.execution_mode, _rec_err)

        # Forensic trace: emit rich event for any failsafe or severe loss (>= -80% ROI)
        is_forensic = close_reason == "FAILSAFE STOP" or tr.realized_roi_pct <= -80.0
        if is_forensic:
            hold_s = round(max(0.0, float(tr.exit_ts or time.time()) - float(tr.entry_ts or tr.ts or time.time())), 1)
            append_telemetry_event(
                "TRADE_FORENSIC_TRACE", mint=tr.mint,
                detail=(
                    f"reason={close_reason} roi={tr.realized_roi_pct:.2f}% pnl={tr.realized_pnl:.4f} "
                    f"mfe={tr.mfe_pct:.2f}% mae={tr.mae_pct:.2f}% hold_s={hold_s} "
                    f"entry_px={tr.entry_px} exit_px={tr.exit_price} size_usd={tr.size_usd:.3f} "
                    f"forced_exit_unresolved={getattr(tr, 'forced_exit_unresolved', False)}"
                ),
                severity="error",
            )

    # Sentinel: returned by _fetch_live_token_balance when the RPC call failed or timed out.
    # Distinct from None (never used) and 0 (confirmed zero balance).
    _BALANCE_FETCH_ERROR: int = -1

    async def _fetch_live_token_balance(self, mint: str) -> int:
        """
        Return raw (smallest-unit) token balance for this mint in the live wallet.

        Return values:
          >0  — confirmed positive balance (tokens present)
           0  — confirmed zero balance (no token accounts or all zero)
          -1  — fetch failed / RPC timeout / uncertain  (use self._BALANCE_FETCH_ERROR)

        Strategy: race primary + alternate RPC in parallel. Take the first success.
        This halves latency vs sequential and survives primary endpoint degradation.
        """
        wallet_pubkey: Optional[str] = None
        try:
            from live_canary import get_wallet_pubkey
            wallet_pubkey = get_wallet_pubkey()
        except Exception:
            pass
        if not wallet_pubkey:
            log.warning("[LIVE_SELL] token_balance_fetch: no wallet_pubkey available mint=%s", mint)
            return self._BALANCE_FETCH_ERROR
        req = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                wallet_pubkey,
                {"mint": mint},
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        }

        def _parse_balance(data: Any) -> int:
            vals = ((data or {}).get("result") or {}).get("value") or []
            total_raw = 0
            for acct in vals:
                amt = (
                    ((((acct.get("account") or {}).get("data") or {}).get("parsed") or {})
                     .get("info", {}).get("tokenAmount") or {}).get("amount")
                )
                try:
                    total_raw += int(amt or 0)
                except Exception:
                    pass
            return total_raw  # 0 = confirmed zero, >0 = tokens present

        async def _try_url(url: str, label: str) -> Optional[int]:
            """Attempt balance fetch from a specific RPC URL. Returns int on success, None on failure."""
            import aiohttp as _aiohttp
            try:
                s = await self._get_http_session()
                ct = _aiohttp.ClientTimeout(total=2.5, connect=2.0, sock_connect=2.0, sock_read=2.5)
                async with s.post(url, json=req, timeout=ct) as r:
                    if r.status == 200:
                        data = await r.json()
                        result = _parse_balance(data)
                        append_telemetry_event(
                            "LIVE_BALANCE_FETCH_ATTEMPT", mint=mint,
                            detail=f"raw={result} endpoint={label}", severity="info",
                        )
                        return result
                    append_telemetry_event(
                        "LIVE_BALANCE_FETCH_ENDPOINT_FAIL", mint=mint,
                        detail=f"http={r.status} endpoint={label}", severity="warn",
                    )
                    return None
            except Exception as exc:
                append_telemetry_event(
                    "LIVE_BALANCE_FETCH_ENDPOINT_FAIL", mint=mint,
                    detail=f"exc={type(exc).__name__} endpoint={label}", severity="warn",
                )
                return None

        # Race primary + alternate endpoint simultaneously.
        # Build task list: always primary, add alternate if a second URL exists.
        primary_url = RPC_URLS[0]
        alt_url = next((u for u in RPC_URLS[1:] if u and u != primary_url), None)

        tasks: list = [asyncio.create_task(_try_url(primary_url, "primary"))]
        if alt_url:
            tasks.append(asyncio.create_task(_try_url(alt_url, "alt")))

        result: Optional[int] = None
        try:
            done, pending = await asyncio.wait(tasks, timeout=3.0, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                val = t.result()
                if val is not None:
                    result = val
                    break
            # If first winner returned None (HTTP error), check remaining done tasks
            if result is None:
                for t in done:
                    try:
                        val = t.result()
                        if val is not None:
                            result = val
                            break
                    except Exception:
                        pass
        except Exception as exc:
            log.error("[LIVE_SELL] token_balance_fetch race failed mint=%s: %s", mint, exc)
            for t in tasks:
                if not t.done():
                    t.cancel()

        if result is not None:
            log.info("[LIVE_SELL] token_balance_fetch mint=%s raw=%d (race succeeded)", mint, result)
            append_telemetry_event(
                "LIVE_BALANCE_FETCH_OK", mint=mint,
                detail=f"raw={result} strategy=race", severity="info",
            )
            return result

        # Both endpoints failed — last-resort: retry primary once with longer timeout
        try:
            status, data = await self._rpc_post(req, timeout=3.5)
            if status == 200:
                fallback_result = _parse_balance(data)
                log.warning("[LIVE_SELL] token_balance_fetch mint=%s raw=%d (fallback/retry)", mint, fallback_result)
                append_telemetry_event(
                    "LIVE_BALANCE_FETCH_RECOVERED", mint=mint,
                    detail=f"raw={fallback_result} strategy=fallback_retry", severity="warn",
                )
                return fallback_result
        except Exception as exc:
            log.error("[LIVE_SELL] token_balance_fetch fallback failed mint=%s: %s", mint, exc)

        append_telemetry_event(
            "LIVE_BALANCE_FETCH_HARD_FAIL", mint=mint,
            detail="race+fallback all failed — balance unknown", severity="error",
        )
        # Backward compat: also emit old event name so existing log monitors still fire
        append_telemetry_event(
            "LIVE_BALANCE_FETCH_FAILED", mint=mint,
            detail="both_attempts_failed", severity="error",
        )
        return self._BALANCE_FETCH_ERROR

    async def _live_sell_gate(
        self,
        tr: "PaperTrade",
        qty_tracked: float,
        qty_before: float,
        tag: str,
    ) -> tuple[bool, Optional[float]]:
        """
        Submit an on-chain sell TX for a live position.

        Returns (success, actual_proceeds_usd):
          success=True  → TX submitted; caller proceeds with paper accounting
          success=False → TX failed; caller must NOT write paper close
          actual_proceeds_usd → USD value from Jupiter quote if available, else None
        """
        try:
            from live_canary import execute_live_sell
        except ImportError:
            log.error("[LIVE_SELL] execute_live_sell unavailable mint=%s tag=%s", tr.mint, tag)
            append_telemetry_event("LIVE_SELL_UNAVAILABLE", mint=tr.mint, detail=f"tag={tag}", severity="error")
            return False, None

        is_stop_exit = tag in ("TRAIL/SL EXIT", "FAILSAFE STOP", "RAPID DUMP EXIT",
                               "STALE_MARK_TIMEOUT_EXIT", "QUOTEABILITY_GUARD_EXIT", "AGE_GATE_EXIT",
                               "HARD_CAP_EXIT")

        # For stop-loss/emergency exits: skip the slow balance fetch (2.5-4.5s)
        # and use the state-tracked qty directly.  This shaves seconds off the
        # critical path where every second costs more loss.
        # For non-emergency sells (TP, manual), fetch the real balance for accuracy.
        if is_stop_exit:
            # Emergency path: still fetch real balance but with aggressive timeout.
            # We need the real raw token amount (smallest units), not the paper qty
            # which is in token units without decimals applied.
            wallet_raw = await self._fetch_live_token_balance(tr.mint)
            if wallet_raw == self._BALANCE_FETCH_ERROR:
                # Balance fetch failed — can't sell safely without knowing amount.
                log.error(
                    "[LIVE_SELL] FAST_STOP balance fetch failed mint=%s tag=%s — retrying once",
                    tr.mint, tag,
                )
                # One more attempt before giving up
                wallet_raw = await self._fetch_live_token_balance(tr.mint)
                if wallet_raw == self._BALANCE_FETCH_ERROR:
                    log.error("[LIVE_SELL] FAST_STOP balance fetch failed twice mint=%s tag=%s — blocking", tr.mint, tag)
                    return False, None
            if wallet_raw == 0:
                # Guard: RPC balance hasn't propagated yet if the buy just confirmed.
                # A confirmed buy TX takes 3-8s to appear in token-account RPC responses.
                # If the position is younger than 5s, wait 3s and retry once before
                # accepting 0 as truth to prevent a spurious ghost close.
                _hold_s = time.time() - float(tr.entry_ts or tr.ts or time.time())
                if _hold_s < 5.0:
                    log.warning(
                        "[LIVE_SELL] FAST_STOP wallet_raw=0 mint=%s tag=%s hold_s=%.1f — "
                        "position too young for zero to be trusted; waiting 3s for RPC propagation",
                        tr.mint, tag, _hold_s,
                    )
                    append_telemetry_event(
                        "LIVE_SELL_BALANCE_PROPAGATION_WAIT", mint=tr.mint,
                        detail=f"tag={tag} hold_s={_hold_s:.1f} wallet_raw=0 — retrying after 3s",
                        severity="warn",
                    )
                    await asyncio.sleep(3.0)
                    wallet_raw = await self._fetch_live_token_balance(tr.mint)
                    if wallet_raw == self._BALANCE_FETCH_ERROR:
                        log.error(
                            "[LIVE_SELL] FAST_STOP propagation-retry fetch failed mint=%s — blocking",
                            tr.mint,
                        )
                        return False, None
                    if wallet_raw > 0:
                        log.warning(
                            "[LIVE_SELL] FAST_STOP propagation-retry confirmed mint=%s wallet_raw=%d — proceeding with sell",
                            tr.mint, wallet_raw,
                        )
                        # Fall through to normal sell path below
                    else:
                        log.warning(
                            "[LIVE_SELL] FAST_STOP wallet_balance=0 after propagation-retry mint=%s tag=%s — ghost close",
                            tr.mint, tag,
                        )
                        return True, 0.0
                else:
                    log.warning(
                        "[LIVE_SELL] FAST_STOP wallet_balance=0 mint=%s tag=%s hold_s=%.1f — ghost close",
                        tr.mint, tag, _hold_s,
                    )
                    return True, 0.0
            log.warning(
                "[LIVE_SELL] FAST_STOP_PATH mint=%s tag=%s — wallet_raw=%d",
                tr.mint, tag, wallet_raw,
            )
            append_telemetry_event(
                "LIVE_SELL_FAST_STOP", mint=tr.mint,
                detail=f"tag={tag} wallet_raw={wallet_raw}", severity="info",
            )
        else:
            # Non-emergency path: fetch real wallet balance for accuracy.
            wallet_raw = await self._fetch_live_token_balance(tr.mint)

            if wallet_raw == self._BALANCE_FETCH_ERROR:
                log.error(
                    "[LIVE_SELL] BALANCE_FETCH_FAILED mint=%s tag=%s — RPC uncertain, blocking paper close",
                    tr.mint, tag,
                )
                append_recent_activity(action="EXIT BLOCKED", mint=tr.mint, live_pnl=0.0)
                append_telemetry_event(
                    "LIVE_SELL_BALANCE_FETCH_FAILED", mint=tr.mint,
                    detail=f"tag={tag} rpc_uncertain=True", severity="error",
                )
                return False, None

            if wallet_raw == 0:
                # Guard: same propagation-delay protection as FAST_STOP path.
                # RPC token-account balance may still be zero for 3-8s after buy confirmation.
                _hold_s = time.time() - float(tr.entry_ts or tr.ts or time.time())
                if _hold_s < 5.0:
                    log.warning(
                        "[LIVE_SELL] wallet_raw=0 mint=%s tag=%s hold_s=%.1f — "
                        "position too young; waiting 3s for RPC propagation before ghost close",
                        tr.mint, tag, _hold_s,
                    )
                    append_telemetry_event(
                        "LIVE_SELL_BALANCE_PROPAGATION_WAIT", mint=tr.mint,
                        detail=f"tag={tag} hold_s={_hold_s:.1f} wallet_raw=0 — retrying after 3s",
                        severity="warn",
                    )
                    await asyncio.sleep(3.0)
                    wallet_raw = await self._fetch_live_token_balance(tr.mint)
                    if wallet_raw == self._BALANCE_FETCH_ERROR:
                        log.error(
                            "[LIVE_SELL] propagation-retry fetch failed mint=%s — blocking paper close",
                            tr.mint,
                        )
                        return False, None
                    if wallet_raw > 0:
                        log.warning(
                            "[LIVE_SELL] propagation-retry confirmed mint=%s wallet_raw=%d — proceeding with sell",
                            tr.mint, wallet_raw,
                        )
                        # Fall through to normal sell path below
                    else:
                        log.warning(
                            "[LIVE_SELL] wallet_balance=0 after propagation-retry mint=%s tag=%s — confirmed ghost, paper close allowed",
                            tr.mint, tag,
                        )
                        append_telemetry_event(
                            "LIVE_SELL_GHOST_CLOSE", mint=tr.mint,
                            detail=f"tag={tag} wallet_raw=0 confirmed_zero=True after_propagation_retry=True", severity="warn",
                        )
                        return True, 0.0
                else:
                    log.warning(
                        "[LIVE_SELL] wallet_balance=0 mint=%s tag=%s hold_s=%.1f — confirmed ghost, paper close allowed",
                        tr.mint, tag, _hold_s,
                    )
                    append_telemetry_event(
                        "LIVE_SELL_GHOST_CLOSE", mint=tr.mint,
                        detail=f"tag={tag} wallet_raw=0 confirmed_zero=True", severity="warn",
                    )
                    return True, 0.0

        # Fraction of remaining position being sold
        frac_of_remaining = (qty_tracked / qty_before) if qty_before > 0 else 1.0
        if frac_of_remaining >= 0.999:
            raw_to_sell = wallet_raw          # Full close → sell entire wallet balance
        else:
            raw_to_sell = max(1, int(wallet_raw * frac_of_remaining))

        sol_usd = await self._sol_usd()

        tr.sell_attempt_ts = time.time()
        trigger_to_attempt_ms = ((tr.sell_attempt_ts - float(tr.stop_trigger_ts or tr.sell_attempt_ts)) * 1000.0) if tr.stop_trigger_ts else 0.0
        log.warning(
            "[LIVE_SELL] submitting sell mint=%s tag=%s wallet_raw=%d raw_to_sell=%d frac=%.4f trigger_to_attempt_ms=%.1f",
            tr.mint, tag, wallet_raw, raw_to_sell, frac_of_remaining, trigger_to_attempt_ms,
        )
        append_telemetry_event(
            "LIVE_SELL_ATTEMPT", mint=tr.mint,
            detail=f"tag={tag} raw={raw_to_sell} frac={frac_of_remaining:.4f} trigger_to_attempt_ms={trigger_to_attempt_ms:.1f}", severity="info",
        )

        # Stop/emergency exits need high slippage tolerance to guarantee execution on dumps.
        # TP exits get tighter slippage to preserve profit margin.
        sell_slippage = LIVE_SELL_MAX_SLIPPAGE_PCT if is_stop_exit else LIVE_SELL_TP_SLIPPAGE_PCT
        success, sig, err_msg, out_lamports = await execute_live_sell(
            mint=tr.mint,
            token_amount=raw_to_sell,
            sol_price_usd=sol_usd,
            max_slippage_pct=sell_slippage,
        )

        if success:
            actual_proceeds_usd: Optional[float] = None
            if out_lamports is not None and out_lamports > 0:
                actual_proceeds_usd = (out_lamports / 1_000_000_000) * sol_usd
                # Back-compute actual execution price and update mark so paper accounting is accurate.
                # raw_to_sell is in BASE UNITS (whole_tokens × 10^6 for 6-decimal SPL mints).
                # Dividing by base units gives price per base unit — 10^6× too small.
                # Convert to whole tokens first before dividing.
                _TOKEN_DECIMALS = 6  # pump.fun SPL standard (hardcoded, matches buy path)
                _whole_tokens_sold = raw_to_sell / (10 ** _TOKEN_DECIMALS)
                actual_price = actual_proceeds_usd / _whole_tokens_sold if _whole_tokens_sold > 0 else 0.0
                tr.current_px = actual_price
                tr.last_price = actual_price
            elif out_lamports == 0:
                # P2.9: TX succeeded but Jupiter reports zero SOL received. Tokens left the
                # wallet with no proceeds — treating this as a normal sell would book fake PnL
                # off the stale current_px fallback. Force ghost-close semantics
                # (proceeds=0 → pnl=0, qty_closed credited, no capital movement) instead.
                log.error(
                    "[LIVE_SELL] SUCCESS but out_lamports=0 mint=%s tag=%s sig=%s — zero-proceeds anomaly, ghost-close",
                    tr.mint, tag, (sig or "")[:20],
                )
                append_telemetry_event(
                    "LIVE_SELL_ZERO_PROCEEDS", mint=tr.mint,
                    detail=f"tag={tag} sig={(sig or '')[:20]} raw_to_sell={raw_to_sell} — TX succeeded but outAmount=0",
                    severity="critical",
                )
                actual_proceeds_usd = 0.0
            tr.sell_success_ts = time.time()
            trigger_to_success_ms = ((tr.sell_success_ts - float(tr.stop_trigger_ts or tr.sell_success_ts)) * 1000.0) if tr.stop_trigger_ts else 0.0
            log.warning(
                "[LIVE_SELL] SUCCESS mint=%s tag=%s tx=%s out_lamports=%s proceeds_usd=%s trigger_to_success_ms=%.1f",
                tr.mint, tag, sig, out_lamports, actual_proceeds_usd, trigger_to_success_ms,
            )
            append_telemetry_event(
                "LIVE_SELL_SUCCESS", mint=tr.mint,
                detail=f"tag={tag} tx={(sig or '')[:20]} out_lamports={out_lamports} trigger_to_success_ms={trigger_to_success_ms:.1f}",
                severity="info",
            )
            # Fire-and-forget post-sell balance check (3s delay, no added latency to sell path).
            # Confirms wallet went flat; emits POST_SELL_RESIDUAL_BALANCE if dust remains.
            if frac_of_remaining >= 0.999:
                asyncio.create_task(self._post_sell_balance_verify(tr.mint, tag))
            return True, actual_proceeds_usd
        else:
            log.error(
                "[LIVE_SELL] FAILED mint=%s tag=%s err=%s — paper close BLOCKED",
                tr.mint, tag, err_msg,
            )
            append_telemetry_event(
                "LIVE_SELL_FAILED", mint=tr.mint,
                detail=f"tag={tag} err={err_msg}", severity="error",
            )
            # ── Slippage rejection retry (Custom: 6001) ──────────────────────
            # TX was rejected on-chain with "InstructionError Custom: 6001" —
            # the market moved beyond the quote's slippage tolerance between
            # quote and execution. The TX definitely did NOT execute: the position
            # is still open. Re-fetch SOL price and retry once with 15% slippage.
            # This is distinct from tx_verification_timeout (TX lost/uncertain).
            _slippage_retry_done = False
            if "6001" in (err_msg or ""):
                _slippage_retry_done = True
                sol_usd = await self._sol_usd()  # re-fetch: SOL price may have moved
                log.warning(
                    "[LIVE_SELL] slippage_reject 6001 mint=%s tag=%s — refreshing price, retrying at 15%% slippage",
                    tr.mint, tag,
                )
                append_telemetry_event(
                    "SLIPPAGE_REJECT_RETRY", mint=tr.mint,
                    detail=f"tag={tag} first_sig={(sig or '')[:20]} first_err={err_msg} — retrying at 15% slippage",
                    severity="warn",
                )
                _sr_success, _sr_sig, _sr_err, _sr_lamports = await execute_live_sell(
                    mint=tr.mint,
                    token_amount=raw_to_sell,
                    sol_price_usd=sol_usd,
                    max_slippage_pct=0.15,
                )
                if _sr_success:
                    _sr_proceeds: Optional[float] = None
                    if _sr_lamports is not None and _sr_lamports > 0:
                        _sr_proceeds = (_sr_lamports / 1_000_000_000) * sol_usd
                        _whole_sr = raw_to_sell / (10 ** 6)
                        _sr_price = _sr_proceeds / _whole_sr if _whole_sr > 0 else 0.0
                        tr.current_px = _sr_price
                        tr.last_price = _sr_price
                    elif _sr_lamports == 0:
                        # P2.9: same zero-proceeds anomaly guard as first attempt.
                        log.error(
                            "[LIVE_SELL] slippage_reject retry SUCCESS but out_lamports=0 mint=%s tag=%s — ghost-close",
                            tr.mint, tag,
                        )
                        append_telemetry_event(
                            "LIVE_SELL_ZERO_PROCEEDS", mint=tr.mint,
                            detail=f"tag={tag} sig={(_sr_sig or '')[:20]} retry=slippage — outAmount=0",
                            severity="critical",
                        )
                        _sr_proceeds = 0.0
                    tr.sell_success_ts = time.time()
                    log.warning(
                        "[LIVE_SELL] slippage_reject retry SUCCESS mint=%s tag=%s tx=%s out_lamports=%s proceeds_usd=%s",
                        tr.mint, tag, _sr_sig, _sr_lamports, _sr_proceeds,
                    )
                    append_telemetry_event(
                        "SLIPPAGE_REJECT_RETRY_SUCCESS", mint=tr.mint,
                        detail=f"tag={tag} tx={(_sr_sig or '')[:20]} out_lamports={_sr_lamports} proceeds_usd={_sr_proceeds}",
                        severity="info",
                    )
                    return True, _sr_proceeds
                log.error(
                    "[LIVE_SELL] slippage_reject retry also failed mint=%s tag=%s err=%s",
                    tr.mint, tag, _sr_err,
                )
                append_telemetry_event(
                    "SLIPPAGE_REJECT_RETRY_FAILED", mint=tr.mint,
                    detail=f"tag={tag} retry_err={_sr_err}", severity="error",
                )
                err_msg = _sr_err  # update so stop-exit path below sees the actual failure
            # ── TP sell verification timeout retry ────────────────────────────
            # TP sell verification timeout: retry once immediately at current market price.
            # The original TX timed out — most likely it did not confirm (Solana confirms in
            # < 1s normally; timeout implies dropped TX). Get a fresh Jupiter quote (routes
            # through Raydium, not the stale/inflated price the first attempt used) and retry
            # with max slippage to guarantee execution. This prevents the TP stage being
            # consumed with no actual sell occurring.
            if err_msg == "tx_verification_timeout" and not is_stop_exit and not _slippage_retry_done:
                log.warning(
                    "[LIVE_SELL] TP sell tx_verification_timeout mint=%s tag=%s — retrying once at market",
                    tr.mint, tag,
                )
                append_telemetry_event(
                    "TP_SELL_VERIFICATION_TIMEOUT_RETRY", mint=tr.mint,
                    detail=f"tag={tag} first_sig={(sig or '')[:20]} — retrying at market",
                    severity="warn",
                )
                retry_success, retry_sig, retry_err, retry_lamports = await execute_live_sell(
                    mint=tr.mint,
                    token_amount=raw_to_sell,
                    sol_price_usd=sol_usd,
                    max_slippage_pct=LIVE_SELL_MAX_SLIPPAGE_PCT,  # wider tolerance for retry
                )
                if retry_success:
                    retry_proceeds: Optional[float] = None
                    if retry_lamports is not None and retry_lamports > 0:
                        retry_proceeds = (retry_lamports / 1_000_000_000) * sol_usd
                        _TOKEN_DECIMALS_RETRY = 6
                        _whole_retry = raw_to_sell / (10 ** _TOKEN_DECIMALS_RETRY)
                        retry_price = retry_proceeds / _whole_retry if _whole_retry > 0 else 0.0
                        tr.current_px = retry_price
                        tr.last_price = retry_price
                    elif retry_lamports == 0:
                        # P2.9: zero-proceeds anomaly on TP-timeout retry.
                        log.error(
                            "[LIVE_SELL] TP retry SUCCESS but out_lamports=0 mint=%s tag=%s — ghost-close",
                            tr.mint, tag,
                        )
                        append_telemetry_event(
                            "LIVE_SELL_ZERO_PROCEEDS", mint=tr.mint,
                            detail=f"tag={tag} sig={(retry_sig or '')[:20]} retry=tp_timeout — outAmount=0",
                            severity="critical",
                        )
                        retry_proceeds = 0.0
                    tr.sell_success_ts = time.time()
                    log.warning(
                        "[LIVE_SELL] TP retry SUCCESS mint=%s tag=%s tx=%s out_lamports=%s proceeds_usd=%s",
                        tr.mint, tag, retry_sig, retry_lamports, retry_proceeds,
                    )
                    append_telemetry_event(
                        "TP_SELL_RETRY_SUCCESS", mint=tr.mint,
                        detail=f"tag={tag} tx={(retry_sig or '')[:20]} out_lamports={retry_lamports} proceeds_usd={retry_proceeds}",
                        severity="info",
                    )
                    return True, retry_proceeds
                log.error(
                    "[LIVE_SELL] TP retry also failed mint=%s tag=%s err=%s — sell blocked",
                    tr.mint, tag, retry_err,
                )
                append_telemetry_event(
                    "TP_SELL_RETRY_FAILED", mint=tr.mint,
                    detail=f"tag={tag} retry_err={retry_err}", severity="error",
                )
            if is_stop_exit:
                if err_msg == "tx_verification_timeout":
                    # TX may have confirmed on-chain — do NOT declare unresolved yet.
                    # Setting forced_exit_unresolved now would spam retry sells for a
                    # TX that may have already succeeded. Poll the wallet for 60s first.
                    tr.sell_pending_verify_ts = time.time()
                    asyncio.create_task(self._sell_verify_poll(tr, raw_to_sell, tag))
                    append_telemetry_event(
                        "SELL_VERIFY_POLL_STARTED", mint=tr.mint,
                        detail=f"tag={tag} tx={(sig or '')[:20]} — TX timeout; polling wallet every 5s for 60s",
                        severity="warn",
                    )
                else:
                    tr.forced_exit_unresolved = True
                    append_telemetry_event(
                        "FORCED_EXIT_UNQUOTEABLE", mint=tr.mint,
                        detail=f"tag={tag} err={err_msg} — stop exit sell failed, position remains open",
                        severity="critical",
                    )
                    # HARD_CAP ghost-close: if sells keep failing (illiquid rug), give up
                    # after 4 consecutive failures and record the loss as a ghost close.
                    # Threshold is 4 (not 10) because forced_exit exhausts at
                    # FORCED_EXIT_MAX_ATTEMPTS=5 after ~100s — the ghost close must fire
                    # before that path takes over (~80s at 20s cooldown × 4 attempts).
                    if "HARD_CAP" in tag:
                        tr.hcap_sell_fail_count = int(tr.hcap_sell_fail_count or 0) + 1
                        log.error(
                            "[HARD_CAP_SELL_FAIL] mint=%s tag=%s fail=%d/4 err=%s",
                            tr.mint, tag, tr.hcap_sell_fail_count, err_msg,
                        )
                        if tr.hcap_sell_fail_count >= 4:
                            log.error(
                                "[HARD_CAP_GHOST_CLOSE] mint=%s — %d consecutive sell failures, "
                                "recording loss and ghost closing (proceeds=0)",
                                tr.mint, tr.hcap_sell_fail_count,
                            )
                            append_telemetry_event(
                                "HARD_CAP_GHOST_CLOSE", mint=tr.mint,
                                detail=f"tag={tag} fail_count={tr.hcap_sell_fail_count} err={err_msg} — ghost closing after 4 sell failures",
                                severity="critical",
                            )
                            tr.forced_exit_unresolved = False
                            self._finalize_trade_close(tr, "HARD_CAP_EXIT")
            return False, None

    async def _post_sell_balance_verify(self, mint: str, tag: str):
        """
        Fire-and-forget: re-fetch wallet balance 3s after a full-close sell success.
        Emits POST_SELL_RESIDUAL_BALANCE (warn) if tokens remain, or
        POST_SELL_BALANCE_CONFIRMED_ZERO (info) when flat.
        Adds zero latency to the sell path.
        """
        await asyncio.sleep(3.0)
        try:
            residual = await self._fetch_live_token_balance(mint)
            if residual == self._BALANCE_FETCH_ERROR:
                append_telemetry_event(
                    "POST_SELL_BALANCE_FETCH_FAILED", mint=mint,
                    detail=f"tag={tag} — could not verify wallet flat after sell",
                    severity="warn",
                )
            elif residual > 0:
                append_telemetry_event(
                    "POST_SELL_RESIDUAL_BALANCE", mint=mint,
                    detail=f"tag={tag} residual_raw={residual} — token still in wallet 3s post-sell (partial fill or dust)",
                    severity="warn",
                )
            else:
                append_telemetry_event(
                    "POST_SELL_BALANCE_CONFIRMED_ZERO", mint=mint,
                    detail=f"tag={tag} — wallet confirmed flat 3s post-sell",
                    severity="info",
                )
        except Exception as exc:
            log.warning("[POST_SELL_VERIFY] mint=%s tag=%s error=%s", mint, tag, exc)

    async def _sell_verify_poll(self, tr: "PaperTrade", raw_to_sell: int, tag: str):
        """
        Background task: after a stop-exit TX verification timeout, poll the wallet
        every 5s for up to 60s to determine whether the sell TX actually confirmed.

        Outcome:
          wallet=0  → sell confirmed on-chain. Finalize as ghost-close (zero proceeds,
                      no PnL mutation) then ghost-reconcile to remove from inventory.
          wallet>0 after 60s → escalate: set forced_exit_unresolved so the existing
                      forced-exit retry loop retries the sell.
        """
        _POLL_INTERVAL_S = 5.0
        _MAX_POLLS = 12  # 12 × 5s = 60s total
        mint = tr.mint

        for poll_num in range(1, _MAX_POLLS + 1):
            await asyncio.sleep(_POLL_INTERVAL_S)

            # Guard: another code path already resolved this position
            if tr.closed or float(tr.sell_pending_verify_ts or 0.0) == 0.0:
                log.info(
                    "[SELL_VERIFY_POLL] mint=%s poll=%d — position already resolved, aborting poll",
                    mint, poll_num,
                )
                return

            wallet_raw = await self._fetch_live_token_balance(mint)

            if wallet_raw == self._BALANCE_FETCH_ERROR:
                log.warning(
                    "[SELL_VERIFY_POLL] mint=%s poll=%d/%d — balance fetch error, retrying",
                    mint, poll_num, _MAX_POLLS,
                )
                continue

            log.info(
                "[SELL_VERIFY_POLL] mint=%s poll=%d/%d wallet_raw=%d",
                mint, poll_num, _MAX_POLLS, wallet_raw,
            )

            if wallet_raw == 0:
                # Sell TX confirmed on-chain: tokens gone from wallet.
                # Finalize as ghost close (zero proceeds, no PnL) then terminalize.
                # P2.6: acquire per-mint sell lock for the entire finalization so a
                # concurrent forced-exit sell can't start between our state mutations
                # and create overlapping PnL / sell TXs.
                async with self._get_sell_lock(mint):
                    log.warning(
                        "[SELL_VERIFY_POLL] mint=%s poll=%d — wallet=0 confirmed; sell TX succeeded (was verification timeout)",
                        mint, poll_num,
                    )
                    append_telemetry_event(
                        "SELL_VERIFY_POLL_CONFIRMED_ZERO", mint=mint,
                        detail=f"tag={tag} poll={poll_num}/{_MAX_POLLS} raw_to_sell={raw_to_sell} — TX confirmed via wallet poll",
                        severity="warn",
                    )
                    tr.position_truth_state = "GHOST_CLOSE_PENDING"
                    # Mirror the ghost-close accounting path from _sell_fraction:
                    # qty, basis, fees all fully realized; proceeds=0; pnl=0
                    tr.qty_closed = float(tr.qty_total or 0.0)
                    tr.qty_remaining = 0.0
                    tr.remaining_basis_usd = 0.0
                    tr.realized_basis_usd = float(tr.cost_basis_total_usd or tr.size_usd)
                    tr.realized_fees_usd = float(tr.fees_usd or 0.0)
                    tr.status = "closed"
                    tr.closed = True
                    tr.close_reason = tag
                    if not tr.exit_ts:
                        tr.exit_ts = time.time()
                    if not tr.exit_price:
                        tr.exit_price = float(tr.current_px or tr.entry_px or 0.0)
                    # Clear sell_pending_verify_ts LAST so monitor_positions_loop can't
                    # observe "verify done" before it can observe "closed=True".
                    tr.sell_pending_verify_ts = 0.0
                    # Write trade_close to history so _ghost_reconciliation_terminalize
                    # finds a confirmed close record and safely pops the position.
                    self._finalize_trade_close(tr, tag)
                    self._ghost_reconciliation_terminalize(tr)
                    self._state_sync()
                return

            # wallet_raw > 0 — sell has not yet confirmed; keep polling

        # All polls exhausted: wallet still shows tokens after 60s.
        # Escalate to the existing forced-exit retry loop.
        log.error(
            "[SELL_VERIFY_POLL] mint=%s — %d polls (%.0fs) exhausted, wallet non-zero — escalating to forced_exit_unresolved",
            mint, _MAX_POLLS, _POLL_INTERVAL_S * _MAX_POLLS,
        )
        append_telemetry_event(
            "SELL_VERIFY_POLL_TIMEOUT_ESCALATE", mint=mint,
            detail=f"tag={tag} polls={_MAX_POLLS} total_s={_POLL_INTERVAL_S * _MAX_POLLS:.0f} — wallet non-zero, setting forced_exit_unresolved",
            severity="critical",
        )
        # P2.6: set forced_exit_unresolved BEFORE clearing sell_pending_verify_ts so
        # monitor_positions_loop can't observe "verify done + not forced" (which would
        # let HARD_CAP or trail re-trigger a brand-new sell while the original TX is
        # still in flight). Write under the sell lock for a consistent transition.
        async with self._get_sell_lock(mint):
            tr.forced_exit_unresolved = True
            tr.sell_pending_verify_ts = 0.0
            self._state_sync()

    async def _sell_fraction(self, tr: PaperTrade, frac_of_total: float, tag: str, stage_flag: Optional[str] = None):
        # P2.2: serialize sells per-mint. The entire quote→sign→submit→accounting
        # path runs under the lock so concurrent exits (TP stage + HARD_CAP) can't
        # over-sell qty or commit inconsistent accounting.
        # P2.4: if stage_flag is provided (e.g. "stage1_done"), check-and-set it
        # INSIDE the lock so two concurrent TP evaluations can't both pass and
        # double-sell the same ladder step.
        async with self._get_sell_lock(tr.mint):
            if stage_flag is not None:
                if bool(getattr(tr, stage_flag, False)):
                    return  # another coroutine already fired this stage
                setattr(tr, stage_flag, True)
            qty_before = float(tr.qty_remaining or 0.0)
            qty = min(qty_before, float(tr.qty_total or 0.0) * frac_of_total)
            if qty <= 0 or qty_before <= 0:
                return

            # ── LIVE SELL GATE ──────────────────────────────────────────────────
            proceeds_override: Optional[float] = None
            if self.execution_mode == "live":
                sold_ok, proceeds_override = await self._live_sell_gate(tr, qty, qty_before, tag)
                if not sold_ok:
                    return  # TX failed — do not record paper close
            # ── END LIVE SELL GATE ──────────────────────────────────────────────

            now_ts = time.time()
            if proceeds_override is not None:
                proceeds = proceeds_override
            else:
                # Paper proceeds: ask Jupiter what we'd actually receive selling
                # `qty` tokens right now. Embeds real swap impact at the actual
                # sell size — without this, paper records proceeds = qty ×
                # trigger_price, which omits the 3-15% fill decay that hits live
                # TRAIL/RAPID exits. Falls back to current_px on quote failure.
                _exec_px: Optional[float] = None
                _qty_int = int(qty)
                if _qty_int > 0:
                    try:
                        _exec_px = await self._executable_mark_price(tr.mint, _qty_int)
                    except Exception as _exc:
                        log.warning(
                            "[PAPER_SELL] executable quote raised mint=%s tag=%s: %s",
                            tr.mint, tag, _exc,
                        )

                _entry_px_floor = float(tr.entry_px or 0.0)
                _trigger_px = float(tr.current_px or 0.0)

                if _exec_px is not None and _exec_px > 0:
                    proceeds = qty * _exec_px
                    _delta_pct = (
                        100.0 * (_exec_px / _trigger_px - 1.0)
                        if _trigger_px > 0 else 0.0
                    )
                    log.info(
                        "[PAPER_SELL] proceeds=%.6f qty=%.4f exec_px=%.10g "
                        "trigger_px=%.10g delta_pct=%.2f mint=%s tag=%s SOURCE=jupiter_quote",
                        proceeds, qty, _exec_px, _trigger_px, _delta_pct, tr.mint, tag,
                    )
                else:
                    _fallback_px = _trigger_px
                    # Guard: if current_px is missing or suspiciously near-zero (< 0.1% of entry_px),
                    # it was likely corrupted by the base-unit division bug in _live_sell_gate after
                    # a prior sell (actual_price = proceeds_usd / raw_to_sell_in_base_units ≈ 0).
                    # Use entry_px as the floor so proceeds are not catastrophically under-stated.
                    if _fallback_px <= 0 or (_entry_px_floor > 0 and _fallback_px < _entry_px_floor * 0.001):
                        log.warning(
                            "[SELL_FRACTION] current_px=%.10g is near-zero vs entry_px=%.10g mint=%s tag=%s — "
                            "using entry_px as proceeds floor to prevent -106%% ROI",
                            _fallback_px, _entry_px_floor, tr.mint, tag,
                        )
                        append_telemetry_event(
                            "SELL_PROCEEDS_FLOOR_APPLIED", mint=tr.mint,
                            detail=f"tag={tag} current_px={_fallback_px:.10g} entry_px={_entry_px_floor:.10g}",
                            severity="warn",
                        )
                        _fallback_px = _entry_px_floor
                    proceeds = qty * _fallback_px
                    log.info(
                        "[PAPER_SELL] proceeds=%.6f qty=%.4f fallback_px=%.10g mint=%s tag=%s "
                        "SOURCE=trigger_px (jupiter_unavailable)",
                        proceeds, qty, _fallback_px, tr.mint, tag,
                    )
            basis_sold = float(tr.remaining_basis_usd or 0.0) * (qty / qty_before)
            remaining_fee_reserve = max(0.0, float(tr.fees_usd or 0.0) - float(tr.realized_fees_usd or 0.0))
            fee_sold = remaining_fee_reserve * (qty / qty_before)

            # Ghost close: _live_sell_gate returned proceeds_override=0.0 because wallet_raw=0
            # at sell time — no TX was submitted, sell_attempt_ts was never set. The true
            # position outcome is unresolved (tokens may not have settled). Do NOT charge the
            # cost_basis as a realized loss; that would permanently debit -$0.53 from
            # realized_pnl / canonical_realized_usd / history even though no sell occurred.
            # Ghost reconciliation terminalize handles cleanup without PnL mutation.
            _is_ghost_close = self.execution_mode == "live" and proceeds_override == 0.0
            if _is_ghost_close:
                tr.position_truth_state = "GHOST_CLOSE_PENDING"
            pnl = (proceeds - basis_sold - fee_sold) if not _is_ghost_close else 0.0

            tr.realized_pnl += pnl
            tr.realized_basis_usd += basis_sold
            tr.realized_fees_usd += fee_sold
            tr.remaining_basis_usd = max(0.0, float(tr.remaining_basis_usd or 0.0) - basis_sold)
            tr.qty_remaining = max(0.0, qty_before - qty)
            tr.qty_closed = max(0.0, float(tr.qty_total or 0.0) - float(tr.qty_remaining or 0.0))
            tr.proceeds_usd += proceeds

            if tr.qty_remaining <= 1e-9:
                tr.qty_remaining = 0.0
                tr.remaining_basis_usd = 0.0
                tr.realized_basis_usd = float(tr.cost_basis_total_usd or tr.size_usd)
                tr.realized_fees_usd = float(tr.fees_usd or 0.0)
                tr.status = "closed"
                tr.closed = True
                tr.close_reason = tag
                if not tr.exit_ts:
                    tr.exit_ts = now_ts
                if not tr.exit_price:
                    tr.exit_price = tr.current_px
            else:
                tr.status = "partial_exit"

            _sf_cost = float(tr.cost_basis_total_usd or tr.size_usd)
            _sf_proceeds = float(tr.proceeds_usd or 0.0)
            if _sf_proceeds > 0 and _sf_cost > 0:
                tr.realized_roi_pct = (_sf_proceeds / _sf_cost - 1.0) * 100.0
            elif not _is_ghost_close and _sf_cost > 0:
                # Skip pnl/cost fallback for ghost closes — proceeds=0 yields misleading
                # sub-100% ROI. Leave realized_roi_pct at its current value (unresolved).
                tr.realized_roi_pct = (float(tr.realized_pnl or 0.0) / _sf_cost) * 100.0
            self.budget += proceeds
            self._bump_canonical_realized_usd(pnl)  # 0.0 for ghost closes — no capital impact
            append_history({"type": "partial_realize", "ts": int(now_ts), "mint": tr.mint, "decision_id": tr.decision_id or "", "tag": tag, "pnl_usd": round(pnl, 4), "qty_closed": round(tr.qty_closed, 6), "qty_remaining": round(tr.qty_remaining, 6), "proceeds_usd": round(tr.proceeds_usd, 6), "fees_usd": round(tr.fees_usd, 6), "realized_basis_usd": round(tr.realized_basis_usd, 6), "remaining_basis_usd": round(tr.remaining_basis_usd, 6), "realized_fees_usd": round(tr.realized_fees_usd, 6)})
            if tr.qty_remaining <= 1e-9:
                emitted = self._ensure_trade_close_integrity(tr, tag)
                if not emitted and not tr.canonical_close_emitted:
                    append_telemetry_event("ZERO_QTY_WITHOUT_CANONICAL_CLOSE", mint=tr.mint, detail=f"tag={tag}", severity="error")
            await tg_send(SNIPER_TG_TOKEN, SNIPER_TG_CHAT_ID, f"{tag} {tr.mint}: realized ${pnl:.2f}, remaining {tr.qty_remaining:.2f}")

    def _operator_controls(self) -> dict:
        st = read_state()
        controls = dict(st.get("operator_controls") or {})
        controls.setdefault("disable_new_entries", False)
        controls.setdefault("default_downside_stop_pct", DEFAULT_DOWNSIDE_STOP_PCT)
        controls.setdefault("absolute_failsafe_stop_pct", ABSOLUTE_FAILSAFE_STOP_PCT)
        return controls

    def _reset_session_state(self, mode: str, ts_now: int) -> dict:
        current_budget = float(self.budget)
        current_paper_rows = self._paper_positions_snapshot()
        current_visible_open_rows = self._capital_open_position_rows(current_paper_rows, include_stale=False)
        current_capital_rows = self._capital_open_position_rows(current_paper_rows, include_stale=True)
        current_open_unrealized = round(sum(float(r.get("unrealized_pnl_usd", 0.0) or 0.0) for r in current_capital_rows), 6)
        st_before = read_state()
        self._ensure_canonical_capital_fields(st_before, rows=current_paper_rows, cash_budget=current_budget)
        canonical_start_capital_usd = round(float(st_before.get("canonical_start_capital_usd") or 0.0), 6)
        canonical_realized_usd = round(float(st_before.get("canonical_realized_usd") or 0.0), 6)
        current_canonical_capital_usd = round(canonical_start_capital_usd + canonical_realized_usd + current_open_unrealized, 6)
        new_session_window_start_budget = 100.0 if mode == "baseline_100" else current_canonical_capital_usd
        day = utc_day()
        lifetime = None
        preserved_moonbags = []
        if mode != "baseline_100":
            for tr in self._trades_snapshot():
                if tr.closed:
                    continue
                if tr.stage2_done and tr.qty_remaining > 0:
                    preserved_moonbags.append({
                        "mint": tr.mint,
                        "qty_remaining": round(tr.qty_remaining, 4),
                        "entry_px": tr.entry_px,
                        "current_px": round(tr.current_px, 6),
                        "stop_px": round(tr.stop_px, 6),
                    })

        def _mut(base):
            nonlocal lifetime
            phase = dict(base.get("analytics_phase5c") or {})
            lifetime = phase.get("lifetime", {})
            formulas = phase.get("formulas", {})
            daily_metrics = default_metrics(day)
            daily_metrics["session_start_ts"] = ts_now
            current_open_block = {
                "count": 0,
                "unrealized_pnl_usd": 0.0,
                "avg_unrealized_roi_pct": 0.0,
                "avg_mfe_pct": 0.0,
                "avg_mae_pct": 0.0,
            }
            base["pnl_usd"] = 0.0
            base["roi_pct"] = 0.0
            base["session_start_budget_usd"] = round(new_session_window_start_budget, 6)
            base["session_window_budget_usd"] = round(new_session_window_start_budget, 6)
            base["paper_spent_usd"] = 0.0
            base["paper_positions"] = [] if mode == "baseline_100" else current_paper_rows
            base["active_positions"] = [] if mode == "baseline_100" else current_visible_open_rows
            base["active_moonbags"] = [] if mode == "baseline_100" else preserved_moonbags
            base["last_trade"] = None
            base["last_entry"] = None
            base["last_signal"] = None
            base["top_catch_24h"] = None
            base["closed_trades_24h"] = 0
            base["recent_activity"] = []
            base["telemetry_events"] = []
            base["daily_metrics"] = daily_metrics
            base["rolling_24h"] = {
                "window_seconds": 86400,
                "execution_attempts": 0,
                "closed_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate_pct": 0.0,
                "avg_decision_latency_ms": 0.0,
            }
            base["analytics_phase5c"] = {
                "formulas": formulas,
                "today_utc": zero_phase5c_block(),
                "session": zero_phase5c_block(),
                "lifetime": lifetime,
                "current_open": current_open_block,
            }
            base["process_start_ts"] = ts_now
            base["process_uptime_seconds"] = 0
            if mode == "baseline_100":
                base["paper_budget_left"] = 100.0
                base["paper_spent_usd"] = 0.0
                base["canonical_start_capital_usd"] = 100.0
                base["canonical_realized_usd"] = 0.0
                base["canonical_unrealized_usd"] = 0.0
                base["canonical_capital_usd"] = 100.0
                base["sim_budget_usd"] = 100.0
            else:
                base["paper_budget_left"] = round(current_budget, 2)
                base["canonical_start_capital_usd"] = round(canonical_start_capital_usd, 6)
                base["canonical_realized_usd"] = round(canonical_realized_usd, 6)
                base["canonical_unrealized_usd"] = round(current_open_unrealized, 6)
                base["canonical_capital_usd"] = round(current_canonical_capital_usd, 6)
                base["sim_budget_usd"] = round(current_canonical_capital_usd, 6)
            base["operator_last_command"] = {
                "ts": ts_now,
                "action": "reset_session",
                "mode": mode,
                "sim_budget_usd": round(base.get("sim_budget_usd", new_session_window_start_budget), 2),
                "pnl_usd": 0.0,
                "roi_pct": 0.0,
                "active_positions": 0 if mode == "baseline_100" else len(current_visible_open_rows),
                "paper_positions": 0 if mode == "baseline_100" else len(current_paper_rows),
            }
            # Zero all session-scoped in-process diagnostic counters in the state
            # file so the HUD reflects a clean slate immediately (not stale values
            # from the previous run's last flush).
            base["entry_funnel"] = {
                "raw_candidates_seen": 0, "qualified_candidates": 0,
                "parsed_successfully": 0, "parse_failed": 0,
                "veto_liquidity": 0, "veto_price": 0, "veto_dependency": 0, "veto_other": 0,
                "entry_eligible": 0, "execution_attempted": 0,
                "execution_succeeded": 0, "execution_failed": 0,
                "migration_retry_enqueued": 0, "migration_retry_attempts_total": 0,
                "migration_retry_price_found": 0, "migration_retry_exhausted_count": 0,
                "migration_retry_missing_ref_price_count": 0,
                "delayed_entry_enqueued": 0, "delayed_entry_price_alive": 0,
                "delayed_entry_dump_discarded": 0,
                "migration_retry_gated_live_tx": 0,
                "migration_retry_gated_trading_disabled": 0,
                "reserve_miss_total": 0,
                "reserve_miss_suppressed": 0,
                "reserve_miss_unique_mints": 0,
            }
            base["terminal_blockers_summary"] = {
                "liquidity_unlocked": 0, "rpc_timeout": 0, "parse_low_confidence": 0,
                "excluded_mint": 0, "dependency_unavailable": 0, "queue_stale": 0,
                "execution_blocked": 0, "execution_failed": 0, "duplicate": 0, "other": 0,
            }
            base["execution_audit"] = {
                "execution_attempts": 0, "execution_prechecks_passed": 0, "execution_blocked": 0,
                "live_submit_attempts": 0, "live_submit_success": 0, "live_submit_failures": 0,
                "live_tx_verified_success": 0, "live_tx_verified_failure": 0,
                "live_tx_verification_timeout": 0,
                "last_tx_verify_state": None, "last_tx_verify_err": None,
                "last_failure_reason": None, "last_tx_signature": None,
                "avg_submit_latency_ms": 0.0,
            }
            base["candidate_timing"] = {
                "avg_time_seen_to_parse_ms": 0.0,
                "avg_time_parse_to_decision_ms": 0.0,
                "avg_time_decision_to_execution_ms": 0.0,
                "stale_before_parse": 0, "stale_before_decision": 0,
                "stale_before_execution": 0, "total_samples": 0,
            }
            base["rpc_health"] = {
                "endpoints": {}, "current_rpc_url": "",
                "timeout_rate_pct": 0.0, "total_requests": 0, "total_timeouts": 0,
            }

        _locked_state_update(_mut, force_flush=True)
        # Zero in-memory dicts to match the state file — keeps HUD and process
        # counters in sync for the remainder of this process's lifetime.
        self._funnel = {
            "raw_candidates_seen": 0, "qualified_candidates": 0,
            "parsed_successfully": 0, "parse_failed": 0,
            "veto_liquidity": 0, "veto_price": 0, "veto_dependency": 0, "veto_other": 0,
            "entry_eligible": 0, "execution_attempted": 0,
            "execution_succeeded": 0, "execution_failed": 0,
            "migration_retry_enqueued": 0, "migration_retry_attempts_total": 0,
            "migration_retry_price_found": 0, "migration_retry_exhausted_count": 0,
            "migration_retry_missing_ref_price_count": 0,
            "delayed_entry_enqueued": 0, "delayed_entry_price_alive": 0,
            "delayed_entry_dump_discarded": 0,
            "settle_window_rejected": 0,
            "migration_retry_gated_live_tx": 0,
            "migration_retry_gated_trading_disabled": 0,
            "reserve_miss_total": 0,
            "reserve_miss_suppressed": 0,
            "reserve_miss_unique_mints": 0,
        }
        self._reserve_miss_cache = {}
        self._ghost_recon_no_sell_mints = set()
        self._terminal_blockers = {
            "liquidity_unlocked": 0, "rpc_timeout": 0, "parse_low_confidence": 0,
            "excluded_mint": 0, "dependency_unavailable": 0, "queue_stale": 0,
            "execution_blocked": 0, "execution_failed": 0, "duplicate": 0, "other": 0,
        }
        self._exec_audit = {
            "execution_attempts": 0, "execution_prechecks_passed": 0, "execution_blocked": 0,
            "live_submit_attempts": 0, "live_submit_success": 0, "live_submit_failures": 0,
            "live_tx_verified_success": 0, "live_tx_verified_failure": 0,
            "live_tx_verification_timeout": 0,
            "last_tx_verify_state": None, "last_tx_verify_err": None,
            "last_failure_reason": None, "last_tx_signature": None,
            "avg_submit_latency_ms": 0.0, "_submit_latency_sum": 0.0, "_submit_count": 0,
        }
        self._timing = {
            "avg_time_seen_to_parse_ms": 0.0, "avg_time_parse_to_decision_ms": 0.0,
            "avg_time_decision_to_execution_ms": 0.0,
            "stale_before_parse": 0, "stale_before_decision": 0, "stale_before_execution": 0,
            "total_samples": 0,
            "_seen_to_parse_sum": 0.0, "_parse_to_decision_sum": 0.0, "_decision_to_exec_sum": 0.0,
            # P5-1: per-task timing accumulators
            "_decision_samples": 0,
            "_mutable_ms_sum": 0.0, "_price_ms_sum": 0.0, "_rug_ms_sum": 0.0,
            "_holder_ms_sum": 0.0, "_onchain_ms_sum": 0.0,
        }
        self._rpc_health = {"endpoints": {}, "total_requests": 0, "total_timeouts": 0, "total_successes": 0}
        if mode == "baseline_100":
            self.budget = 100.0
            self.trades = {}
        else:
            self.budget = current_budget
        self.start_ts = ts_now
        return {
            "sim_budget_usd": round(100.0 if mode == "baseline_100" else current_canonical_capital_usd, 2),
            "pnl_usd": 0.0,
            "roi_pct": 0.0,
            "active_positions": 0 if mode == "baseline_100" else len(current_visible_open_rows),
            "paper_positions": 0 if mode == "baseline_100" else len(current_paper_rows),
        }

    async def _process_operator_commands(self):
        # P5.1: read new commands from OPERATOR_COMMANDS_FILE (append-only jsonl).
        # Cursor = byte offset of last-processed line. Persist in runtime_state so
        # a restart replays only truly-new commands. HUD never writes state.
        def _read_new_commands():
            new_cmds: list[dict] = []
            if not OPERATOR_COMMANDS_FILE.exists():
                return new_cmds, 0
            base = read_state()
            cursor = int(base.get("operator_cmd_cursor") or 0)
            lock_fh = None
            try:
                OPERATOR_COMMANDS_LOCK.parent.mkdir(parents=True, exist_ok=True)
                lock_fh = open(OPERATOR_COMMANDS_LOCK, "a+")
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
                with open(OPERATOR_COMMANDS_FILE, "r", encoding="utf-8") as f:
                    f.seek(cursor)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            new_cmds.append(json.loads(line))
                        except Exception as e:
                            log.warning("[OPERATOR_CMD] skipped malformed line: %s", e)
                    new_cursor = f.tell()
                return new_cmds, new_cursor
            finally:
                if lock_fh is not None:
                    try:
                        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                        lock_fh.close()
                    except Exception:
                        pass

        commands, new_cursor = await asyncio.to_thread(_read_new_commands)
        if not commands:
            return

        # Advance cursor ONCE, up-front, so a crash mid-processing doesn't
        # silently replay already-acted commands (flatten_all etc. are not
        # idempotent).
        def _advance(base):
            base["operator_cmd_cursor"] = int(new_cursor)
        await asyncio.to_thread(_locked_state_update, _advance, True)

        for cmd in commands:
            action = str(cmd.get("action") or "").strip().lower()
            mint = str(cmd.get("mint") or "").strip()
            fraction = float(cmd.get("fraction") or 0.0)
            ts_now = int(time.time())

            if action == "set_disable_new_entries":
                value = bool(cmd.get("value", True))

                def _set_disable(base):
                    controls = dict(base.get("operator_controls") or {})
                    controls["disable_new_entries"] = value
                    controls.setdefault("default_downside_stop_pct", DEFAULT_DOWNSIDE_STOP_PCT)
                    controls.setdefault("absolute_failsafe_stop_pct", ABSOLUTE_FAILSAFE_STOP_PCT)
                    base["operator_controls"] = controls
                    base["operator_last_command"] = {"ts": ts_now, "action": action, "value": value}

                await asyncio.to_thread(_locked_state_update, _set_disable, True)
                append_recent_activity("DISABLE NEW ENTRIES" if value else "ENABLE NEW ENTRIES", mint="system", live_pnl=0.0, ts=ts_now)
                await tg_send(SNIPER_TG_TOKEN, SNIPER_TG_CHAT_ID, f"Operator control: {'disable' if value else 'enable'} new entries")
                continue

            if action == "flatten_all":
                open_trades = [tr for tr in self._trades_snapshot() if not tr.closed and tr.qty_remaining > 0]
                for tr in open_trades:
                    await self._sell_fraction(tr, 1.0, "MANUAL FLATTEN ALL")
                    if tr.closed:
                        self._finalize_trade_close(tr, "MANUAL FLATTEN ALL")
                append_recent_activity("FLATTEN ALL", mint="system", live_pnl=0.0, ts=ts_now)
                write_state(operator_last_command={"ts": ts_now, "action": action, "count": len(open_trades)})
                await tg_send(SNIPER_TG_TOKEN, SNIPER_TG_CHAT_ID, f"Operator control: flatten all executed on {len(open_trades)} positions")
                continue

            if action == "reset_session":
                mode = str(cmd.get("mode") or "metrics_only").strip().lower()
                if mode not in {"metrics_only", "baseline_100"}:
                    write_state(operator_last_command={"ts": ts_now, "action": action, "status": "invalid_mode", "mode": mode})
                    continue
                summary = self._reset_session_state(mode, ts_now)
                await tg_send(
                    SNIPER_TG_TOKEN,
                    SNIPER_TG_CHAT_ID,
                    f"Operator control: reset session ({mode}) complete • budget ${summary['sim_budget_usd']:.2f} • pnl ${summary['pnl_usd']:.2f} • roi {summary['roi_pct']:.2f}% • open {summary['active_positions']}",
                )
                continue

            if action == "set_pending_execution_mode":
                requested = str(cmd.get("requested") or "").strip().lower()
                if requested not in {"paper", "live"}:
                    write_state(operator_last_command={"ts": ts_now, "action": action, "status": "invalid_mode", "requested": requested})
                    continue

                def _set_pending_mode(base):
                    base["pending_execution_mode"] = requested
                    base["operator_last_command"] = {"ts": ts_now, "action": action, "requested": requested}

                await asyncio.to_thread(_locked_state_update, _set_pending_mode, True)
                continue

            if action == "sell_fraction":
                tr = self.trades.get(mint)
                if not tr or tr.closed or tr.qty_remaining <= 0:
                    append_recent_activity("MANUAL SELL MISS", mint=mint or "unknown", live_pnl=0.0, ts=ts_now)
                    write_state(operator_last_command={"ts": ts_now, "action": action, "mint": mint, "status": "not_found"})
                    continue
                fraction = max(0.0, min(1.0, fraction))
                if fraction <= 0:
                    continue
                pct_label = int(round(fraction * 100))
                tag = f"MANUAL SELL {pct_label}%"
                await self._sell_fraction(tr, fraction, tag)
                if tr.closed:
                    self._finalize_trade_close(tr, tag)
                    append_telemetry_event(
                        "MANUAL_SELL_CLOSE_CONFIRMED", mint=mint,
                        detail=f"tag={tag} realized_roi_pct={tr.realized_roi_pct:.2f}% realized_pnl={tr.realized_pnl:.4f}",
                        severity="info",
                    )
                    # Immediate state sync — do not wait for the end of the monitor loop.
                    # Without this, the HUD reads stale on-disk state and shows the position
                    # as still open for 1-2s after a successful manual sell.
                    self._state_sync()
                elif tr.status == "partial_exit":
                    append_telemetry_event(
                        "MANUAL_SELL_PARTIAL_CONFIRMED", mint=mint,
                        detail=f"tag={tag} qty_remaining={tr.qty_remaining:.4f} realized_roi_pct={tr.realized_roi_pct:.2f}%",
                        severity="info",
                    )
                    self._state_sync()
                append_recent_activity(tag, mint=mint, live_pnl=0.0, ts=ts_now)
                write_state(operator_last_command={"ts": ts_now, "action": action, "mint": mint, "fraction": fraction, "status": "ok"})
                continue

            if action == "inject_position":
                qty = float(cmd.get("qty") or 0.0)
                entry_price_usd = float(cmd.get("entry_price_usd") or 0.0)
                size_usd = float(cmd.get("size_usd") or 0.0)
                if not mint or qty <= 0:
                    write_state(operator_last_command={"ts": ts_now, "action": action, "mint": mint, "status": "error", "detail": "mint or qty invalid"})
                    log.error("[OPERATOR] inject_position rejected — mint=%r qty=%s", mint, qty)
                    continue
                if mint in self.trades and not self.trades[mint].closed:
                    write_state(operator_last_command={"ts": ts_now, "action": action, "mint": mint, "status": "skipped", "detail": "position already open"})
                    log.warning("[OPERATOR] inject_position skipped — position already open mint=%s", mint)
                    continue
                entry_px = entry_price_usd if entry_price_usd > 0 else 1e-12
                if size_usd <= 0:
                    size_usd = round(qty * entry_px, 6) if entry_px > 1e-12 else 0.0
                now_ts_f = float(ts_now)
                injected = PaperTrade(
                    mint=mint,
                    ts=now_ts_f,
                    size_usd=size_usd,
                    reason="operator_inject:manual",
                    entry_px=entry_px,
                    current_px=entry_px,
                    current_px_source="injected",
                    last_mark_ts=now_ts_f,
                    high_px=entry_px,
                    low_px=entry_px,
                    stop_px=entry_px * (1 - DEFAULT_DOWNSIDE_STOP_PCT),  # P3.2: canonical stop at entry
                    qty_total=qty,
                    qty_remaining=qty,
                    fees_usd=0.0,
                    status="open",
                    entry_ts=now_ts_f,
                    entry_price=entry_px,
                    last_price=entry_px,
                    last_price_update_at=0.0,
                    mark_price_source="fallback",
                    mark_notional_usd_used=0.0,
                    mark_notional_size_used=0.0,
                    mark_confidence_level="LOW",
                    mark_vs_full_liq_diff_pct=None,
                    mark_stale=True,
                    mark_staleness_seconds=0.0,
                    unrealized_roi_pct=0.0,
                    realized_roi_pct=0.0,
                    exit_ts=0.0,
                    exit_price=0.0,
                    close_reason="",
                    qty_closed=0.0,
                    proceeds_usd=0.0,
                    cost_basis_total_usd=size_usd,
                    remaining_basis_usd=size_usd,
                    realized_basis_usd=0.0,
                    realized_fees_usd=0.0,
                    close_recorded=False,
                )
                self.trades[mint] = injected
                self._state_sync()
                append_recent_activity("INJECT POSITION", mint=mint, live_pnl=0.0, ts=ts_now)
                write_state(operator_last_command={"ts": ts_now, "action": action, "mint": mint, "qty": qty, "entry_price_usd": entry_px, "size_usd": size_usd, "status": "ok"})
                log.warning(
                    "[OPERATOR] inject_position — mint=%s qty=%.4g entry_px=%.10g size_usd=%.4f stop_px=%.10g",
                    mint, qty, entry_px, size_usd, entry_px * (1 - DEFAULT_DOWNSIDE_STOP_PCT),
                )
                await tg_send(
                    SNIPER_TG_TOKEN, SNIPER_TG_CHAT_ID,
                    f"🔧 Operator inject: {mint[:20]}... qty={qty:.4g} entry=${entry_px:.6g} size_usd=${size_usd:.2f}",
                )
                continue

            if action == "enable_live_tx":
                # Arm live TX submission (prerequisite for trading, not sufficient alone for buys)
                try:
                    import live_canary as _lc
                    _lc.enable_live_tx_submission()
                    def _set_live_tx_on(base):
                        controls = dict(base.get("operator_controls") or {})
                        prev_tx = controls.get("live_tx_submission_enabled", False)
                        prev_trading = controls.get("trading_enabled")
                        controls["live_tx_submission_enabled"] = True
                        # Safety: if trading_enabled has no explicit operator value,
                        # default it to False so arming TX never silently enables buys.
                        if "trading_enabled" not in controls:
                            controls["trading_enabled"] = False
                        base["operator_controls"] = controls
                        base["operator_last_command"] = {"ts": ts_now, "action": action}
                        log.warning(
                            "[OPERATOR] enable_live_tx — live_tx: %s→True | trading_enabled: %s (unchanged unless was missing, then False)",
                            prev_tx, prev_trading,
                        )
                    await asyncio.to_thread(_locked_state_update, _set_live_tx_on, True)
                    append_recent_activity("LIVE TX ARMED", mint="system", live_pnl=0.0, ts=ts_now)
                    log.warning("[OPERATOR] enable_live_tx applied — TX infrastructure armed, trading still requires enable_trading")
                except Exception as e:
                    log.error("enable_live_tx operator command failed: %s", e)
                continue

            if action == "disable_live_tx":
                # Disarm live TX submission — safety cascade also disables trading
                try:
                    import live_canary as _lc
                    _lc.disable_live_tx_submission()  # cascades TRADING_ENABLED=False internally
                    def _set_live_tx_off(base):
                        controls = dict(base.get("operator_controls") or {})
                        controls["live_tx_submission_enabled"] = False
                        controls["trading_enabled"] = False  # mirror safety cascade
                        base["operator_controls"] = controls
                        base["operator_last_command"] = {"ts": ts_now, "action": action}
                    await asyncio.to_thread(_locked_state_update, _set_live_tx_off, True)
                    append_recent_activity("LIVE TX DISARMED", mint="system", live_pnl=0.0, ts=ts_now)
                    log.warning("[OPERATOR] disable_live_tx applied — TX disarmed, trading also disabled (safety cascade)")
                except Exception as e:
                    log.error("disable_live_tx operator command failed: %s", e)
                continue

            if action == "enable_trading":
                # Requires live_tx_submission_enabled first; raises RuntimeError if not
                try:
                    import live_canary as _lc
                    _lc.enable_trading()
                    def _set_trading_on(base):
                        controls = dict(base.get("operator_controls") or {})
                        controls["trading_enabled"] = True
                        base["operator_controls"] = controls
                        base["operator_last_command"] = {"ts": ts_now, "action": action}
                    await asyncio.to_thread(_locked_state_update, _set_trading_on, True)
                    append_recent_activity("TRADING ENABLED", mint="system", live_pnl=0.0, ts=ts_now)
                    log.warning("[OPERATOR] enable_trading applied — buys are now live")
                except RuntimeError as blocked_err:
                    log.error("[OPERATOR] enable_trading BLOCKED: %s", blocked_err)
                    append_telemetry_event("TRADING_ENABLE_BLOCKED", detail=str(blocked_err), severity="error")
                    write_state(operator_last_command={"ts": ts_now, "action": action, "status": "blocked", "reason": str(blocked_err)})
                except Exception as e:
                    log.error("enable_trading operator command failed: %s", e)
                continue

            if action == "disable_trading":
                try:
                    import live_canary as _lc
                    _lc.disable_trading()
                    def _set_trading_off(base):
                        controls = dict(base.get("operator_controls") or {})
                        controls["trading_enabled"] = False
                        base["operator_controls"] = controls
                        base["operator_last_command"] = {"ts": ts_now, "action": action}
                    await asyncio.to_thread(_locked_state_update, _set_trading_off, True)
                    append_recent_activity("TRADING DISABLED", mint="system", live_pnl=0.0, ts=ts_now)
                    log.warning("[OPERATOR] disable_trading applied")
                except Exception as e:
                    log.error("disable_trading operator command failed: %s", e)
                continue

    async def _update_trailing_and_stages(self, tr: PaperTrade):
        # GHOST_PENDING positions have no confirmed wallet backing — their price
        # marks are stale/sentinel and would trip TRAIL/SL exits against tokens
        # that may never have landed. _ghost_pending_recheck owns the lifecycle.
        if tr.position_truth_state == "GHOST_PENDING":
            return
        if tr.current_px > tr.high_px:
            tr.high_px = tr.current_px
        pnl_pct = (tr.current_px - tr.entry_px) / tr.entry_px
        controls = self._operator_controls()
        default_stop_pct = float(controls.get("default_downside_stop_pct", DEFAULT_DOWNSIDE_STOP_PCT) or DEFAULT_DOWNSIDE_STOP_PCT)
        absolute_failsafe_stop_pct = float(controls.get("absolute_failsafe_stop_pct", ABSOLUTE_FAILSAFE_STOP_PCT) or ABSOLUTE_FAILSAFE_STOP_PCT)
        # P6.7: in live mode CANARY_FAILSAFE_STOP_PCT tightens the hard cap for
        # real-money positions. Takes the min of the two so whichever is stricter wins.
        if self.execution_mode == "live":
            canary_failsafe = float(os.getenv("CANARY_FAILSAFE_STOP_PCT", "0.0") or 0.0)
            if canary_failsafe > 0:
                absolute_failsafe_stop_pct = min(absolute_failsafe_stop_pct, canary_failsafe)

        # Rapid dump detection: exit immediately if price fell sharply in the last window.
        # Sample prev_px each call; if price dropped >= RAPID_DUMP_PCT in <= RAPID_DUMP_WINDOW_SECONDS, exit now.
        now_ts = time.time()
        hold_time_s = now_ts - float(tr.entry_ts or tr.ts or now_ts)
        if (tr.prev_px is not None and tr.prev_px_ts is not None
                and (now_ts - tr.prev_px_ts) <= RAPID_DUMP_WINDOW_SECONDS
                and tr.prev_px > 0):
            recent_drop = (tr.prev_px - tr.current_px) / tr.prev_px
            if recent_drop >= RAPID_DUMP_PCT and tr.qty_remaining > 0 and pnl_pct > 0.10:
                # Minimum hold guard: suppress early noise dumps. A single stale or
                # wide-spread quote within the first 10s with < 3 executable marks can
                # look like a 10%+ dump even when no real price move occurred.
                # HARD_CAP_EXIT and FAILSAFE STOP are in separate code paths and
                # remain active from T=0 regardless of this guard.
                if hold_time_s < 10.0 and tr.executable_mark_observations < 3:
                    append_telemetry_event(
                        "RAPID_DUMP_EXIT_GUARDED", mint=tr.mint,
                        detail=f"drop={recent_drop*100:.1f}% hold_s={hold_time_s:.1f} exec_marks={tr.executable_mark_observations} — too early/thin, skipping",
                        severity="warn",
                    )
                else:
                    append_telemetry_event("RAPID_DUMP_EXIT", mint=tr.mint,
                        detail=f"drop={recent_drop*100:.1f}% window_s={now_ts-tr.prev_px_ts:.0f} hold_s={hold_time_s:.1f} exec_marks={tr.executable_mark_observations}", severity="warn")
                    tr.stop_trigger_ts = now_ts
                    await self._sell_fraction(tr, tr.qty_remaining / tr.qty_total, "RAPID DUMP EXIT")
                    if tr.closed:
                        self._finalize_trade_close(tr, "RAPID DUMP EXIT")
                    return
        tr.prev_px = tr.current_px
        tr.prev_px_ts = now_ts

        # Layer 5: time exit — close if age > 120s AND ROI < +10%.
        if hold_time_s > 120.0 and pnl_pct < 0.10 and tr.high_px < tr.entry_px * 1.15 and tr.qty_remaining > 0:
            tr.stop_trigger_ts = now_ts
            append_telemetry_event("TIME_EXIT_120S", mint=tr.mint,
                detail=f"age={hold_time_s:.0f}s roi={pnl_pct*100:.1f}%", severity="info")
            await self._sell_fraction(tr, tr.qty_remaining / tr.qty_total, "TIME_EXIT_120S")
            if tr.closed:
                self._finalize_trade_close(tr, "TIME_EXIT_120S")
            return

        # Time-based stop tightening: narrow the stop floor as position ages.
        # This prevents slow bleeds where a losing position sits at -11% for hours.
        position_age_s = max(0.0, now_ts - float(tr.entry_ts or tr.ts or now_ts))
        if position_age_s >= STOP_TIGHTEN_PHASE3_SECONDS:
            effective_stop_pct = STOP_TIGHTEN_PHASE3_PCT
        elif position_age_s >= STOP_TIGHTEN_PHASE2_SECONDS:
            effective_stop_pct = STOP_TIGHTEN_PHASE2_PCT
        else:
            # P3.2: use DEFAULT_DOWNSIDE_STOP_PCT from entry onward. A prior revision
            # used a wider 15% stop for the first 30s, creating a silent gap
            # between config and behavior. The stop now matches config from tick 0
            # and tightens via STOP_TIGHTEN_PHASE2/PHASE3 as the position ages.
            effective_stop_pct = default_stop_pct
        # Ratchet: only move stop up (tighter), never widen it
        tr.stop_px = max(tr.stop_px, tr.entry_px * (1 - effective_stop_pct))

        # Absolute failsafe: liquidate everything if loss exceeds hard cap
        if pnl_pct <= -absolute_failsafe_stop_pct and tr.qty_remaining > 0:
            tr.stop_trigger_ts = now_ts
            append_telemetry_event(
                "FORCED_FAILSAFE_EXIT_TRIGGERED", mint=tr.mint,
                detail=f"pnl_pct={pnl_pct:.4f} threshold={-absolute_failsafe_stop_pct:.4f} entry_px={tr.entry_px} current_px={tr.current_px} mfe_pct={tr.mfe_pct:.2f}",
                severity="error",
            )
            await self._sell_fraction(tr, 1.0, "FAILSAFE STOP")
            if tr.closed:
                self._finalize_trade_close(tr, "FAILSAFE STOP")
            return

        # Restore guard: block TP replay on positions restored from state
        if tr.restored_from_state_ts and not tr.fresh_mark_seen_since_restore:
            if tr.stage1_done and pnl_pct >= TP1_PCT:
                append_telemetry_event("RESTORED_TP_REPLAY_BLOCKED", mint=tr.mint, detail="stage1", severity="warn")
            if tr.stage2_done and pnl_pct >= TP2_PCT:
                append_telemetry_event("RESTORED_TP_REPLAY_BLOCKED", mint=tr.mint, detail="stage2", severity="warn")
            if tr.stage3_done and pnl_pct >= TP3_PCT:
                append_telemetry_event("RESTORED_TP_REPLAY_BLOCKED", mint=tr.mint, detail="stage3", severity="warn")

        # ── Sentinel-entry suspect detection (graduation curve phantoms) ──
        # Skip breakeven/trail stops when entry_px is a known sentinel: their
        # stop levels would be meaningless.
        _SUSPECT_SENTINELS = (6.162627048e-05, 1.3e-05)
        _entry_is_suspect = any(
            abs(tr.entry_px - sp) / sp <= 0.001 for sp in _SUSPECT_SENTINELS
        ) if tr.entry_px > 0 else False
        if _entry_is_suspect and not getattr(tr, '_suspect_skip_logged', False):
            log.warning(
                "[SUSPECT_ENTRY_PX_SKIP] mint=%s entry_px=%.10g — skipping breakeven floor and trail (graduation curve sentinel)",
                tr.mint, tr.entry_px,
            )
            tr._suspect_skip_logged = True

        # P3.1: doctrine — TP ladder runs FIRST. Trailing stop only arms after
        # stage1_done. A prior revision armed trail at ROI>=25% and let it fire
        # before TP1 (30%), which meant a spike from 25→28→retrace would close
        # the full position via trail without TP1 ever laddering. Per
        # project_constants.md the canonical doctrine is the TP ladder; trail is
        # the runner-management mechanism for what's left after TP1.
        if (not tr.stage1_done) and pnl_pct >= TP1_PCT:
            stp = read_state(); write_state(tp1_hits=int(stp.get("tp1_hits", 0)) + 1)
            await self._sell_fraction(tr, TP1_SELL, f"TP1(+{TP1_PCT*100:.0f}%)", stage_flag="stage1_done")

        if (not tr.stage2_done) and pnl_pct >= TP2_PCT:
            stp = read_state(); write_state(tp2_hits=int(stp.get("tp2_hits", 0)) + 1)
            await self._sell_fraction(tr, TP2_SELL, f"TP2(+{TP2_PCT*100:.0f}%)", stage_flag="stage2_done")

        if (not tr.stage3_done) and pnl_pct >= TP3_PCT:
            await self._sell_fraction(tr, TP3_SELL, f"TP3(+{TP3_PCT*100:.0f}%)", stage_flag="stage3_done")

        if not _entry_is_suspect:
            # Priority 4 — BREAKEVEN floor. Only before TP1 fires; once
            # stage1_done the trail block below owns the stop.
            if (not tr.stage1_done) and pnl_pct >= BREAKEVEN_TRIGGER_PCT and tr.qty_remaining > 0:
                tr.stop_px = max(tr.stop_px, tr.entry_px * 1.02)

            # P3.1: TRAILING_STOP for the runner tranche — only active after TP1
            # has fired. Ratchet-only. If trail pierces, close the remaining qty.
            if tr.stage1_done and tr.qty_remaining > 0:
                trail_factor = 0.95 if tr.stage3_done else (1.0 - TRAIL_FROM_MFE_PCT)
                trail_stop = tr.high_px * trail_factor
                tr.stop_px = max(tr.stop_px, trail_stop)
                log.debug("[TRAIL_ARM] mint=%s roi=%.1f%% stop_px=%.10g high_px=%.10g",
                          tr.mint, pnl_pct * 100, tr.stop_px, tr.high_px)
                if tr.current_px <= trail_stop:
                    tr.stop_trigger_ts = now_ts
                    append_telemetry_event("TRAIL_STOP_EXIT", mint=tr.mint,
                        detail=f"roi={pnl_pct*100:.1f}% high_px={tr.high_px:.10g} trail_stop={trail_stop:.10g}",
                        severity="info")
                    await self._sell_fraction(tr, tr.qty_remaining / tr.qty_total, "TRAIL/SL EXIT")
                    if tr.closed:
                        self._finalize_trade_close(tr, "TRAIL/SL EXIT")
                    return

        # Breakeven/trail logic moved above TP ladder (priority reorder).

        # Exit remaining position if price hits stop
        if tr.current_px <= tr.stop_px and tr.qty_remaining > 0:
            tr.stop_trigger_ts = now_ts
            await self._sell_fraction(tr, tr.qty_remaining / tr.qty_total, "TRAIL/SL EXIT")
            if tr.closed:
                self._finalize_trade_close(tr, "TRAIL/SL EXIT")

    def _build_phase5c_analytics(self, hist: list[dict]) -> dict:
        now_ts = int(time.time())
        cutoff_today = utc_day()
        today_start_ts = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

        all_rows = self._paper_positions_snapshot()
        open_rows = [r for r in all_rows if r.get("status") != "closed"]
        canonical_open_rows = self._canonical_session_position_rows(all_rows)
        canonical_open_rows_with_stale = self._canonical_session_position_rows(all_rows, include_stale=True)
        close_events = [x for x in hist if x.get("type") == "trade_close"]

        def _sum(rows, key):
            return round(sum(float(r.get(key, 0.0) or 0.0) for r in rows), 6)

        def _avg(vals):
            vals=[float(v) for v in vals if v is not None]
            return round(sum(vals)/len(vals), 4) if vals else 0.0

        def _reason_breakdown(rows):
            out={}
            for r in rows:
                reason=str(r.get("close_reason") or "OPEN")
                bucket=out.setdefault(reason,{"count":0,"wins":0,"losses":0,"realized_pnl_usd":0.0,"avg_realized_roi_pct":0.0})
                pnl=float(r.get("realized_pnl_usd", r.get("pnl_usd", 0.0)) or 0.0)
                roi=float(r.get("realized_roi_pct", r.get("roi_pct", 0.0)) or 0.0)
                bucket["count"] += 1
                bucket["wins"] += 1 if pnl >= 0 else 0
                bucket["losses"] += 1 if pnl < 0 else 0
                bucket["realized_pnl_usd"] += pnl
                bucket.setdefault("_rois", []).append(roi)
            for reason,b in out.items():
                rois=b.pop("_rois",[])
                b["realized_pnl_usd"] = round(b["realized_pnl_usd"],4)
                b["avg_realized_roi_pct"] = round(sum(rois)/len(rois),4) if rois else 0.0
            return out

        def _expectancy(rows):
            if not rows:
                return {"per_trade_usd":0.0,"win_rate_pct":0.0,"avg_win_usd":0.0,"avg_loss_usd":0.0}
            pnls=[float(r.get("realized_pnl_usd", r.get("pnl_usd", 0.0)) or 0.0) for r in rows]
            wins=[p for p in pnls if p >= 0]
            losses=[p for p in pnls if p < 0]
            win_rate=(len(wins)/len(pnls))*100.0 if pnls else 0.0
            avg_win=(sum(wins)/len(wins)) if wins else 0.0
            avg_loss=(sum(losses)/len(losses)) if losses else 0.0
            expectancy=(win_rate/100.0)*avg_win + (1.0-win_rate/100.0)*avg_loss
            return {"per_trade_usd": round(expectancy,4), "win_rate_pct": round(win_rate,4), "avg_win_usd": round(avg_win,4), "avg_loss_usd": round(avg_loss,4)}

        def _window(closed_filter):
            rows=[
                r for r in close_events
                if closed_filter({**r, "exit_ts": r.get("exit_ts", r.get("ts", 0)), "entry_ts": r.get("entry_ts", 0)})
            ]
            openw=[r for r in open_rows if closed_filter({"entry_ts": r.get("entry_ts",0)})]
            realized=round(sum(float(r.get("realized_pnl_usd", r.get("pnl_usd", 0.0)) or 0.0) for r in rows), 6)
            unrealized=_sum(openw, "unrealized_pnl_usd")
            hold_avg=_avg([r.get("hold_time_seconds") for r in rows])
            mfe_avg=_avg([r.get("mfe_pct") for r in rows])
            mae_avg=_avg([r.get("mae_pct") for r in rows])
            return {
                "realized_pnl_usd": realized,
                "unrealized_pnl_usd": unrealized,
                "total_pnl_usd": round(realized + unrealized, 6),
                "closed_trades": len(rows),
                "open_positions": len(openw),
                "avg_hold_time_seconds": hold_avg,
                "avg_mfe_pct": mfe_avg,
                "avg_mae_pct": mae_avg,
                "expectancy": _expectancy(rows),
                "win_loss_by_close_reason": _reason_breakdown(rows),
            }

        session_start = int(self.start_ts)
        analytics = {
            "formulas": {
                "realized_pnl_usd": "sum(closed.realized_pnl_usd)",
                "unrealized_pnl_usd": "sum(open.market_value_usd - open.remaining_basis_usd - open.remaining_fee_reserve_usd)",
                "expectancy_per_trade_usd": "win_rate * avg_win_usd + (1 - win_rate) * avg_loss_usd",
                "avg_hold_time_seconds": "mean(exit_ts - entry_ts for closed trades)",
                "mfe_pct": "max((high_px - entry_px)/entry_px*100)",
                "mae_pct": "min((low_px - entry_px)/entry_px*100)",
            },
            "today_utc": _window(lambda r: int(r.get("exit_ts", r.get("entry_ts",0)) or 0) >= today_start_ts),
            "session": _window(lambda r: int(r.get("exit_ts", r.get("entry_ts",0)) or 0) >= session_start),
            "lifetime": _window(lambda r: True),
        }
        analytics["current_open"] = {
            "count": len(canonical_open_rows),
            "unrealized_pnl_usd": _sum(canonical_open_rows, "unrealized_pnl_usd"),
            "avg_unrealized_roi_pct": _avg([r.get("unrealized_roi_pct") for r in canonical_open_rows]),
            "avg_mfe_pct": _avg([r.get("mfe_pct") for r in canonical_open_rows]),
            "avg_mae_pct": _avg([r.get("mae_pct") for r in canonical_open_rows]),
            "stale_excluded_count": max(0, len(canonical_open_rows_with_stale) - len(canonical_open_rows)),
        }
        return analytics

    def _state_sync(self):
        st = read_state()
        session_start_budget = float(st.get("session_start_budget_usd") or MAX_TEST_BUDGET_USD)
        paper_rows = self._paper_positions_snapshot()
        self._ensure_canonical_capital_fields(st, rows=paper_rows, cash_budget=self.budget)
        canonical_open_rows = self._canonical_session_position_rows(paper_rows)
        canonical_capital_rows = self._capital_open_position_rows(paper_rows, include_stale=True)
        canonical_visible_capital_rows = self._capital_open_position_rows(paper_rows, include_stale=False)
        moonbags = []
        for tr in self._trades_snapshot():
            if tr.closed:
                continue
            if int(tr.entry_ts or tr.ts or 0) < int(self.start_ts or 0):
                continue
            if tr.stage2_done and tr.qty_remaining > 0:
                moonbags.append({"mint": tr.mint, "qty_remaining": round(tr.qty_remaining, 4), "entry_px": tr.entry_px, "current_px": round(tr.current_px, 6), "stop_px": round(tr.stop_px, 6)})

        cutoff = int(time.time()) - 86400
        candidates = [x for x in self.closed_trades if x.get("ts", 0) >= cutoff]
        top24 = max(candidates, key=lambda x: x.get("roi_pct", -9999)) if candidates else None

        dm = st.get("daily_metrics") or default_metrics(utc_day())
        telemetry_rows = list(st.get("telemetry_events") or [])

        # Rolling 24h analytics (persistent across restarts via history.json).
        now_ts = int(time.time())
        cutoff_24h = now_ts - 86400
        hist = load_history()
        exec24 = [x for x in hist if x.get("type") == "execution_attempt" and int(x.get("ts", 0) or 0) >= cutoff_24h]
        close24 = [x for x in hist if x.get("type") == "trade_close" and int(x.get("ts", 0) or 0) >= cutoff_24h]
        avg_latency_24h = round(sum(float(x.get("latency_ms", 0) or 0) for x in exec24) / len(exec24), 3) if exec24 else 0.0
        wins24 = len([x for x in close24 if float(x.get("pnl_usd", 0) or 0) >= 0])
        losses24 = len(close24) - wins24
        win_rate_24h = round((wins24 / len(close24)) * 100.0, 2) if close24 else 0.0

        analytics_phase5c = self._build_phase5c_analytics(hist)
        session_block = dict(analytics_phase5c.get("session") or {})
        session_window_unrealized = round(sum(float(r.get("unrealized_pnl_usd", 0.0) or 0.0) for r in canonical_open_rows), 6)
        session_block["unrealized_pnl_usd"] = session_window_unrealized
        session_block["open_positions"] = len(canonical_open_rows)
        session_block["total_pnl_usd"] = round(float(session_block.get("realized_pnl_usd", 0.0) or 0.0) + session_window_unrealized, 6)
        analytics_phase5c["session"] = session_block
        analytics_phase5c["current_open"] = {
            "count": len(canonical_open_rows),
            "unrealized_pnl_usd": session_window_unrealized,
            "avg_unrealized_roi_pct": round(sum(float(r.get("unrealized_roi_pct", 0.0) or 0.0) for r in canonical_open_rows) / len(canonical_open_rows), 4) if canonical_open_rows else 0.0,
            "avg_mfe_pct": round(sum(float(r.get("mfe_pct", 0.0) or 0.0) for r in canonical_open_rows) / len(canonical_open_rows), 4) if canonical_open_rows else 0.0,
            "avg_mae_pct": round(sum(float(r.get("mae_pct", 0.0) or 0.0) for r in canonical_open_rows) / len(canonical_open_rows), 4) if canonical_open_rows else 0.0,
            "stale_excluded_count": max(0, len(self._canonical_session_position_rows(paper_rows, include_stale=True)) - len(canonical_open_rows)),
        }
        pnl_total = round(float(session_block.get("total_pnl_usd", 0.0) or 0.0), 6)
        session_window_budget_usd = round(session_start_budget + pnl_total, 6)
        capital_unrealized_usd = round(sum(float(r.get("unrealized_pnl_usd", 0.0) or 0.0) for r in canonical_capital_rows), 6)
        canonical_start_capital_usd = round(float(st.get("canonical_start_capital_usd") or 0.0), 6)
        canonical_realized_usd = round(float(st.get("canonical_realized_usd") or 0.0), 6)
        canonical_capital_usd = round(canonical_start_capital_usd + canonical_realized_usd + capital_unrealized_usd, 6)
        sim_budget_usd = canonical_capital_usd
        roi_pct = (pnl_total / session_start_budget) * 100.0 if session_start_budget > 0 else 0.0
        telemetry_1h = [row for row in telemetry_rows if now_ts - int(row.get("ts", 0) or 0) <= 3600]
        ws_disconnects_1h = sum(1 for row in telemetry_1h if str(row.get("type") or "") == "WS_DISCONNECT")
        ws_subscribe_1h = sum(1 for row in telemetry_1h if str(row.get("type") or "") == "WS_SUBSCRIBE_ACK")
        rpc_timeouts_1h = sum(1 for row in telemetry_1h if str(row.get("type") or "") == "RPC_TIMEOUT")
        rpc_ok_1h = sum(1 for row in telemetry_1h if str(row.get("type") or "") == "RPC_OK")
        rpc_success_rate = (rpc_ok_1h / max(1, rpc_ok_1h + rpc_timeouts_1h)) * 100.0
        ws_uptime_ratio = (ws_subscribe_1h / max(1, ws_subscribe_1h + ws_disconnects_1h))

        _paper_only_sync = {} if self.execution_mode == "live" else {
            "sim_budget_usd": round(sim_budget_usd, 6),
            "paper_budget_left": round(self.budget, 2),
            "paper_spent_usd": round(max(0.0, session_start_budget - self.budget), 2),
        }
        write_state(
            session_start_budget_usd=round(session_start_budget, 6),
            session_window_budget_usd=round(session_window_budget_usd, 6),
            canonical_start_capital_usd=round(canonical_start_capital_usd, 6),
            canonical_realized_usd=round(canonical_realized_usd, 6),
            canonical_unrealized_usd=round(capital_unrealized_usd, 6),
            canonical_capital_usd=round(canonical_capital_usd, 6),
            pnl_usd=round(pnl_total, 4),
            roi_pct=round(roi_pct, 3),
            **_paper_only_sync,
            top_catch_24h=top24,
            closed_trades_24h=len(candidates),
            active_moonbags=moonbags,
            paper_positions=paper_rows,
            active_positions=canonical_visible_capital_rows,
            analytics_phase5c=analytics_phase5c,
            rolling_24h={
                "window_seconds": 86400,
                "execution_attempts": len(exec24),
                "closed_trades": len(close24),
                "wins": wins24,
                "losses": losses24,
                "win_rate_pct": win_rate_24h,
                "avg_decision_latency_ms": avg_latency_24h,
            },
            last_trade=st.get("last_trade"),
            process_uptime_seconds=int(time.time()) - self.start_ts,
            ws_disconnects_1h=ws_disconnects_1h,
            ws_uptime_ratio=round(ws_uptime_ratio, 4),
            rpc_timeouts_1h=rpc_timeouts_1h,
            rpc_success_rate=round(rpc_success_rate, 2),
            decision_breakdown={
                "total_scans": dm.get("total_scans", 0),
                "unique_mints": dm.get("unique_mints", 0),
                "duplicates_skipped": dm.get("duplicates_skipped", 0),
                "vetoed_liquidity": dm.get("vetoed_liquidity", 0),
                "vetoed_price": dm.get("vetoed_price", 0),
                "successful_entries": dm.get("successful_entries", 0),
            },
        )

    async def _post_execution(self, mint: str, intent: ExecutionIntent, result: ExecutionResult, t0: float, source: str = "primary", decision_breakdown: Optional[dict] = None):
        latency_ms = (time.perf_counter() - t0) * 1000
        decision_breakdown = dict(decision_breakdown or {})
        ts_now = int(time.time())
        self._finalize_hot_until = max(float(self._finalize_hot_until or 0.0), time.time() + 3.0)

        # Stamp detect_ts_wall + decision_id on the trade for ALL entry paths
        # (previously only stamped in on_log_event — delayed_entry / migration_retry /
        # dependency_retry paths were missing it, making opportunity_age_ms unpopulated).
        if result.ok and mint in self.trades:
            if float(intent.ts_detected or 0.0) > 0 and not self.trades[mint].detect_ts_wall:
                self.trades[mint].detect_ts_wall = float(intent.ts_detected)
            if intent.decision_id and not self.trades[mint].decision_id:
                self.trades[mint].decision_id = intent.decision_id

        t_hist_start = time.perf_counter()
        append_history(
            {
                "type": "execution_attempt",
                "ts": ts_now,
                "mode": result.mode,
                "decision_id": intent.decision_id,
                "mint": mint,
                "size_usd": round(intent.size_usd, 4),
                "reference_price_usd": round(intent.reference_price_usd, 10) if intent.reference_price_usd is not None else None,
                "fill_price_usd": result.extra.get("fill_price_usd"),
                "reference_price_is_real": result.extra.get("reference_price_is_real", False),
                "tx_sig": result.tx_sig,
                "cost_to_trade_usd": result.cost_to_trade_usd,
                "ok": result.ok,
                "error": result.error,
                "latency_ms": round(latency_ms, 4),
                "ix_order": result.extra.get("ix_order", []),
                "tip_account": result.extra.get("tip_account"),
                "execution_latency_ms": round(result.latency_ms, 4),
                "decision_breakdown_ms": decision_breakdown,
                "min_liquidity_usd_active": self.current_liquidity_usd,
                "source": source,
                # P1-1: detect → execution wall-clock latency (ts_detected is set at signal enqueue)
                "detect_to_exec_ms": round((time.time() - float(intent.ts_detected)) * 1000.0, 1) if intent.ts_detected else None,
                "ts_detected": round(float(intent.ts_detected), 3) if intent.ts_detected else None,
                "ts_decided": round(float(intent.ts_decided), 3) if intent.ts_decided else None,
                "holder_pct_top5": decision_breakdown.get("holder_pct_top5"),
            }
        )
        decision_breakdown["post_exec_history_ms"] = round((time.perf_counter() - t_hist_start) * 1000, 4)

        day = utc_day()
        daily_delta = {
            "successful_entries": 1 if result.ok else 0,
            "decision_count": 1,
            "decision_latency_sum_ms": latency_ms,
        }
        all_delta = dict(daily_delta)

        def _mut_post_execution(base):
            dm = base.get("daily_metrics") or default_metrics(day)
            at = base.get("all_time_metrics") or default_metrics("all-time")
            if dm.get("day_utc") != day:
                append_history({"type": "daily_metrics_rollover", "ts": int(time.time()), "day_utc": dm.get("day_utc"), "metrics": dm})
                dm = default_metrics(day)
                self.daily_seen_mints = set()
            for k, v in daily_delta.items():
                dm[k] = (dm.get(k, 0) or 0) + v
            for k, v in all_delta.items():
                at[k] = (at.get(k, 0) or 0) + v
            dm = self._with_rates(dm)
            at = self._with_rates(at)
            base["daily_metrics"] = dm
            base["all_time_metrics"] = at
            base["min_liquidity_usd_active"] = self.current_liquidity_usd
            base["paper_buys_under_lowered_threshold"] = self.paper_buys_under_lowered_threshold
            base["avg_latency_ms"] = latency_ms
            base["execution_mode"] = self.execution_mode
            base["cu_price"] = result.cu_price
            base["cu_limit"] = result.cu_limit
            base["jito_tip"] = result.jito_tip_lamports
            base["cost_to_trade"] = result.cost_to_trade_usd
            base["priority_tier"] = result.priority_tier
            base["live_trade_attempts"] = self.live_trade_attempts
            base["last_execution"] = {
                "ts": ts_now,
                "mode": result.mode,
                "decision_id": intent.decision_id,
                "mint": mint,
                "size_usd": round(intent.size_usd, 4),
                "cu_price": result.cu_price,
                "cu_limit": result.cu_limit,
                "jito_tip": result.jito_tip_lamports,
                "priority_tier": result.priority_tier,
                "tx_sig": result.tx_sig,
                "cost_to_trade": result.cost_to_trade_usd,
                "ok": result.ok,
                "error": result.error,
                "source": source,
            }

        t_state_start = time.perf_counter()
        await asyncio.to_thread(_locked_state_update, _mut_post_execution)
        decision_breakdown["post_exec_state_ms"] = round((time.perf_counter() - t_state_start) * 1000, 4)

        t_notify_start = time.perf_counter()
        await self._notify_execution_result(mint, intent, result, latency_ms)
        decision_breakdown["post_exec_notify_ms"] = round((time.perf_counter() - t_notify_start) * 1000, 4)

    async def _notify_execution_result(self, mint: str, intent: ExecutionIntent, result: ExecutionResult, latency_ms: float):
        if result.mode == "paper" and result.ok:
            tip_usd = float(result.extra.get("tip_usd", 0.0) or 0.0)
            fees_usd = float(result.extra.get("fees_usd", 0.0) or 0.0)

            paper_msg = (
                f"PAPER BUY {mint} | mode=paper | size=${intent.size_usd:.2f} | "
                f"cu_price={result.cu_price} | jito_tip={result.jito_tip_lamports} | "
                f"cost_to_trade={result.cost_to_trade_usd} | tip_usd=${tip_usd:.4f} | "
                f"fees_usd=${fees_usd:.4f} | latency_ms={latency_ms:.2f}"
            )
            log.info(paper_msg)
            asyncio.create_task(tg_send(SNIPER_TG_TOKEN, SNIPER_TG_CHAT_ID, paper_msg))

            if self.current_liquidity_usd < BASE_MIN_LIQUIDITY_USD:
                self.paper_buys_under_lowered_threshold += 1
                if self.paper_buys_under_lowered_threshold >= LOW_LIQUIDITY_PAPER_BUY_CAP:
                    self.current_liquidity_usd = BASE_MIN_LIQUIDITY_USD
                    write_state(min_liquidity_usd_active=self.current_liquidity_usd)
                    asyncio.create_task(tg_send(SNIPER_TG_TOKEN, SNIPER_TG_CHAT_ID, f"Safety Revert: hit {LOW_LIQUIDITY_PAPER_BUY_CAP} PAPER BUYs under lowered threshold; MIN_LIQUIDITY_USD reverted to ${BASE_MIN_LIQUIDITY_USD:.0f}"))

        if result.mode == "live_canary":
            asyncio.create_task(tg_send(
                SNIPER_TG_TOKEN,
                SNIPER_TG_CHAT_ID,
                f"LIVE ATTEMPT {mint} | size=${intent.size_usd:.2f} | cu_price={result.cu_price} cu_limit={result.cu_limit} tip={result.jito_tip_lamports} | status={'ok' if result.ok else 'error'} | err={result.error}",
            ))

    def _is_candidate_event(self, msg: dict) -> bool:
        value = (((msg.get("params") or {}).get("result") or {}).get("value") or {})
        logs = value.get("logs") or []
        if not logs:
            return False

        self._metrics_bump({"raw_candidates": 1}, {"raw_candidates": 1})

        # Debug sampler: write first LOG_SAMPLE_MAX events to ws_log_samples.jsonl so we can
        # inspect what pump.fun log strings actually look like and tune the migration filter.
        if self._log_sample_count < self._log_sample_max:
            try:
                sample_file = Path(__file__).parent / "ws_log_samples.jsonl"
                import json as _json
                sig = str((value.get("signature") or ""))
                passed = self.is_migration_signature(logs) or self.is_raydium_pool_creation(logs)
                with open(sample_file, "a") as _f:
                    _f.write(_json.dumps({"sig": sig, "passed": passed, "logs": logs}) + "\n")
                self._log_sample_count += 1
            except Exception:
                pass

        # Check pump.fun migration first (primary signal), then direct Raydium pool creation
        return self.is_migration_signature(logs) or self.is_raydium_pool_creation(logs)

    async def _event_worker_loop(self, worker_id: int):
        while True:
            item = await self.event_queue.get()
            t_worker_start = time.perf_counter()
            try:
                queue_wait_ms = max(0.0, (time.perf_counter() - float(item.get("queued_at", time.perf_counter()))) * 1000)
                queue_depth_at_dequeue = int(getattr(self.event_queue, 'qsize', lambda: 0)() or 0)
                if queue_wait_ms > 1500:
                    queue_telemetry_event("DECISION_QUEUE_DELAY", detail=f"worker={worker_id} queue_wait_ms={queue_wait_ms:.1f} queue_depth_at_enqueue={int(item.get('queue_depth_at_enqueue',0) or 0)} queue_depth_at_dequeue={queue_depth_at_dequeue}", severity="warn")
                await self.on_log_event(item["msg"], t0=item["t0"], candidate_key=item.get("candidate_key", ""))
                worker_busy_ms = max(0.0, (time.perf_counter() - t_worker_start) * 1000)
                queue_telemetry_event("WORKER_BUSY", detail=f"worker={worker_id} worker_busy_ms={worker_busy_ms:.1f} queue_wait_ms={queue_wait_ms:.1f} queue_depth_at_enqueue={int(item.get('queue_depth_at_enqueue',0) or 0)} queue_depth_at_dequeue={queue_depth_at_dequeue}", severity="info")
            except Exception as e:
                log.error(f"worker={worker_id} candidate handling error: {e}")
            finally:
                self.event_queue.task_done()

    def _ensure_workers(self):
        if self._workers_started:
            return
        self._workers_started = True
        for idx in range(self.event_workers):
            asyncio.create_task(self._event_worker_loop(idx + 1))
        write_state(event_workers=self.event_workers, event_workers_active=True, event_workers_started_at=int(time.time()))
        log.info(f"event_workers active={self.event_workers} queue_max={EVENT_QUEUE_MAX}")

    async def on_log_event(self, msg: dict, t0: Optional[float] = None, candidate_key: str = ""):
        t0 = t0 or time.perf_counter()

        if candidate_key.startswith("sig:"):
            sig = candidate_key.split("sig:", 1)[1].strip()
            suppress_until = float(self._terminal_timeout_suppression.get(sig, 0.0) or 0.0)
            if suppress_until > time.time():
                self._parse_timeout_suppressed_count += 1
                self._post_timeout_candidate_reseen_count += 1
                queue_telemetry_event("PARSE_TIMEOUT_SUPPRESSED", detail=f"candidate_key={candidate_key} parse_timeout_suppressed_count={self._parse_timeout_suppressed_count} post_timeout_candidate_reseen_count={self._post_timeout_candidate_reseen_count}", severity="info")
                return

        # total_scans/qualified_candidates and CANDIDATE_SEEN are emitted only after candidate dedupe acceptance in run().
        t_post_parse_terminal_start = None
        _t_parse_start = time.perf_counter()  # instrumentation: timing
        try:
            mint, mint_source, terminal_reason = await asyncio.wait_for(
                self.parse_candidate_mint(msg),
                timeout=MINT_PARSE_CANDIDATE_BUDGET_SECONDS,
            )
        except asyncio.TimeoutError:
            terminal_reason = "rpc_timeout"
            mint, mint_source = None, "none"
            t_post_parse_terminal_start = time.perf_counter()
            queue_telemetry_event(
                "PARSE_CANDIDATE_BUDGET_TIMEOUT",
                detail=f"candidate_key={candidate_key} budget_s={MINT_PARSE_CANDIDATE_BUDGET_SECONDS}",
                severity="warn",
            )
        _t_parse_done = time.perf_counter()  # instrumentation: timing
        if not mint:
            # instrumentation: parse failed
            self._funnel["parse_failed"] += 1
            _tb_key = {"rpc_timeout": "rpc_timeout", "low_confidence": "parse_low_confidence",
                       "excluded_mint": "excluded_mint"}.get(terminal_reason or "no_balances", "other")
            self._terminal_blockers[_tb_key] = self._terminal_blockers.get(_tb_key, 0) + 1
            _seen_to_parse_ms = (_t_parse_done - t0) * 1000
            if _seen_to_parse_ms > DIAG_STALE_PARSE_MS:
                self._timing["stale_before_parse"] += 1
            self._flush_diagnostics()
            self._record_parse_terminal_failure(terminal_reason or "no_balances", candidate_key=candidate_key)
            queue_recent_activity(action="PARSE FAIL", mint="unknown", live_pnl=0.0)
            return
        # instrumentation: parse succeeded
        self._funnel["parsed_successfully"] += 1

        async with self._seen_lock:
            if mint in self.daily_seen_mints:
                self._metrics_bump({"duplicates_skipped": 1}, {"duplicates_skipped": 1})
                # instrumentation: duplicate terminal blocker
                self._terminal_blockers["duplicate"] += 1
                self._flush_diagnostics()
                if self._should_emit_dup_skip(mint):
                    queue_recent_activity(action="DUP SKIP", mint=mint, live_pnl=0.0)
                return
            self.daily_seen_mints.add(mint)

        source_metric = {}
        if mint_source.startswith("log") or mint_source in {"pump_log", "contextual_log", "loose_log"}:
            source_metric = {"mint_extracted_from_logs": 1}
        else:
            source_metric = {"mint_extracted_from_rpc": 1}
        self._metrics_bump({"unique_mints": 1, **source_metric}, {"unique_mints": 1, **source_metric})
        queue_telemetry_event("MINT_PARSED", mint=mint, detail=f"source={mint_source}")

        if is_excluded_mint(mint):
            # instrumentation: excluded_mint terminal blocker
            self._terminal_blockers["excluded_mint"] += 1
            self._flush_diagnostics()
            return

        controls = self._operator_controls()
        if bool(controls.get("disable_new_entries", False)):
            # P2-1: shadow execution — count separately from real execution attempts.
            # Do NOT increment execution_attempted here; that counter is reserved for
            # candidates that reached the actual buy path with trading enabled.
            self._funnel["entry_blocked_trading_disabled"] = self._funnel.get("entry_blocked_trading_disabled", 0) + 1
            self._exec_audit["execution_blocked"] += 1
            self._terminal_blockers["execution_blocked"] += 1
            self._flush_diagnostics()
            queue_telemetry_event("SHADOW_EXECUTION", mint=mint, detail="disable_new_entries=True", severity="info")
            queue_recent_activity(action="ENTRY BLOCKED", mint=mint)
            queue_state_update(last_entry_blocked={"mint": mint, "ts": int(time.time()), "reason": "disable_new_entries"})
            return

        # instrumentation: record seen_to_parse timing and qualified count
        _seen_to_parse_ms = (_t_parse_done - t0) * 1000
        self._timing["_seen_to_parse_sum"] += _seen_to_parse_ms
        self._timing["total_samples"] += 1
        _t_decision_start = time.perf_counter()  # instrumentation: start of decision phase

        # Token age gate: skip tokens that are too old at decision time (already peaked)
        candidate_age_s = (time.perf_counter() - t0)
        if candidate_age_s > MAX_TOKEN_AGE_SECONDS:
            append_telemetry_event("TOKEN_AGE_VETO", mint=mint, detail=f"age_s={candidate_age_s:.0f} max={MAX_TOKEN_AGE_SECONDS}", severity="info")
            self._funnel["veto_other"] = self._funnel.get("veto_other", 0) + 1
            self._flush_diagnostics()
            return

        min_trade_usd = LIVE_BASE_TRADE_USD if self.execution_mode == "live" else 10.0
        if mint in self.trades:
            # P2-2: already holding this mint — count separately (not an execution failure)
            self._funnel["entry_blocked_duplicate"] = self._funnel.get("entry_blocked_duplicate", 0) + 1
            queue_telemetry_event("ENTRY_BLOCKED_DUPLICATE", mint=mint, severity="info")
            return
        if self.budget < min_trade_usd:
            # P2-2: insufficient budget — count separately (opportunity cost, not exec failure)
            self._funnel["entry_blocked_budget"] = self._funnel.get("entry_blocked_budget", 0) + 1
            queue_telemetry_event("ENTRY_BLOCKED_BUDGET", mint=mint, detail=f"budget={self.budget:.2f} min={min_trade_usd:.2f}", severity="info")
            return
        hist_rows = [x for x in load_history() if str(x.get("mint") or "") == mint]
        if hist_rows:
            has_partial = any(str(x.get("type") or "") == "partial_realize" for x in hist_rows)
            has_close = any(str(x.get("type") or "") == "trade_close" for x in hist_rows)
            if has_partial and not has_close:
                queue_telemetry_event("REENTRY_BLOCKED_MISSING_CANONICAL_CLOSE", mint=mint, detail="prior lifecycle has partial_realize without trade_close", severity="error")
                queue_recent_activity(action="REENTRY BLOCKED", mint=mint)
                return

        # instrumentation: candidate passed dedup, exclusion, budget checks — now entering decision/veto path
        self._funnel["qualified_candidates"] += 1

        # Tightened decision path: run all independent checks in parallel, and skip deeper dependency work
        # when a hard veto is already known.
        t_fast_veto_start = time.perf_counter()
        # P3-1: launch ALL independent checks in parallel (price, mutable, holder, rugcheck, onchain).
        # Previously rugcheck + onchain launched only after mutable completed — adds 0.8-2s serial latency.
        price_task = asyncio.create_task(self._entry_reference_price_with_retries(mint, confirmed_migration=True))
        mutable_task = asyncio.create_task(self.helius_mutable_veto(mint))
        holder_task = asyncio.create_task(self._holder_concentration_veto(mint))
        rug_task = asyncio.create_task(asyncio.wait_for(self.rug_check(mint), timeout=DEPENDENCY_PRIMARY_BUDGET_SECONDS))
        onchain_task = asyncio.create_task(self._onchain_fast_veto(mint))
        self._timing["_decision_samples"] += 1  # P5-1: count parallel decision phases
        t_meta_start = time.perf_counter()
        mutable = await mutable_task
        metadata_lookup_ms = (time.perf_counter() - t_meta_start) * 1000
        self._timing["_mutable_ms_sum"] += metadata_lookup_ms  # P5-1
        veto_mutable, mutable_reason = mutable
        fast_veto_checks_ms = (time.perf_counter() - t_fast_veto_start) * 1000

        if veto_mutable:
            # P3-1: cancel all parallel tasks on mutable veto
            for _t in (price_task, holder_task, rug_task, onchain_task):
                if not _t.done():
                    _t.cancel()
            for _t in (price_task, holder_task, rug_task, onchain_task):
                try:
                    await _t
                except BaseException:
                    pass
            queue_state_update(last_veto={"ts": int(time.time()), "mint": mint, "reason": mutable_reason, "type": "VETO"})
            append_veto(
                mint,
                mutable_reason,
                source="helius_mutable",
                historical_threshold_usd=self.current_liquidity_usd,
                current_policy_threshold_usd=self.current_liquidity_usd,
                historical_threshold_context_only=False,
            )
            queue_recent_activity(action="VETO", mint=mint)
            asyncio.create_task(self._record_shadow_watch(
                mint,
                mutable_reason,
                None,
                historical_threshold_usd=self.current_liquidity_usd,
                current_policy_threshold_usd=self.current_liquidity_usd,
            ))
            asyncio.create_task(tg_send(SNIPER_TG_TOKEN, SNIPER_TG_CHAT_ID, f"VETO {mint}: {mutable_reason}"))
            self._metrics_bump({"vetoed_price": 1, "decision_count": 1, "decision_latency_sum_ms": (time.perf_counter() - t0) * 1000}, {"vetoed_price": 1, "decision_count": 1, "decision_latency_sum_ms": (time.perf_counter() - t0) * 1000})
            # instrumentation: price/mutable veto
            self._funnel["veto_price"] += 1
            self._timing["_parse_to_decision_sum"] += (time.perf_counter() - _t_decision_start) * 1000
            self._flush_diagnostics()
            return

        t_price_start = time.perf_counter()
        live_price = await price_task
        price_lookup_ms = (time.perf_counter() - t_price_start) * 1000
        self._timing["_price_ms_sum"] += price_lookup_ms  # P5-1

        if live_price is None:
            # P3-1: cancel all remaining parallel tasks on price veto
            for _t in (holder_task, rug_task, onchain_task):
                if not _t.done():
                    _t.cancel()
            reason = f"price_unavailable_after_{PRICE_RETRY_ATTEMPTS}_retries"
            queue_state_update(last_veto={"ts": int(time.time()), "mint": mint, "reason": reason, "type": "VETO"})
            append_veto(
                mint,
                reason,
                source="price_check",
                historical_threshold_usd=self.current_liquidity_usd,
                current_policy_threshold_usd=self.current_liquidity_usd,
                historical_threshold_context_only=False,
            )
            queue_recent_activity(action="VETO", mint=mint)
            asyncio.create_task(self._record_shadow_watch(
                mint,
                reason,
                None,
                historical_threshold_usd=self.current_liquidity_usd,
                current_policy_threshold_usd=self.current_liquidity_usd,
            ))
            asyncio.create_task(tg_send(SNIPER_TG_TOKEN, SNIPER_TG_CHAT_ID, f"VETO {mint}: {reason}"))
            self._metrics_bump({"vetoed_price": 1, "decision_count": 1, "decision_latency_sum_ms": (time.perf_counter() - t0) * 1000}, {"vetoed_price": 1, "decision_count": 1, "decision_latency_sum_ms": (time.perf_counter() - t0) * 1000})
            # instrumentation: price unavailable veto
            self._funnel["veto_price"] += 1
            self._timing["_parse_to_decision_sum"] += (time.perf_counter() - _t_decision_start) * 1000
            self._flush_diagnostics()

            # ── MIGRATION RETRY: requeue confirmed migrations for delayed price lookup ──
            # Every candidate reaching this point passed is_migration_signature (real Raydium
            # program ID in logs).  Price fails due to Jupiter/DexScreener indexing lag.
            # Retry at 30s, 60s, 90s when the pool should be indexed.
            if MIGRATION_RETRY_ENABLED and mint not in self.trades:
                try:
                    first_delay = MIGRATION_RETRY_DELAYS_SECONDS[0] if MIGRATION_RETRY_DELAYS_SECONDS else 30.0
                    self._migration_retry_queue.put_nowait({
                        "mint": mint,
                        "attempt": 0,
                        "retry_at": time.time() + first_delay,
                        "first_ts": time.time(),
                    })
                    self._funnel["migration_retry_enqueued"] = self._funnel.get("migration_retry_enqueued", 0) + 1
                    queue_telemetry_event(
                        "MIGRATION_RETRY_ENQUEUED", mint=mint,
                        detail=f"first_retry_in={first_delay}s queue_size={self._migration_retry_queue.qsize()}",
                        severity="info",
                    )
                except asyncio.QueueFull:
                    queue_telemetry_event("MIGRATION_RETRY_QUEUE_FULL", mint=mint, severity="warn")
            return

        t_rug_start = time.perf_counter()
        # P3-1: rug_task and onchain_task were launched in parallel with price/mutable/holder above.
        # Just await the result here — no new tasks created.
        try:
            ok, reason = await rug_task
        except asyncio.TimeoutError:
            ok, reason = False, "rug_check_unavailable"
            queue_telemetry_event("DEPENDENCY_PRIMARY_BUDGET_TIMEOUT", mint=mint, detail=f"budget_s={DEPENDENCY_PRIMARY_BUDGET_SECONDS}", severity="warn")
        except Exception:
            ok, reason = False, "rug_check_unavailable"
        rugcheck_primary_ms = (time.perf_counter() - t_rug_start) * 1000
        self._timing["_rug_ms_sum"] += rugcheck_primary_ms  # P5-1
        fallback_ms = 0.0
        if not ok:
            # P2-1: On-chain authority fast pass — applies when rugcheck is unavailable
            # OR when lp_pct_threshold policy gets "Liquidity not locked" (indexing lag).
            # If authorities are revoked on-chain, the token is safe even without LP lock data.
            _try_onchain_bypass = (
                reason == "rug_check_unavailable"
                or (LOCK_POLICY == "lp_pct_threshold" and "liquidity" in reason.lower() and "not locked" in reason.lower())
            )
            if _try_onchain_bypass:
                # Fast-path: if on-chain authorities are already revoked, skip the retry queue
                _t_onchain = time.perf_counter()
                try:
                    onchain_veto, onchain_reason = await asyncio.wait_for(onchain_task, timeout=ONCHAIN_VETO_TIMEOUT_SECONDS)
                except (asyncio.TimeoutError, Exception):
                    onchain_veto, onchain_reason = True, "onchain_timeout"
                self._timing["_onchain_ms_sum"] += (time.perf_counter() - _t_onchain) * 1000  # P5-1
                if not onchain_veto:
                    # Authorities revoked on-chain — safe to proceed without rugcheck
                    ok, reason = True, f"onchain_fast_pass:{onchain_reason}"
                    queue_telemetry_event("ONCHAIN_FAST_PASS", mint=mint, detail=f"{onchain_reason} original_reason={reason}", severity="info")
                else:
                    if not onchain_task.done():
                        onchain_task.cancel()
                    if reason == "rug_check_unavailable":
                        self._queue_dependency_retry(mint, live_price, reason, attempts=1)
                        queue_telemetry_event("DEPENDENCY_UNAVAILABLE", mint=mint, detail=f"rugcheck_primary_unavailable onchain_veto={onchain_reason}", severity="warn")
                        # instrumentation: dependency unavailable veto
                        self._funnel["veto_dependency"] += 1
                        self._terminal_blockers["dependency_unavailable"] += 1
                        self._timing["_parse_to_decision_sum"] += (time.perf_counter() - _t_decision_start) * 1000
                        self._flush_diagnostics()
                        asyncio.create_task(asyncio.to_thread(append_recent_activity, action="DEPENDENCY WAIT", mint=mint, live_pnl=0.0))
                        return
                    # else: liquidity not locked + onchain failed → fall through to normal veto
            if not ok:
                # P0-1: RugCheck indexing lag — re-queue for delayed retry instead of hard-veto.
                # The pool was just created; RugCheck hasn't indexed LP lock data yet.
                if "liquidity_lock_indexing_lag" in reason and MIGRATION_RETRY_ENABLED and mint not in self.trades:
                    if not holder_task.done():
                        holder_task.cancel()
                    try:
                        _lag_delay = 30.0  # seconds — RugCheck typically indexes within 30-45s
                        self._migration_retry_queue.put_nowait({
                            "mint": mint,
                            "attempt": 0,
                            "retry_at": time.time() + _lag_delay,
                            "first_ts": time.time(),
                            "source": "indexing_lag_retry",
                        })
                        self._funnel["indexing_lag_requeued"] = self._funnel.get("indexing_lag_requeued", 0) + 1
                        queue_telemetry_event(
                            "INDEXING_LAG_REQUEUED", mint=mint,
                            detail=f"reason={reason} retry_in={_lag_delay}s",
                            severity="info",
                        )
                        log.info("[P0-1] mint=%s liquidity_lock_indexing_lag — re-queued for %.0fs delayed retry", mint, _lag_delay)
                    except asyncio.QueueFull:
                        queue_telemetry_event("INDEXING_LAG_QUEUE_FULL", mint=mint, severity="warn")
                    self._timing["_parse_to_decision_sum"] += (time.perf_counter() - _t_decision_start) * 1000
                    self._flush_diagnostics()
                    return

                if not holder_task.done():
                    holder_task.cancel()
                queue_state_update(last_veto={"ts": int(time.time()), "mint": mint, "reason": reason, "type": "VETO"})
                append_veto(
                    mint,
                    reason,
                    source="rugcheck",
                    historical_threshold_usd=self.current_liquidity_usd,
                    current_policy_threshold_usd=self.current_liquidity_usd,
                    historical_threshold_context_only=False,
                )
                queue_recent_activity(action="VETO", mint=mint)
                asyncio.create_task(self._record_shadow_watch(
                    mint,
                    reason,
                    live_price,
                    historical_threshold_usd=self.current_liquidity_usd,
                    current_policy_threshold_usd=self.current_liquidity_usd,
                ))
                asyncio.create_task(tg_send(SNIPER_TG_TOKEN, SNIPER_TG_CHAT_ID, f"VETO {mint}: {reason}"))
                key = "vetoed_liquidity" if "liquidity" in reason.lower() else "vetoed_price"
                self._metrics_bump({key: 1, "decision_count": 1, "decision_latency_sum_ms": (time.perf_counter() - t0) * 1000}, {key: 1, "decision_count": 1, "decision_latency_sum_ms": (time.perf_counter() - t0) * 1000})
                # instrumentation: liquidity/price veto from rugcheck, mirror into funnel
                if "liquidity" in reason.lower():
                    self._funnel["veto_liquidity"] += 1
                    self._terminal_blockers["liquidity_unlocked"] += 1
                else:
                    self._funnel["veto_other"] += 1
                    self._terminal_blockers["other"] += 1
                self._timing["_parse_to_decision_sum"] += (time.perf_counter() - _t_decision_start) * 1000
                self._flush_diagnostics()
                return

        # Cancel on-chain task if it's still running (rugcheck passed — no longer needed)
        if not onchain_task.done():
            onchain_task.cancel()

        # Holder concentration check (non-blocking: already running in background)
        if HOLDER_CHECK_ENABLED:
            _t_holder = time.perf_counter()
            try:
                holder_veto, holder_reason = await asyncio.wait_for(holder_task, timeout=HOLDER_CHECK_TIMEOUT_SECONDS)
            except (asyncio.TimeoutError, Exception):
                holder_veto, holder_reason = False, "holder_check_timeout"
            self._timing["_holder_ms_sum"] += (time.perf_counter() - _t_holder) * 1000  # P5-1
            if holder_veto:
                queue_state_update(last_veto={"ts": int(time.time()), "mint": mint, "reason": holder_reason, "type": "VETO"})
                append_veto(mint, holder_reason, source="holder_check",
                    historical_threshold_usd=self.current_liquidity_usd,
                    current_policy_threshold_usd=self.current_liquidity_usd,
                    historical_threshold_context_only=False)
                queue_recent_activity(action="VETO", mint=mint)
                asyncio.create_task(tg_send(SNIPER_TG_TOKEN, SNIPER_TG_CHAT_ID, f"VETO {mint}: {holder_reason}"))
                self._funnel["veto_other"] = self._funnel.get("veto_other", 0) + 1
                self._timing["_parse_to_decision_sum"] += (time.perf_counter() - _t_decision_start) * 1000
                self._flush_diagnostics()
                return
        else:
            if not holder_task.done():
                holder_task.cancel()

        # instrumentation: rug check passed — candidate is entry eligible
        self._funnel["entry_eligible"] += 1
        self._exec_audit["execution_prechecks_passed"] += 1
        _t_exec_decision_done = time.perf_counter()  # instrumentation: timing before execution

        # instrumentation: decision-to-execution timing stale check
        _parse_to_decision_ms = (_t_exec_decision_done - _t_decision_start) * 1000
        if _parse_to_decision_ms > DIAG_STALE_DECISION_MS:
            self._timing["stale_before_decision"] += 1
        self._timing["_parse_to_decision_sum"] += _parse_to_decision_ms

        # ── DELAYED ENTRY: enqueue for later execution instead of buying now ──
        # Cache the confirmed, veto-passed migration and wait DELAYED_ENTRY_SECONDS
        # before entering. At dequeue time the retry worker checks price-still-alive.
        # This skips the initial sniper dump (15-30s) and ensures Jupiter has indexed.
        if DELAYED_ENTRY_ENABLED and mint not in self.trades:
            # Task 1: Never enqueue with reference_price=0 or None — it breaks price-alive checks.
            # If live_price is invalid, re-fetch fresh price or skip delayed entry.
            if not live_price or live_price <= 0:
                log.warning("[DELAYED_ENTRY] SKIPPED mint=%s — invalid reference_price=%s, proceeding to immediate entry", mint, live_price)
                # Fall through to immediate entry instead of queueing with bad price
            else:
                try:
                    _ts_detected = time.time()
                    self._migration_retry_queue.put_nowait({
                        "mint": mint,
                        "attempt": 0,
                        "retry_at": _ts_detected + DELAYED_ENTRY_SECONDS,
                        "first_ts": _ts_detected,
                        "reference_price": live_price,
                        "source": "delayed_entry",
                        "vetted": True,  # already passed rugcheck/mutable/holder checks
                    })
                    # Pre-fetch entry score during the 30s wait — zero exec-path latency.
                    self._kick_entry_score_task(mint, _ts_detected)
                    self._funnel["delayed_entry_enqueued"] = self._funnel.get("delayed_entry_enqueued", 0) + 1
                    # P2-2: count deferred entries separately — these are NOT failures
                    self._funnel["execution_deferred"] = self._funnel.get("execution_deferred", 0) + 1
                    queue_telemetry_event(
                        "DELAYED_ENTRY_ENQUEUED", mint=mint,
                        detail=f"delay={DELAYED_ENTRY_SECONDS}s ref_price={live_price:.10g}",
                        severity="info",
                    )
                    log.info("[DELAYED_ENTRY] Cached mint=%s ref_price=$%.10g — will re-check in %.0fs", mint, live_price, DELAYED_ENTRY_SECONDS)
                    self._timing["_parse_to_decision_sum"] += (time.perf_counter() - _t_decision_start) * 1000
                    self._flush_diagnostics()
                    return
                except asyncio.QueueFull:
                    queue_telemetry_event("DELAYED_ENTRY_QUEUE_FULL", mint=mint, severity="warn")
                    # Fall through to immediate entry
            self._timing["_parse_to_decision_sum"] += (time.perf_counter() - _t_decision_start) * 1000
            self._flush_diagnostics()
        # ── END DELAYED ENTRY ──────────────────────────────────────────────────

        signal_ts = int(time.time())
        _candidate_age_ms = round((_t_exec_decision_done - t0) * 1000)
        # C1: extract numeric holder pct from holder_reason string
        _holder_pct = None
        if HOLDER_CHECK_ENABLED:
            import re as _re_c1
            _hm = _re_c1.search(r"(\d+)pct_top5", holder_reason)
            if _hm:
                _holder_pct = int(_hm.group(1))
        append_history({
            "type": "signal_accept", "ts": signal_ts, "mint": mint,
            "price_usd": round(live_price, 10), "mode": self.execution_mode,
            "candidate_age_ms": _candidate_age_ms, "holder_pct_top5": _holder_pct,
            # Schema-consistent telemetry nulls (immediate-entry path; no
            # pre-entry DexScreener liquidity fetch).
            "liquidity_usd": None, "market_cap_usd": None, "fdv_usd": None,
            "price_usd_at_signal": round(live_price, 10),
            "price_usd_at_entry_quote": None,
            "price_change_m5_pct": None, "price_change_h1_pct": None,
            "volume_m5_usd": None, "volume_h1_usd": None,
            "buy_count_m5": None, "sell_count_m5": None,
            "buy_count_h1": None, "sell_count_h1": None,
            "pair_created_at_ms": None,
            "quote_to_fill_slippage_pct": None,
            "route_available_at_signal": None,
            "route_available_at_entry": None,
            "pre_entry_structure": "unknown",
            "local_pullback_depth_pct": None,
            "local_spike_height_pct": None,
            "reference_price_is_real": None,
            "entry_price_source": None,
            "price_change_10s_pct_pre_entry": None,
            "price_change_30s_pct_pre_entry": None,
            "volume_10s_pre_entry": None,
            "volume_30s_pre_entry": None,
            "volatility_10s_pre_entry": None,
            "volatility_30s_pre_entry": None,
            "dev_holder_pct": None,
            "sniper_holder_pct": None,
            "unique_holders_count": None,
            "dev_sells_seen_pre_entry": None,
            "early_sell_pressure_ratio": None,
            "buy_count_30s_pre_entry": None,
            "sell_count_30s_pre_entry": None,
            "buy_usd_30s_pre_entry": None,
            "sell_usd_30s_pre_entry": None,
            "holder_pct_top10": None,
        })
        queue_state_update(last_signal={"mint": mint, "ts": signal_ts, "price_usd": round(live_price, 10), "mode": self.execution_mode, "status": "ready"})

        intent = await self._build_execution_intent(mint, ts_detected=time.time(), reference_price_usd=live_price)

        # ── PRE-ENTRY DRIFT GUARD ──────────────────────────────────────────────
        # Re-quote the token NOW (right before buying) and compare to the signal
        # price captured earlier. If price has already dumped, abort — we'd be
        # buying into someone else's exit.
        # P3-2: If the price lookup from the earlier step completed <2s ago, reuse it
        # with a small staleness discount instead of making a redundant API call.
        if ENTRY_DRIFT_ENABLED and live_price and live_price > 0:
            _price_age_s = time.perf_counter() - t_price_start
            if _price_age_s < 2.0:
                # Price is fresh enough — use it directly (small drift is negligible at <2s)
                fresh_px = live_price
            else:
                fresh_px = await self._token_price(mint)
            if fresh_px is not None and fresh_px > 0:
                drift_pct = (live_price - fresh_px) / live_price
                if drift_pct >= ENTRY_DRIFT_MAX_DROP_PCT:
                    log.warning(
                        "[ENTRY_DRIFT] REJECTED mint=%s signal_price=%.10g fresh_price=%.10g drift=%.1f%% threshold=%.1f%%",
                        mint, live_price, fresh_px, drift_pct * 100, ENTRY_DRIFT_MAX_DROP_PCT * 100,
                    )
                    append_telemetry_event(
                        "ENTRY_DRIFT_REJECTED", mint=mint,
                        detail=f"signal={live_price:.10g} fresh={fresh_px:.10g} drift={drift_pct*100:.1f}%",
                        severity="warn",
                    )
                    self._funnel["entry_drift_rejected"] = self._funnel.get("entry_drift_rejected", 0) + 1
                    self._flush_diagnostics()
                    return
                # Update intent with fresher price so entry accounting is more accurate
                intent.reference_price_usd = fresh_px
        # ── END PRE-ENTRY DRIFT GUARD ──────────────────────────────────────────

        # ── PRE-ENTRY SWAP ROUTE GATE (primary detection path) ───────────────
        # New Raydium pools take 30-90s to be indexed by Jupiter's aggregator.
        # At T=0 (primary detection), the route check almost always fails and
        # the candidate is requeued to migration_retry anyway — wasting ~1.4s of
        # HTTP latency on a call that never succeeds.
        #
        # The real route gate lives in _migration_retry_worker (T+30s), where
        # Jupiter has had time to index the pool and the check actually succeeds.
        # Skip the check here and send directly to migration_retry — the route
        # guard is enforced before execution, just 30s later.
        if ENTRY_SWAP_ROUTE_CHECK_ENABLED and MIGRATION_RETRY_ENABLED and mint not in self.trades:
            try:
                first_delay = MIGRATION_RETRY_DELAYS_SECONDS[0] if MIGRATION_RETRY_DELAYS_SECONDS else 30.0
                _ts_detected = time.time()
                # P1.11: validate reference_price before enqueue. None/<=0 corrupts the
                # price-still-alive check at dequeue; omit the key so delayed_entry falls
                # back to re-fetching live_price instead of using a bogus baseline.
                _ref_px = intent.reference_price_usd
                _queue_item: dict = {
                    "mint": mint,
                    "attempt": 0,
                    "retry_at": _ts_detected + first_delay,
                    "first_ts": _ts_detected,
                    "source": "delayed_entry",
                    "vetted": True,
                }
                if _ref_px is not None and float(_ref_px) > 0:
                    _queue_item["reference_price"] = float(_ref_px)
                self._migration_retry_queue.put_nowait(_queue_item)
                # Pre-fetch entry score during the 30s wait — zero exec-path latency.
                self._kick_entry_score_task(mint, _ts_detected)
                _ref_str = f"{float(_ref_px):.10g}" if (_ref_px is not None and float(_ref_px) > 0) else "none"
                queue_telemetry_event(
                    "DELAYED_ENTRY_ENQUEUED", mint=mint,
                    detail=f"delay={first_delay}s ref_price={_ref_str} reason=route_gate_deferred",
                    severity="info",
                )
            except asyncio.QueueFull:
                # P3.8: emit explicit telemetry so a saturated queue shows up in
                # dashboards rather than just bumping the entry_no_swap_route
                # funnel (which conflates with real missing-route vetoes).
                self._funnel["entry_no_swap_route"] = self._funnel.get("entry_no_swap_route", 0) + 1
                self._funnel["migration_retry_queue_full"] = self._funnel.get("migration_retry_queue_full", 0) + 1
                queue_telemetry_event(
                    "MIGRATION_RETRY_QUEUE_FULL", mint=mint,
                    detail="path=route_gate_deferred_delayed_entry",
                    severity="warn",
                )
            self._flush_diagnostics()
            return
        elif ENTRY_SWAP_ROUTE_CHECK_ENABLED:
            # MIGRATION_RETRY disabled or already in trades: fall through to immediate execution
            # with a fast route check (1s timeout — if Jupiter doesn't respond in 1s at T=0, skip)
            _t_route_check = time.perf_counter()
            route_ok = await self._pre_entry_swap_route_check(mint, intent.reference_price_usd)
            _route_check_ms = (time.perf_counter() - _t_route_check) * 1000
            if not route_ok:
                log.warning("[SWAP_ROUTE_GATE] mint=%s no route (%.0fms) — discarding (retry disabled)", mint, _route_check_ms)
                self._funnel["entry_no_swap_route"] = self._funnel.get("entry_no_swap_route", 0) + 1
                self._flush_diagnostics()
                return
            log.info("[SWAP_ROUTE_GATE] OK mint=%s route_check_ms=%.0f", mint, _route_check_ms)

            # P3.4: surge-ratio check on the immediate-entry fallback path.
            # The retry-worker path at _migration_retry_worker already applies
            # PRE_ENTRY_SURGE_RATIO_MAX via its pre-entry quote (~line 5090),
            # but the immediate path bypassed it whenever MIGRATION_RETRY_ENABLED
            # is false or the mint is already in self.trades. Without this
            # guard, a >2× reference-price spike would be entered at the peak.
            _ref_px = intent.reference_price_usd
            if _ref_px is not None and float(_ref_px) > 0 and float(intent.max_cost_lamports) > 0:
                _ie_sol_usd = await self._sol_usd()
                if _ie_sol_usd > 0:
                    try:
                        _ie_quote = await get_jupiter_quote(
                            input_mint="So11111111111111111111111111111111111111112",
                            output_mint=mint,
                            amount=int(intent.max_cost_lamports),
                            slippage_bps=int(intent.max_slippage_pct * 10_000),
                            priority=True,
                            skip_throttle=True,
                        )
                    except Exception as _ie_qexc:
                        log.warning("[PRE_ENTRY_SURGE_CHECK] mint=%s quote_error=%s — proceeding without surge guard", mint, _ie_qexc)
                        _ie_quote = None
                    if _ie_quote is not None:
                        _ie_out = int(_ie_quote.get("outAmount", 0))
                        if _ie_out > 0:
                            _ie_fill_px = (int(intent.max_cost_lamports) / 1e9 * _ie_sol_usd) / (_ie_out / 10 ** 6)
                            _ie_ratio = _ie_fill_px / float(_ref_px)
                            if _ie_ratio > PRE_ENTRY_SURGE_RATIO_MAX:
                                log.warning(
                                    "[PRE_ENTRY_PRICE_SURGE] mint=%s fill_px=%.10g ref_px=%.10g ratio=%.2fx — rejecting (immediate path)",
                                    mint, _ie_fill_px, float(_ref_px), _ie_ratio,
                                )
                                queue_telemetry_event(
                                    "PRE_ENTRY_PRICE_SURGE", mint=mint,
                                    detail=f"path=immediate fill_px={_ie_fill_px:.6g} ref_px={float(_ref_px):.6g} ratio={_ie_ratio:.2f}x",
                                    severity="warn",
                                )
                                self._flush_diagnostics()
                                return
                            intent.cached_quote = _ie_quote  # reuse in adapter
        # ── END PRE-ENTRY SWAP ROUTE GATE ─────────────────────────────────────

        # instrumentation: execution timing
        _t_exec_start = time.perf_counter()
        _decision_to_exec_ms = (_t_exec_start - _t_exec_decision_done) * 1000
        if _decision_to_exec_ms > DIAG_STALE_EXECUTION_MS:
            self._timing["stale_before_execution"] += 1
        self._timing["_decision_to_exec_sum"] += _decision_to_exec_ms

        # P3.5: enforce ENTRY_SCORE_THRESHOLD on the immediate-entry path. The
        # retry worker already gates on this at ~line 4836 for delayed-entry
        # and migration-retry; without this block, a mint that falls through
        # here (MIGRATION_RETRY disabled, queue_full, or already in trades)
        # would execute with no score gate — breaking the "scored on every
        # entry" invariant that the validation runway depends on.
        if ENTRY_SCORE_ENABLED:
            self._kick_entry_score_task(mint, float(intent.ts_detected or time.time()))
            _score_task_ie = self._migration_scores.get(mint)
            if _score_task_ie is not None:
                try:
                    _partial_score = await asyncio.wait_for(asyncio.shield(_score_task_ie), timeout=3.0)
                except (asyncio.TimeoutError, Exception) as _score_exc:
                    _partial_score = None
                    log.warning("[ENTRY_SCORE] immediate-path score unavailable mint=%s err=%s — proceeding without gate", mint, _score_exc)
                if _partial_score is not None:
                    _delay_s = time.time() - float(intent.ts_detected or time.time())
                    _ie_score = self._finalize_entry_score(
                        _partial_score, dex_price_verified=True, holder_reason=None, delay_s=_delay_s,
                    )
                    _ie_total = int(_ie_score.get("total") or 0)
                    if _ie_total < ENTRY_SCORE_THRESHOLD:
                        log.warning(
                            "[ENTRY_SCORE_VETO] immediate-path mint=%s total=%d/%d — blocking entry",
                            mint, _ie_total, ENTRY_SCORE_THRESHOLD,
                        )
                        queue_telemetry_event(
                            "ENTRY_SCORE_VETO", mint=mint,
                            detail=f"path=immediate total={_ie_total}/{ENTRY_SCORE_THRESHOLD}",
                            severity="warn",
                        )
                        self._funnel["entry_score_vetoed"] = self._funnel.get("entry_score_vetoed", 0) + 1
                        self._migration_scores.pop(mint, None)
                        self._flush_diagnostics()
                        return
                    self._funnel["entry_score_passed"] = self._funnel.get("entry_score_passed", 0) + 1

        # instrumentation: execution attempted
        self._funnel["execution_attempted"] += 1
        self._exec_audit["execution_attempts"] += 1
        if self.execution_mode == "live":
            self._exec_audit["live_submit_attempts"] += 1

        if self.execution_mode == "live":
            self.live_trade_attempts += 1
            queue_state_update(live_trade_attempts=self.live_trade_attempts)
            result = await self.live_adapter.execute(intent)
        else:
            result = await self.paper_adapter.execute(intent)

        # instrumentation: execution result
        if result.ok:
            # P1-1: stamp detection wall-clock on the new trade so _finalize_trade_close can
            # compute opportunity_age_ms (detect → entry latency) without a history scan.
            # 2-3: also stamp decision_id so trade_close carries causal identity for replay.
            if mint in self.trades:
                if float(intent.ts_detected or 0.0) > 0:
                    self.trades[mint].detect_ts_wall = float(intent.ts_detected)
                if intent.decision_id:
                    self.trades[mint].decision_id = intent.decision_id
            self._funnel["execution_succeeded"] += 1
            if self.execution_mode == "live":
                self._exec_audit["live_submit_success"] += 1
                # live_tx_verified_* set by _background_verify_live_buy after deferred verify
                self._exec_audit["last_tx_verify_state"] = "pending_background_verify"
                self._exec_audit["last_tx_verify_err"] = None
            self._exec_audit["last_tx_signature"] = result.tx_sig
            if result.latency_ms:
                self._exec_audit["_submit_latency_sum"] += float(result.latency_ms)
                self._exec_audit["_submit_count"] += 1
                _cnt = max(1, self._exec_audit["_submit_count"])
                self._exec_audit["avg_submit_latency_ms"] = round(self._exec_audit["_submit_latency_sum"] / _cnt, 2)
        else:
            self._funnel["execution_failed"] += 1
            if self.execution_mode == "live":
                self._exec_audit["live_submit_failures"] += 1
                err = result.error or ""
                if err.startswith("tx_failed_on_chain:"):
                    self._exec_audit["live_tx_verified_failure"] += 1
                    self._exec_audit["last_tx_verify_state"] = "confirmed_failure"
                    self._exec_audit["last_tx_verify_err"] = err[len("tx_failed_on_chain:"):]
                elif err == "tx_verification_timeout":
                    self._exec_audit["live_tx_verification_timeout"] += 1
                    self._exec_audit["last_tx_verify_state"] = "verification_timeout"
                    self._exec_audit["last_tx_verify_err"] = None
                # Also update last_tx_signature for failed txs where sig was returned
                if result.tx_sig:
                    self._exec_audit["last_tx_signature"] = result.tx_sig
            self._terminal_blockers["execution_failed"] += 1
            self._exec_audit["last_failure_reason"] = result.error
        self._flush_diagnostics()

        log.info(
            "mode=%s decision_id=%s mint=%s cu_price=%s cu_limit=%s jito_tip=%s priority_tier=%s tx_sig=%s cost_to_trade=%s status=%s err=%s ix_order=%s",
            result.mode,
            intent.decision_id,
            mint,
            result.cu_price,
            result.cu_limit,
            result.jito_tip_lamports,
            result.priority_tier,
            result.tx_sig,
            result.cost_to_trade_usd,
            "ok" if result.ok else "error",
            result.error,
            ",".join(result.extra.get("ix_order", [])),
        )

        total_elapsed_ms = (time.perf_counter() - t0) * 1000
        finalize_budget_ms = max(0.0, total_elapsed_ms - fast_veto_checks_ms - metadata_lookup_ms - price_lookup_ms - rugcheck_primary_ms - float(result.latency_ms or 0.0))
        queue_depth = int(getattr(self.event_queue, 'qsize', lambda: 0)() or 0)
        queue_pressure_relief_ms = min(finalize_budget_ms, float(queue_depth) * 35.0)
        serial_gap_ms = max(0.0, finalize_budget_ms - queue_pressure_relief_ms)
        await self._post_execution(
            mint,
            intent,
            result,
            t0=t0,
            source="primary",
            decision_breakdown={
                "price_meta_rug_ms": round(metadata_lookup_ms + price_lookup_ms + rugcheck_primary_ms, 4),
                "rpc_wait_ms": round(float(daily_metrics.get("mint_parse_gettx_wait_sum_ms", 0.0) or 0.0) / max(1, int(daily_metrics.get("mint_parse_successes", 0) or 0)), 4) if (daily_metrics := read_state().get("daily_metrics") or {}) else 0.0,
                "metadata_lookup_ms": round(metadata_lookup_ms, 4),
                "price_lookup_ms": round(price_lookup_ms, 4),
                "rugcheck_primary_ms": round(rugcheck_primary_ms, 4),
                "fallback_ms": round(fallback_ms, 4),
                "fast_veto_checks_ms": round(fast_veto_checks_ms, 4),
                "serial_gap_ms": round(serial_gap_ms, 4),
                "rug_check_ms": round(rugcheck_primary_ms + fallback_ms, 4),
                "decision_finalize_ms": round(finalize_budget_ms, 4),
                "holder_pct_top5": _holder_pct,
            },
        )

    def _write_metrics_snapshot(self) -> None:
        """Write a JSONL snapshot of all positions + session metrics every N seconds.

        Each line is one complete snapshot at a point in time. Used by
        review_trades.py to show price/ROI timelines alongside trade closes.
        Fields capture everything needed to reconstruct what happened:
        entry, current mark, ROI, MFE/MAE, stop level, mark staleness, funnel.
        """
        now = time.time()
        if now - self._last_snapshot_ts < METRICS_SNAPSHOT_INTERVAL_SECONDS:
            return
        self._last_snapshot_ts = now

        positions = []
        for tr in self._trades_snapshot():
            if tr.closed:
                continue
            entry_px = float(tr.entry_price or tr.entry_px or 0.0)
            current_px = float(tr.current_px or entry_px)
            qty_remaining = float(tr.qty_remaining or 0.0)
            remaining_basis = float(tr.remaining_basis_usd or 0.0)
            unrealized_usd = round((current_px - entry_px) * qty_remaining, 6) if entry_px > 0 else 0.0
            hold_s = round(max(0.0, now - float(tr.entry_ts or tr.ts or now)), 2)
            positions.append({
                "mint": tr.mint,
                "entry_px": round(entry_px, 12),
                "current_px": round(current_px, 12),
                "mark_source": tr.mark_price_source or "unknown",
                "mark_confidence": tr.mark_confidence_level or "LOW",
                "roi_pct": round(tr.unrealized_roi_pct, 4),
                "unrealized_usd": unrealized_usd,
                "qty_remaining": round(qty_remaining, 4),
                "qty_total": round(float(tr.qty_total or 0.0), 4),
                "remaining_basis_usd": round(remaining_basis, 6),
                "hold_s": hold_s,
                "mfe_pct": round(float(tr.mfe_pct or 0.0), 4),
                "mae_pct": round(float(tr.mae_pct or 0.0), 4),
                "mark_stale": bool(tr.mark_stale),
                "mark_staleness_s": round(float(tr.mark_staleness_seconds or 0.0), 1),
                "exec_mark_observations": int(tr.executable_mark_observations or 0),
                "total_mark_observations": int(tr.total_mark_observations or 0),
                "mark_source_sequence": str(tr.mark_source_sequence or ""),
                "stage1_done": bool(tr.stage1_done),
                "stage2_done": bool(tr.stage2_done),
                "stage3_done": bool(tr.stage3_done),
                "stop_px": round(float(tr.stop_px or 0.0), 12),
                "entry_ts": float(tr.entry_ts or tr.ts or 0.0),
                "size_usd": round(float(tr.size_usd or 0.0), 6),
            })

        daily_metrics = read_state().get("daily_metrics") or {}
        snapshot = {
            "type": "metrics_snapshot",
            "ts": round(now, 3),
            "positions": positions,
            "session": {
                "open_positions": len(positions),
                "total_unrealized_usd": round(sum(p["unrealized_usd"] for p in positions), 6),
                "budget_usd": round(float(self.budget), 6),
                "execution_mode": self.execution_mode,
                "closed_wins": int(daily_metrics.get("closed_wins") or 0),
                "closed_losses": int(daily_metrics.get("closed_losses") or 0),
                "realized_pnl_usd": round(float(daily_metrics.get("realized_pnl_usd") or 0.0), 6),
                "quoteability_guard_exits": int(daily_metrics.get("quoteability_guard_exits") or 0),
            },
            "funnel": {
                "entry_no_swap_route": int(self._funnel.get("entry_no_swap_route") or 0),
                "entry_drift_rejected": int(self._funnel.get("entry_drift_rejected") or 0),
                "execution_succeeded": int(self._funnel.get("execution_succeeded") or 0),
                "execution_failed": int(self._funnel.get("execution_failed") or 0),
                "qualified_candidates": int(self._funnel.get("qualified_candidates") or 0),
            },
        }
        try:
            _rotate_metrics_snapshot_if_needed()
            with open(METRICS_SNAPSHOT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot) + "\n")
        except Exception as e:
            log.warning("[SNAPSHOT] write failed: %s", e)

    async def monitor_positions_loop(self):
        while True:
            await self._process_operator_commands()
            # C2: collect stop-exit sells for parallel execution instead of serial 3s waits
            _deferred_stop_exits: list[tuple] = []
            for tr in self._trades_snapshot():
                if tr.closed:
                    continue
                if self._terminalize_trade(tr):
                    continue
                # ── GHOST_PENDING: skip all exit/stop/trail/HARD_CAP evaluation ──
                # Wallet hasn't confirmed tokens yet — marks are stale/sentinel and
                # would otherwise trip TRAIL/SL/HARD_CAP against non-existent inventory.
                # _ghost_pending_recheck owns the lifecycle: either clears to OPEN
                # (tokens landed late) or finalizes as GHOST_BUY_NO_TOKENS.
                if tr.position_truth_state == "GHOST_PENDING":
                    continue
                # ── Sell verification poll in progress ────────────────────────
                # A stop-exit TX timed out verification; _sell_verify_poll is polling
                # the wallet in the background. Do not trigger new exits or trailing
                # logic until the poll resolves (max 60s).
                if float(tr.sell_pending_verify_ts or 0.0) > 0.0:
                    continue
                # ── Sentinel entry_px — exits BLOCKED until first executable mark ──
                # entry_px=1e-12 means fill+dex+reference were all unavailable at buy;
                # any exit math (TP +30% → 1.3e-12, trail, HARD_CAP) would fire against a
                # garbage baseline. _refresh_trade_mark rewrites entry_px and clears this
                # flag on the first executable mark.
                if getattr(tr, "entry_px_sentinel", False):
                    continue
                # ── HARD CAPITAL CAP (bypass everything) ──────────────────────
                # If mark price shows >= MAX_POSITION_LOSS_PCT loss, force-sell
                # immediately. Bypasses: quoteability guard, trailing stop, delay
                # logic, stale mark check. This is pure risk containment.
                #
                # Guards (bug-fix):
                #   1. position_age_s >= 5.0  — tokens need time to settle post-buy TX;
                #      firing at T+1.3s hits wallet_raw=0 → ghost close → -106% ROI
                #   2. executable_mark_observations >= 1  — entry_px itself is the first
                #      _cur_px value; without a live exec mark the 20% loss comparison
                #      is entry_px vs a stale pool spot, not a real observable price
                _entry_px = float(tr.entry_px or 0.0)
                _cur_px = float(tr.current_px or tr.last_price or 0.0)
                _hcap_age = max(0.0, time.time() - float(tr.entry_ts or tr.ts or time.time()))
                _hcap_exec_marks = int(tr.executable_mark_observations or 0)
                if _entry_px > 0 and _cur_px > 0:
                    _loss_pct = (_entry_px - _cur_px) / _entry_px
                    if _loss_pct >= MAX_POSITION_LOSS_PCT and float(tr.qty_remaining or 0.0) > 1e-9:
                        # P1.9 stale-mark guard: HARD_CAP fires against _cur_px. If the mark
                        # is stale (mark_stale=True OR staleness_seconds >= 10), _loss_pct is
                        # computed from an outdated price — the position may already be flat
                        # or winning. Defer until a fresh mark arrives; STALE_MARK_TIMEOUT_EXIT
                        # still owns true unquoteable positions at 45s.
                        _mark_stale_flag = bool(getattr(tr, "mark_stale", False))
                        _mark_stale_secs = float(getattr(tr, "mark_staleness_seconds", 0.0) or 0.0)
                        if _mark_stale_flag or _mark_stale_secs >= 10.0:
                            log.warning(
                                "[HARD_CAP] mint=%s LOSS=%.1f%% DEFERRED — mark stale (flag=%s secs=%.1f)",
                                tr.mint, _loss_pct * 100, _mark_stale_flag, _mark_stale_secs,
                            )
                            append_telemetry_event(
                                "HARD_CAP_STALE_MARK_DEFER", mint=tr.mint,
                                detail=f"loss={_loss_pct*100:.1f}% stale={_mark_stale_flag} secs={_mark_stale_secs:.1f}",
                                severity="warn",
                            )
                            continue
                        # Catastrophic dump path: loss >= 35% → fire immediately, no guards.
                        # At this depth waiting only compounds losses; ghost risk is secondary.
                        _catastrophic = _loss_pct >= 0.35
                        # Fast-rug path: loss >= cap AND age < 20s AND exec_marks <= 5.
                        # These are tokens that dumped immediately post-fill — not recoverable.
                        # The background verify window (T+5-8s) is a ghost risk, but at age
                        # >= 10s with exec_marks > 0 we have confirmed price data: fire now.
                        _fast_rug = _hcap_age >= 10.0 and _hcap_age < 20.0 and _hcap_exec_marks <= 5
                        _guards_met = _hcap_age >= 10.0 and _hcap_exec_marks >= 1
                        if not _catastrophic and not _fast_rug and not _guards_met:
                            # Not yet safe to fire: background token verify completes at
                            # T+5-8s; require age>=10s to ensure ghost removal has run.
                            # Also need at least one executable mark (real observable price).
                            # Re-evaluated on the next loop tick.
                            log.warning(
                                "[HARD_CAP] mint=%s LOSS=%.1f%% but guards not met "
                                "(age=%.1fs exec_marks=%d) — deferring",
                                tr.mint, _loss_pct * 100, _hcap_age, _hcap_exec_marks,
                            )
                            append_telemetry_event(
                                "HARD_CAP_DEFERRED", mint=tr.mint,
                                detail=f"loss={_loss_pct*100:.1f}% age={_hcap_age:.1f}s exec_marks={_hcap_exec_marks}",
                                severity="warn",
                            )
                        else:
                            _trigger = "catastrophic_dump" if _catastrophic else "fast_rug" if _fast_rug else "normal"
                            append_telemetry_event(
                                "HARD_CAPITAL_CAP_EXIT", mint=tr.mint,
                                detail=f"loss={_loss_pct*100:.1f}% cap={MAX_POSITION_LOSS_PCT*100:.0f}% entry={_entry_px:.10g} cur={_cur_px:.10g} age={_hcap_age:.1f}s exec_marks={_hcap_exec_marks} trigger={_trigger}",
                                severity="error",
                            )
                            log.error("[HARD_CAP] mint=%s LOSS=%.1f%% >= cap=%.0f%% age=%.1fs exec_marks=%d trigger=%s — force-selling NOW", tr.mint, _loss_pct * 100, MAX_POSITION_LOSS_PCT * 100, _hcap_age, _hcap_exec_marks, _trigger)
                            tr.stop_trigger_ts = time.time()
                            _deferred_stop_exits.append((tr, 1.0, "HARD_CAP_EXIT"))
                        continue
                # ── END HARD CAPITAL CAP ──────────────────────────────────────
                # ── BLIND NEGATIVE EXIT ───────────────────────────────────────
                # If we have ZERO mark observations AND the position is losing,
                # exit immediately. We cannot manage what we cannot observe.
                # Only applies when position is confirmed negative (entry_px known).
                _blind_total_marks = int(tr.total_mark_observations or 0)
                _blind_age = max(0.0, time.time() - float(tr.entry_ts or tr.ts or time.time()))
                if (_blind_total_marks == 0 and _blind_age >= 20.0
                        and _entry_px > 0 and _cur_px > 0 and _cur_px < _entry_px
                        and float(tr.qty_remaining or 0.0) > 1e-9):
                    append_telemetry_event(
                        "BLIND_NEGATIVE_EXIT", mint=tr.mint,
                        detail=f"age={_blind_age:.0f}s total_marks=0 entry={_entry_px:.10g} cur={_cur_px:.10g}",
                        severity="error",
                    )
                    log.error("[BLIND_NEG_EXIT] mint=%s age=%.0fs NO price data + negative — force-selling", tr.mint, _blind_age)
                    tr.stop_trigger_ts = time.time()
                    _deferred_stop_exits.append((tr, 1.0, "BLIND_NEGATIVE_EXIT"))
                    continue
                # ── END BLIND NEGATIVE EXIT ───────────────────────────────────
                # ── C3: TTL age warning (once per position at 50% threshold) ──
                _pos_age_c3 = max(0.0, time.time() - float(tr.entry_ts or tr.ts or time.time()))
                _ttl_warn_threshold = MAX_POSITION_AGE_SECONDS * 0.5
                if _pos_age_c3 >= _ttl_warn_threshold and not getattr(tr, '_ttl_warn_logged', False):
                    log.warning("[POSITION_AGE_WARNING] mint=%s age=%.0fs — approaching TTL (%ds)", tr.mint, _pos_age_c3, MAX_POSITION_AGE_SECONDS)
                    tr._ttl_warn_logged = True
                # ── TTL EXIT (max position age) ───────────────────────────────
                # Force-close any position held beyond MAX_POSITION_AGE_SECONDS
                # unless it is actively profitable above TTL_EXIT_MIN_PROGRESS_PCT.
                # Prevents indefinitely-held stuck positions that bleed capital via
                # sporadic DexScreener marks keeping other exit paths from firing.
                if TTL_EXIT_ENABLED and float(tr.qty_remaining or 0.0) > 1e-9:
                    _ttl_age = max(0.0, time.time() - float(tr.entry_ts or tr.ts or time.time()))
                    if _ttl_age >= MAX_POSITION_AGE_SECONDS:
                        _ttl_roi_pct = ((tr.current_px - tr.entry_px) / tr.entry_px * 100.0) if tr.entry_px > 0 else 0.0
                        if _ttl_roi_pct < TTL_EXIT_MIN_PROGRESS_PCT:
                            log.warning(
                                "[TTL_EXIT] mint=%s age=%.0fs >= cap=%ds roi=%.1f%% < threshold=%.1f%% — force-selling",
                                tr.mint, _ttl_age, MAX_POSITION_AGE_SECONDS, _ttl_roi_pct, TTL_EXIT_MIN_PROGRESS_PCT,
                            )
                            append_telemetry_event(
                                "TTL_EXIT", mint=tr.mint,
                                detail=f"age={_ttl_age:.0f}s cap={MAX_POSITION_AGE_SECONDS}s roi={_ttl_roi_pct:.1f}% threshold={TTL_EXIT_MIN_PROGRESS_PCT:.1f}%",
                                severity="warn",
                            )
                            tr.stop_trigger_ts = time.time()
                            _deferred_stop_exits.append((tr, 1.0, "TTL_EXIT"))
                            continue
                # ── END TTL EXIT ──────────────────────────────────────────────
                # Auto-exit if the mark has been stale for too long — token is presumed dead.
                # _live_sell_gate will do a real sell if wallet holds tokens, ghost-close if not.
                position_age_s = max(0.0, time.time() - float(tr.entry_ts or tr.ts or time.time()))
                total_marks = int(tr.total_mark_observations or 0)
                exec_marks = int(tr.executable_mark_observations or 0)
                # Only fire quoteability guard if we have ZERO price observations from ANY source.
                # If DexScreener or RPC provided marks, the position is observable — don't force-exit.
                _needs_quoteability_exit = (
                    position_age_s >= QUOTEABILITY_VALIDATION_WINDOW_SECONDS
                    and total_marks < QUOTEABILITY_REQUIRED_EXEC_MARKS
                )
                _needs_stale_exit = (
                    tr.mark_stale
                    and tr.mark_staleness_seconds >= STALE_MARK_EXIT_TIMEOUT_SECONDS
                )
                # Absolute age gate: if a position has been open 5+ minutes with no executable
                # mark (exec_marks == 0), force-exit regardless of quoteability/stale state.
                # Catches the "sporadic DexScreener mark keeps stale timer low but no real
                # price discovery ever happens" failure mode (e.g. low-volume tokens).
                _needs_age_gate_exit = (
                    position_age_s >= 300.0
                    and exec_marks == 0
                    and float(tr.qty_remaining or 0.0) > 1e-9
                )
                if _needs_quoteability_exit or _needs_stale_exit or _needs_age_gate_exit:
                    if _needs_age_gate_exit and not _needs_quoteability_exit and not _needs_stale_exit:
                        log.warning(
                            "[AGE_GATE_EXIT] mint=%s age=%.0fs exec_marks=0 — no executable mark in 5min, force-exit",
                            tr.mint, position_age_s,
                        )
                        append_telemetry_event(
                            "AGE_GATE_EXIT", mint=tr.mint,
                            detail=f"age_s={position_age_s:.0f} total_marks={total_marks} exec_marks=0",
                            severity="warn",
                        )
                    if _needs_quoteability_exit:
                        # P1-3: sub-tag based on position age to distinguish indexing lag vs structural failure.
                        # transient  — fired within 2× guard window: pool likely just hasn't been indexed yet.
                        # no_pool    — fired late: pool may not exist or is permanently unreachable.
                        _q_sub = "transient" if position_age_s <= QUOTEABILITY_VALIDATION_WINDOW_SECONDS * 2.0 else "no_pool"
                        _exit_tag = f"QUOTEABILITY_GUARD_EXIT:{_q_sub}"
                    elif _needs_age_gate_exit:
                        _exit_tag = "AGE_GATE_EXIT"
                    else:
                        _exit_tag = "STALE_MARK_TIMEOUT_EXIT"
                    # ── Forced exit cooldown: don't hammer every 1s loop iteration ──────────
                    _since_last_attempt = time.time() - float(tr.forced_exit_last_attempt_ts or 0.0)
                    if tr.forced_exit_last_attempt_ts and _since_last_attempt < FORCED_EXIT_RETRY_COOLDOWN_SECONDS:
                        # Still within cooldown — let trailing/stages run so we keep tracking
                        await self._update_trailing_and_stages(tr)
                        continue
                    # ── Escalation: after N failed attempts, classify as ghost unresolved ──
                    if tr.forced_exit_attempt_count >= FORCED_EXIT_MAX_ATTEMPTS:
                        log.error(
                            "[FORCED_EXIT] mint=%s tag=%s — %d failed attempts, escalating to GHOST_UNRESOLVED",
                            tr.mint, _exit_tag, tr.forced_exit_attempt_count,
                        )
                        append_telemetry_event(
                            "FORCED_EXIT_FINAL_FAILURE", mint=tr.mint,
                            detail=f"tag={_exit_tag} attempts={tr.forced_exit_attempt_count} age_s={position_age_s:.0f} staleness_s={tr.mark_staleness_seconds:.0f}",
                            severity="critical",
                        )
                        tr.position_truth_state = "GHOST_UNRESOLVED"
                        tr.forced_exit_unresolved = True
                        self._ghost_reconciliation_terminalize(tr)
                        self._state_sync()
                        continue
                    # ── Attempt the forced exit ───────────────────────────────────────────
                    tr.forced_exit_last_attempt_ts = time.time()
                    tr.forced_exit_attempt_count += 1
                    if _needs_quoteability_exit:
                        append_telemetry_event(
                            "QUOTEABILITY_GUARD_EXIT", mint=tr.mint,
                            detail=f"age_s={position_age_s:.0f} total_marks={total_marks} exec_marks={exec_marks} source={tr.mark_price_source} attempt={tr.forced_exit_attempt_count}",
                            severity="warn",
                        )
                        self._metrics_bump({"quoteability_guard_exits": 1}, {"quoteability_guard_exits": 1})
                    else:
                        log.warning(
                            "[STALE_EXIT] mint=%s mark_staleness=%.0fs >= timeout=%ds attempt=%d — triggering auto-exit",
                            tr.mint, tr.mark_staleness_seconds, STALE_MARK_EXIT_TIMEOUT_SECONDS, tr.forced_exit_attempt_count,
                        )
                        append_telemetry_event(
                            "STALE_MARK_TIMEOUT_EXIT", mint=tr.mint,
                            detail=f"staleness={tr.mark_staleness_seconds:.0f}s timeout={STALE_MARK_EXIT_TIMEOUT_SECONDS}s attempt={tr.forced_exit_attempt_count}",
                            severity="warn",
                        )
                    append_telemetry_event(
                        "FORCED_EXIT_RETRY", mint=tr.mint,
                        detail=f"tag={_exit_tag} attempt={tr.forced_exit_attempt_count} max={FORCED_EXIT_MAX_ATTEMPTS}",
                        severity="warn",
                    )
                    tr.stop_trigger_ts = time.time()
                    await self._sell_fraction(tr, 1.0, _exit_tag)
                    if tr.closed:
                        self._finalize_trade_close(tr, _exit_tag)
                    elif not tr.closed:
                        # Sell failed — forced_exit_attempt_count already incremented
                        log.warning(
                            "[FORCED_EXIT] sell FAILED mint=%s tag=%s attempt=%d — will retry after %.0fs cooldown",
                            tr.mint, _exit_tag, tr.forced_exit_attempt_count, FORCED_EXIT_RETRY_COOLDOWN_SECONDS,
                        )
                    continue
                await self._update_trailing_and_stages(tr)
            # C2: fire all collected stop exits concurrently instead of serial 3s waits
            if _deferred_stop_exits:
                async def _exec_stop_exit(_tr, _frac, _reason):
                    await self._sell_fraction(_tr, _frac, _reason)
                    if _tr.closed:
                        self._finalize_trade_close(_tr, _reason)
                await asyncio.gather(*[_exec_stop_exit(t, f, r) for t, f, r in _deferred_stop_exits])
            if time.time() < float(self._finalize_hot_until or 0.0):
                await asyncio.sleep(1)
                continue
            self._state_sync()
            self._write_metrics_snapshot()
            rotate_veto_log_if_needed()
            await self._refresh_graveyard()
            await asyncio.sleep(1)

    async def _ws_heartbeat_loop(self, ws, sub_id: int):
        hb_id = 100000
        while True:
            await asyncio.sleep(WS_HEARTBEAT_SECONDS)
            hb_id += 1
            payload = {"jsonrpc": "2.0", "id": hb_id, "method": "ping", "params": [sub_id]}
            try:
                await ws.send(json.dumps(payload))
                self._ws_last_heartbeat_ts = int(time.time())
            except Exception:
                # Fallback to protocol ping if JSON-RPC ping is not accepted.
                pong = await ws.ping()
                await asyncio.wait_for(pong, timeout=10)
                self._ws_last_heartbeat_ts = int(time.time())

    def _ws_feed_stale_reason(self, st: dict, now_ts: int) -> Optional[str]:
        # P0-2: prefer in-memory timestamps (updated on every WS message) over
        # disk-persisted state to avoid false stale detection from flush lag.
        last_hb = self._ws_last_heartbeat_ts or int(st.get("last_ws_heartbeat_ts", 0) or 0)
        last_msg = self._ws_last_message_ts or int(st.get("last_ws_message_ts", 0) or 0)
        last_sub = self._ws_last_subscribe_ts or int(st.get("last_successful_subscribe_ts", 0) or 0)
        if last_hb and (now_ts - last_hb) > WS_STALE_HEARTBEAT_SECONDS:
            return f"heartbeat_stale:{now_ts-last_hb}s"
        if last_sub and last_msg <= last_sub and (now_ts - last_sub) > WS_STALE_CANDIDATE_SECONDS:
            return f"no_message_post_subscribe:{now_ts-last_sub}s"
        if last_msg > last_sub and (now_ts - last_msg) > max(WS_STALE_CANDIDATE_SECONDS * 2, WS_STALE_HEARTBEAT_SECONDS + 30):
            return f"message_stale:{now_ts-last_msg}s"
        return None

    def _ws_flush_cached_timestamps(self):
        """Flush in-memory WS timestamps to disk if flush interval has elapsed."""
        now = time.time()
        if now - self._ws_state_last_flush < self._ws_state_flush_interval:
            return
        self._ws_state_last_flush = now
        updates: dict = {}
        if self._ws_last_message_ts:
            updates["last_ws_message_ts"] = self._ws_last_message_ts
        if self._ws_last_candidate_ts:
            updates["last_candidate_seen_ts"] = self._ws_last_candidate_ts
        if self._ws_last_heartbeat_ts:
            updates["last_ws_heartbeat_ts"] = self._ws_last_heartbeat_ts
        if self._ws_last_subscribe_ts:
            updates["last_successful_subscribe_ts"] = self._ws_last_subscribe_ts
        if updates:
            write_state(**updates)

    def _ws_read_state_cached(self) -> dict:
        """Read runtime state with 5s caching to reduce disk I/O on WS hot path."""
        now = time.time()
        if self._ws_cached_state is not None and (now - self._ws_cached_state_ts) < self._ws_state_flush_interval:
            return self._ws_cached_state
        self._ws_cached_state = read_state()
        self._ws_cached_state_ts = now
        return self._ws_cached_state

    async def _supervised_task(self, name: str, coro_fn):
        """Supervisor wrapper: restarts a background coroutine on crash.

        Catches any unhandled exception, logs TASK_CRASHED with traceback,
        waits 5s, then relaunches. Without this, a crash in e.g.
        monitor_positions_loop silently kills all stop-loss management.
        """
        import traceback as _tb
        while True:
            try:
                await coro_fn()
            except asyncio.CancelledError:
                log.info("[SUPERVISOR] %s cancelled — not restarting", name)
                return
            except Exception as exc:
                tb_str = _tb.format_exc()
                log.error("[SUPERVISOR] TASK_CRASHED name=%s error=%s\n%s", name, exc, tb_str)
                append_telemetry_event(
                    "TASK_CRASHED", detail=f"name={name} error={exc}", severity="critical",
                )
                await asyncio.sleep(5)
                log.warning("[SUPERVISOR] restarting %s after crash", name)
                continue
            # Clean return: task is done (e.g. paper-mode early return). Do not restart —
            # restarting burns 100% CPU creating/awaiting/destroying coroutines that yield nothing.
            log.info("[SUPERVISOR] %s completed cleanly — not restarting", name)
            return

    async def run(self):
        asyncio.create_task(self._supervised_task("monitor_positions_loop", self.monitor_positions_loop))
        asyncio.create_task(self._supervised_task("live_monitor_loop", self.live_monitor_loop))
        asyncio.create_task(self._supervised_task("open_position_refresh_loop", self.open_position_refresh_loop))
        asyncio.create_task(self._supervised_task("shadow_watch_loop", self._shadow_watch_loop))
        asyncio.create_task(self._supervised_task("dependency_retry_loop", self._dependency_retry_loop))
        if MIGRATION_RETRY_ENABLED:
            asyncio.create_task(self._supervised_task("migration_retry_worker", self._migration_retry_worker))
        asyncio.create_task(self._supervised_task("startup_ghost_reconciliation", self._startup_ghost_reconciliation_task))
        asyncio.create_task(self._supervised_task("periodic_ghost_reconciliation", self._periodic_ghost_reconciliation_loop))
        asyncio.create_task(self._supervised_task("history_flush_loop", self._history_flush_loop))
        self._ensure_workers()

        if self.execution_mode == "live":
            observed = await self._fetch_priority_fee_p75()
            cu_price = max(CU_PRICE_MIN, min(CU_PRICE_MAX, int(observed)))
            cu_limit = CU_LIMIT_DEFAULT
            tip = JITO_TIP_LAMPORTS
            sol_usd = await self._sol_usd()
            priority_lamports_est = int((cu_price * cu_limit) / 1_000_000)
            cost_to_trade = ((priority_lamports_est + tip) / 1_000_000_000) * sol_usd
            write_state(
                execution_mode="live",
                cu_price=cu_price,
                cu_limit=cu_limit,
                jito_tip=tip,
                priority_tier=LiveExecutionAdapter._priority_tier(cu_price),
                cost_to_trade=round(cost_to_trade, 6),
            )
            log.info(
                "mode=live startup readiness cu_price=%s cu_limit=%s jito_tip=%s priority_tier=%s cost_to_trade=%s",
                cu_price,
                cu_limit,
                tip,
                LiveExecutionAdapter._priority_tier(cu_price),
                round(cost_to_trade, 6),
            )

        while True:
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=WS_PING_INTERVAL_SECONDS,
                    ping_timeout=WS_PING_TIMEOUT_SECONDS,
                    close_timeout=10,
                ) as ws:
                    # Subscribe to each mention key separately (supports multi-program monitoring).
                    # Default: pump.fun only.  Set LOGS_MENTION_KEYS to add more programs.
                    sub_ids = []
                    for idx_key, mention_key in enumerate(LOGS_MENTION_KEYS):
                        sub = {"jsonrpc": "2.0", "id": idx_key + 1, "method": "logsSubscribe", "params": [{"mentions": [mention_key]}, {"commitment": "processed"}]}
                        await ws.send(json.dumps(sub))
                        sub_ack_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        sub_ack = json.loads(sub_ack_raw)
                        sub_id_i = sub_ack.get("result")
                        if not sub_id_i:
                            raise RuntimeError(f"logsSubscribe failed for {mention_key}: {sub_ack}")
                        sub_ids.append(sub_id_i)
                        log.info("logsSubscribe OK: mention_key=%s sub_id=%s", mention_key, sub_id_i)
                    sub_id = sub_ids[0]  # primary sub ID for state tracking
                    now_sub_ts = int(time.time())
                    # P0-2: set in-memory timestamps immediately at subscribe time so
                    # the stale-feed watchdog doesn't false-fire during the 5s flush gap.
                    self._ws_last_subscribe_ts = now_sub_ts
                    self._ws_last_heartbeat_ts = now_sub_ts
                    self._ws_last_message_ts = now_sub_ts
                    self._ws_last_candidate_ts = 0
                    self._ws_consecutive_failures = 0  # P4-3: reset backoff on successful connect
                    write_state(
                        ws_subscription_id=sub_id,
                        ws_subscribed_at=now_sub_ts,
                        last_successful_subscribe_ts=now_sub_ts,
                        last_ws_heartbeat_ts=now_sub_ts,
                        last_ws_message_ts=now_sub_ts,
                        last_candidate_seen_ts=0,
                        execution_mode=self.execution_mode,
                    )
                    append_telemetry_event("WS_SUBSCRIBE_ACK", detail=f"sub_ids={sub_ids} mention_keys={len(LOGS_MENTION_KEYS)} ws={self.ws_url}", severity="info")

                    hb_task = asyncio.create_task(self._ws_heartbeat_loop(ws, sub_id))
                    flow_resumed_emitted = False
                    try:
                        while True:
                            # P0-2: use cached state (5s TTL) instead of reading disk every message.
                            # Killswitch delay is ≤5s — acceptable tradeoff for eliminating per-msg I/O.
                            st = self._ws_read_state_cached()
                            if st.get("killswitch"):
                                await tg_send(SNIPER_TG_TOKEN, SNIPER_TG_CHAT_ID, "Simulation halted: global killswitch=true")
                                return
                            stale_reason = self._ws_feed_stale_reason(st, int(time.time()))
                            if stale_reason:
                                append_telemetry_event("WS_STALE_FEED_DETECTED", detail=f"reason={stale_reason} ws={self.ws_url}", severity="warn")
                                try:
                                    await ws.close(code=1012, reason="stale_feed_watchdog")
                                except Exception:
                                    pass
                                raise RuntimeError(f"ws_stale_feed:{stale_reason}")
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=60)
                            except asyncio.TimeoutError:
                                pong = await ws.ping()
                                await asyncio.wait_for(pong, timeout=10)
                                continue

                            # P0-2: in-memory timestamp, flushed to disk every 5s
                            self._ws_last_message_ts = int(time.time())
                            self._ws_flush_cached_timestamps()
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                continue

                            if not self._is_candidate_event(msg):
                                continue

                            now_candidate_ts = int(time.time())
                            self._ws_last_candidate_ts = now_candidate_ts
                            if not flow_resumed_emitted:
                                append_telemetry_event("WS_FLOW_RESUMED", detail=f"ws={self.ws_url}", severity="info")
                                flow_resumed_emitted = True

                            candidate_key = self._candidate_key(msg)
                            if not await self._remember_candidate_key(candidate_key):
                                self._metrics_bump({"candidate_duplicates_deduped": 1}, {"candidate_duplicates_deduped": 1})
                                queue_telemetry_event("CANDIDATE_DUP_DEDUPED", detail=f"candidate_key={candidate_key}", severity="info")
                                continue

                            self._metrics_bump({"qualified_candidates": 1, "total_scans": 1}, {"qualified_candidates": 1, "total_scans": 1})
                            sig_count = 0
                            ws_reconnect_count = 0
                            if candidate_key.startswith("sig:"):
                                sig = candidate_key.split("sig:", 1)[1].strip()
                                self._candidate_seen_sig_counts[sig] = int(self._candidate_seen_sig_counts.get(sig, 0) or 0) + 1
                                sig_count = self._candidate_seen_sig_counts[sig]
                                last_ws_issue = float(read_state().get('last_ws_issue_ts') or 0.0)
                                if last_ws_issue and (time.time() - last_ws_issue) <= 30.0:
                                    self._candidate_seen_after_ws_reconnect_count += 1
                                    ws_reconnect_count = self._candidate_seen_after_ws_reconnect_count
                            queue_telemetry_event("CANDIDATE_SEEN", detail=f"candidate_key={candidate_key} candidate_seen_sig_count={sig_count} candidate_seen_after_ws_reconnect_count={ws_reconnect_count}", severity="info")

                            try:
                                now_perf = time.perf_counter()
                                self._funnel["raw_candidates_seen"] += 1  # instrumentation
                                self.event_queue.put_nowait({"msg": msg, "t0": now_perf, "queued_at": now_perf, "candidate_key": candidate_key, "queue_depth_at_enqueue": self.event_queue.qsize()})
                            except asyncio.QueueFull:
                                st2 = read_state()
                                write_state(queue_dropped=int(st2.get("queue_dropped", 0)) + 1)
                                log.warning("event queue full; dropped candidate")
                    finally:
                        hb_task.cancel()
            except Exception as e:
                msg = str(e)
                log.error(f"WS error: {msg}")
                self._metrics_bump({"ws_disconnects": 1}, {"ws_disconnects": 1})
                append_telemetry_event("WS_DISCONNECT", detail=msg, severity="error")
                append_recent_activity(action="WS DOWN", mint="solana-ws", live_pnl=0.0)

                st_ws = read_state()
                now_ts = int(time.time())
                history = [int(x) for x in (st_ws.get("ws_disconnect_history") or []) if now_ts - int(x) <= 3600]
                history.append(now_ts)
                write_state(
                    ws_disconnect_history=history[-300:],
                    ws_disconnects_1h=len(history),
                    ws_handshake_timeouts_1h=int(st_ws.get("ws_handshake_timeouts_1h", 0)) + (1 if "opening handshake" in msg else 0),
                )

                if "1011" in msg:
                    self._metrics_bump({"ws_1011_reconnects": 1}, {"ws_1011_reconnects": 1})
                await tg_send(SNIPER_TG_TOKEN, SNIPER_TG_CHAT_ID, f"CRITICAL: sniper simulation websocket down: {e}")
                self.rotate_ws()
                # P4-3: exponential backoff with jitter instead of flat 2-5s.
                # Prevents reconnect storms when all endpoints are degraded.
                self._ws_consecutive_failures += 1
                _base = 3 if any(token in msg for token in ("keepalive ping timeout", "no close frame", "stale_feed", "opening handshake")) else 2
                retry_sleep = min(30, _base * (2 ** min(self._ws_consecutive_failures - 1, 4))) + random.uniform(0, 1)
                log.info("[WS_BACKOFF] consecutive_failures=%d sleep=%.1fs", self._ws_consecutive_failures, retry_sleep)
                await asyncio.sleep(retry_sleep)


def _install_shutdown_handler():
    def _on_term(signum, _frame):
        try:
            write_state(shutting_down=True, shutdown_signal=int(signum), shutdown_ts=int(time.time()))
        except Exception:
            pass
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)


def main():
    _process_lock_fh = acquire_process_lock()
    _install_shutdown_handler()
    asyncio.run(SimEngine().run())


if __name__ == "__main__":
    main()
