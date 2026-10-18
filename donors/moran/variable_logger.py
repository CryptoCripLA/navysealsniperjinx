"""
Variable-change logger.

Tracks a curated set of strategy parameters and writes every change
to data/variable_changes.jsonl so VARIABLES_REGISTRY.md history stays
grounded in real diffs instead of memory.

Two sources are supported:

  1. `.env` (live config) — scanned automatically.
  2. Code constants (simulation_mode.py defaults, hardcoded thresholds) —
     logged manually via `python3 variable_logger.py log KEY VALUE --source code --note "..."`.

Usage
-----
  # scan .env against last snapshot, write diffs
  python3 variable_logger.py scan

  # manually record a change that lives in code, not .env
  python3 variable_logger.py log BURN_TP_LADDER_PCTS "[0.35,0.45,0.20]" --source code --note "split 2x/4x/10x"

  # dump recent history
  python3 variable_logger.py tail 20

State
-----
  data/variable_snapshot.json     — last seen values (baseline for scan)
  data/variable_changes.jsonl     — append-only history of diffs
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
DATA_DIR = ROOT / "data"
SNAPSHOT = DATA_DIR / "variable_snapshot.json"
CHANGES = DATA_DIR / "variable_changes.jsonl"

# Keys tracked from .env. Secrets excluded. Mirrors rows in VARIABLES_REGISTRY.md.
TRACKED_ENV_KEYS = {
    # entry gates / timing
    "MIN_LIQUIDITY_USD",
    "RESEARCH_LP_LOCK_PCT_MIN",
    "MIGRATION_MATURITY_REQUIRE_NON_CURVE_PRICE",
    "LOCK_POLICY",
    "ACTIVE_PROFILE",

    # sizing / portfolio
    "CANARY_TRADE_SIZE_USD",
    "LIVE_BASE_TRADE_USD",
    "CANARY_MAX_CONCURRENT",
    "CANARY_MAX_DAILY_LOSS_USD",

    # exit / stops
    "CANARY_FAILSAFE_STOP_PCT",
    "DEFAULT_DOWNSIDE_STOP_PCT",
    "ABSOLUTE_FAILSAFE_STOP_PCT",

    # execution quality
    "LIVE_BUY_MAX_SLIPPAGE_PCT",
    "LIVE_SELL_MAX_SLIPPAGE_PCT",
    "JITO_TIP_LAMPORTS",

    # flags
    "EXECUTION_MODE",
    "TRADING_ENABLED",
    "ALLOW_LIVE_TRADING",
}


def parse_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k in TRACKED_ENV_KEYS:
            out[k] = v.strip().strip('"').strip("'")
    return out


def load_snapshot() -> Dict[str, Dict[str, str]]:
    if not SNAPSHOT.exists():
        return {}
    try:
        return json.loads(SNAPSHOT.read_text())
    except Exception:
        return {}


def save_snapshot(snap: Dict[str, Dict[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(snap, indent=2, sort_keys=True))


def append_change(rec: Dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CHANGES.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def cmd_scan(note: str = "") -> int:
    snap = load_snapshot()
    current = parse_env(ENV_FILE)
    prev_env = snap.get("env", {})
    ts = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    n = 0
    all_keys = set(prev_env) | set(current)
    for k in sorted(all_keys):
        old = prev_env.get(k)
        new = current.get(k)
        if old == new:
            continue
        rec = {
            "ts": ts,
            "iso": iso,
            "source": ".env",
            "key": k,
            "old": old,
            "new": new,
            "note": note,
        }
        append_change(rec)
        print(f"  {k}: {old!r} -> {new!r}")
        n += 1
    snap["env"] = current
    save_snapshot(snap)
    if n == 0:
        print("no changes.")
    else:
        print(f"logged {n} change(s) to {CHANGES}")
    return 0


def cmd_log(key: str, value: str, source: str, note: str) -> int:
    snap = load_snapshot()
    bucket = snap.setdefault("manual", {})
    old = bucket.get(key)
    ts = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    rec = {
        "ts": ts,
        "iso": iso,
        "source": source,
        "key": key,
        "old": old,
        "new": value,
        "note": note,
    }
    append_change(rec)
    bucket[key] = value
    save_snapshot(snap)
    print(f"logged: {key}: {old!r} -> {value!r}  ({source})")
    return 0


def _try_fetch_hud_metrics(hud_url: str) -> Dict:
    """Best-effort: pull a few metrics from the HUD so the session record is
    not just config. Returns {} on any failure — never raises."""
    try:
        import urllib.request
        import urllib.error
        out: Dict = {}
        for path, key in (("/health", "health"), ("/hud_digest", "digest")):
            try:
                req = urllib.request.Request(hud_url.rstrip("/") + path)
                with urllib.request.urlopen(req, timeout=2) as r:
                    body = r.read().decode("utf-8", errors="ignore")
                    try:
                        out[key] = json.loads(body)
                    except Exception:
                        out[key] = body[:500]
            except Exception:
                pass
        return out
    except Exception:
        return {}


def cmd_session(event: str, note: str, hud_url: str) -> int:
    ts = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    env_snap = parse_env(ENV_FILE)
    hud = _try_fetch_hud_metrics(hud_url) if event == "session_start" else {}
    rec = {
        "ts": ts,
        "iso": iso,
        "source": "session",
        "event": event,
        "env": env_snap,
        "hud": hud,
        "note": note,
    }
    append_change(rec)
    print(f"logged {event} @ {iso} (env_keys={len(env_snap)}, hud_keys={len(hud)})")
    return 0


def cmd_tail(n: int) -> int:
    if not CHANGES.exists():
        print("no history yet.")
        return 0
    lines = CHANGES.read_text().splitlines()
    for ln in lines[-n:]:
        try:
            r = json.loads(ln)
            if r.get("source") == "session":
                print(f"{r['iso']}  [session]  {r.get('event')}  env_keys={len(r.get('env') or {})}"
                      + (f"   # {r['note']}" if r.get("note") else ""))
            else:
                print(f"{r['iso']}  [{r['source']}]  {r['key']}: {r['old']!r} -> {r['new']!r}"
                      + (f"   # {r['note']}" if r.get("note") else ""))
        except Exception:
            print(ln)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("scan", help="diff current .env against last snapshot")
    s1.add_argument("--note", default="")

    s2 = sub.add_parser("log", help="manually record a non-env change")
    s2.add_argument("key")
    s2.add_argument("value")
    s2.add_argument("--source", default="code", choices=["code", "manual", "runtime"])
    s2.add_argument("--note", default="")

    s3 = sub.add_parser("tail", help="show last N changes")
    s3.add_argument("n", type=int, nargs="?", default=20)

    s4 = sub.add_parser("init", help="seed snapshot from current .env without logging")
    _ = s4

    s5 = sub.add_parser("session", help="record a session-boundary event with full env snapshot + optional HUD metrics")
    s5.add_argument("event", choices=["session_start", "session_stop"])
    s5.add_argument("--note", default="")
    s5.add_argument("--hud-url", default="http://127.0.0.1:8000", help="HUD base URL for metrics pull")

    args = ap.parse_args()
    if args.cmd == "scan":
        return cmd_scan(args.note)
    if args.cmd == "log":
        return cmd_log(args.key, args.value, args.source, args.note)
    if args.cmd == "tail":
        return cmd_tail(args.n)
    if args.cmd == "init":
        snap = load_snapshot()
        snap["env"] = parse_env(ENV_FILE)
        save_snapshot(snap)
        print(f"seeded snapshot with {len(snap['env'])} keys")
        return 0
    if args.cmd == "session":
        return cmd_session(args.event, args.note, args.hud_url)
    return 1


if __name__ == "__main__":
    sys.exit(main())
