"""
extract_features.py — Build per-trade feature matrix for edge validation.

Joins:
  - history.json trade_close (label: roi_pct, is_winner)
  - history.json signal_accept (delay_s, source, reference_price presence)
  - logs/logs_sim.err (entry_score, lock policy, holder, fill, pre_entry ratio)

Output: edge_study/features.csv

Feature tiers:
  Tier A (all trades): time features, signal_accept features, history-derived
  Tier B (~445 trades): fill/pool features from LIVE_CANARY logs
  Tier C (~100 trades): entry_score components (ENTRY_SCORE logging added 2026-04-16)

Missing values are left as NaN. Classifier handles natively.
"""
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "history.json"
LOG = ROOT / "logs" / "logs_sim.err"
OUT = ROOT / "edge_study" / "features.csv"


def load_history():
    with open(HIST) as f:
        return json.load(f)


def parse_logs(mints_of_interest: set[str]) -> dict[str, dict]:
    """Single-pass scan. Collects all known patterns per mint. Later ENTRY_SCORE
    lines for the same mint overwrite earlier ones (most recent wins)."""
    features: dict[str, dict] = {m: {} for m in mints_of_interest}

    patterns = {
        "entry_score": re.compile(
            r"\[ENTRY_SCORE_(PASS|VETO)\] mint=(\w+) total=(\d+)/\d+ "
            r"graduation=(\d+) dex=(\d+) holder=(\d+) social=(\d+) "
            r"age_s=([\d.]+|None) fetch_err=(\S+)"
        ),
        "lock": re.compile(
            r"\[LOCK_POLICY:lp_pct_threshold\] PASS mint=(\w+) "
            r"pumpfun=\w+ lpLockedPct=([\d.]+) lpUnlocked=([\d.]+) "
            r"lpLockedUSD=([\d.]+)"
        ),
        "holder": re.compile(
            r"\[DELAYED_ENTRY_HOLDER_OK\] mint=(\w+) reason=holder_ok_(\d+)pct_top5"
        ),
        "live_buy": re.compile(
            r"\[LIVE_CANARY\] Position tracked: mint=(\w+) "
            r"entry_px=([\d.e\-+]+) size_usd=([\d.]+) qty=[\d.]+ "
            r"stop_px=([\d.e\-+]+)"
        ),
        "pre_entry_ok": re.compile(
            r"\[PRE_ENTRY_PRICE_OK\] mint=(\w+) fill_px=([\d.e\-+]+) "
            r"ref_px=([\d.e\-+]+) ratio=([\d.]+)x"
        ),
        "liq": re.compile(
            r"\[LIQ_CHECK\] mint=(\w+) raydium_liq_usd=(\S+)"
        ),
        "route": re.compile(
            r"\[SWAP_ROUTE_CHECK\] mint=(\w+) probe_amount=\d+ "
            r"outAmount=(\d+) routable=(\w+)"
        ),
    }

    with open(LOG, "r", errors="ignore") as f:
        for line in f:
            # ENTRY_SCORE
            m = patterns["entry_score"].search(line)
            if m and m.group(2) in features:
                mint = m.group(2)
                features[mint].update({
                    "score_total": int(m.group(3)),
                    "score_graduation": int(m.group(4)),
                    "score_dex": int(m.group(5)),
                    "score_holder": int(m.group(6)),
                    "score_social": int(m.group(7)),
                    "score_age_s": float(m.group(8)) if m.group(8) != "None" else None,
                    "score_fetch_err": 0 if m.group(9) == "None" else 1,
                    "score_passed": 1 if m.group(1) == "PASS" else 0,
                })
                continue

            m = patterns["lock"].search(line)
            if m and m.group(1) in features:
                features[m.group(1)].setdefault("lp_locked_pct", float(m.group(2)))
                features[m.group(1)].setdefault("lp_locked_usd", float(m.group(4)))
                continue

            m = patterns["holder"].search(line)
            if m and m.group(1) in features:
                features[m.group(1)]["holder_top5_pct"] = int(m.group(2))
                continue

            m = patterns["live_buy"].search(line)
            if m and m.group(1) in features:
                features[m.group(1)].update({
                    "entry_px": float(m.group(2)),
                    "size_usd": float(m.group(3)),
                    "stop_px_initial": float(m.group(4)),
                })
                continue

            m = patterns["pre_entry_ok"].search(line)
            if m and m.group(1) in features:
                features[m.group(1)]["pre_entry_ratio"] = float(m.group(4))
                features[m.group(1)]["pre_entry_fill_px"] = float(m.group(2))
                features[m.group(1)]["pre_entry_ref_px"] = float(m.group(3))
                continue

            m = patterns["liq"].search(line)
            if m and m.group(1) in features:
                v = m.group(2)
                if v == "no_data_proceed":
                    features[m.group(1)]["liq_known"] = 0
                else:
                    try:
                        features[m.group(1)]["liq_usd"] = float(v)
                        features[m.group(1)]["liq_known"] = 1
                    except Exception:
                        pass
                continue

            m = patterns["route"].search(line)
            if m and m.group(1) in features:
                features[m.group(1)]["route_out_amount"] = int(m.group(2))
                features[m.group(1)]["route_routable"] = 1 if m.group(3).lower() == "true" else 0
                continue

    return features


def build_feature_rows():
    hist = load_history()
    tc = [r for r in hist if r.get("type") == "trade_close"]
    sa = {r["mint"]: r for r in hist if r.get("type") == "signal_accept"}

    mints = {r["mint"] for r in tc}
    log_features = parse_logs(mints)

    rows = []
    for r in tc:
        mint = r["mint"]
        ts = int(r.get("ts") or r.get("entry_ts") or 0)
        dt = datetime.fromtimestamp(ts, timezone.utc) if ts else None
        sa_r = sa.get(mint, {})
        lf = log_features.get(mint, {})

        # Label
        roi = float(r.get("roi_pct") or 0.0)

        # Fill-quality: fill_px from pre_entry_ok (most accurate), fall back to entry_px
        fill_px = lf.get("pre_entry_fill_px") or lf.get("entry_px")
        ref_px = lf.get("pre_entry_ref_px") or sa_r.get("reference_price")
        slippage_pct = None
        if fill_px and ref_px and ref_px > 0:
            slippage_pct = (fill_px - ref_px) / ref_px * 100.0

        row = {
            "mint": mint,
            # Label
            "roi_pct": roi,
            "is_winner": 1 if roi > 5.0 else 0,
            "is_big_winner": 1 if roi > 20.0 else 0,
            # History-derived (NOT features for pred: post-hoc)
            "close_reason": r.get("close_reason"),
            "hold_time_seconds": r.get("hold_time_seconds"),
            "mfe_pct": r.get("mfe_pct"),
            "mae_pct": r.get("mae_pct"),
            # Tier A: time + signal_accept
            "hour_of_day": dt.hour if dt else None,
            "day_of_week": dt.weekday() if dt else None,
            "delay_s": sa_r.get("delay_s"),
            "source_migration_retry": 1 if sa_r.get("source") == "migration_retry" else 0,
            "source_unset": 1 if sa_r.get("source") == "unset" else 0,
            "has_reference_price": 1 if sa_r.get("reference_price") else 0,
            # Tier B: fill/pool
            "entry_px": lf.get("entry_px"),
            "size_usd": lf.get("size_usd"),
            "fill_px": fill_px,
            "slippage_pct": slippage_pct,
            "pre_entry_ratio": lf.get("pre_entry_ratio"),
            "lp_locked_pct": lf.get("lp_locked_pct"),
            "lp_locked_usd": lf.get("lp_locked_usd"),
            "holder_top5_pct": lf.get("holder_top5_pct"),
            "liq_known": lf.get("liq_known"),
            "liq_usd": lf.get("liq_usd"),
            "route_out_amount": lf.get("route_out_amount"),
            # Tier C: entry score
            "score_total": lf.get("score_total"),
            "score_graduation": lf.get("score_graduation"),
            "score_dex": lf.get("score_dex"),
            "score_holder": lf.get("score_holder"),
            "score_social": lf.get("score_social"),
            "score_age_s": lf.get("score_age_s"),
            "score_fetch_err": lf.get("score_fetch_err"),
            "score_passed": lf.get("score_passed"),
        }
        rows.append(row)

    return rows


def main():
    rows = build_feature_rows()
    print(f"Extracted {len(rows)} trades")
    # Summary
    n_label_winner = sum(r["is_winner"] for r in rows)
    print(f"Winners (ROI > 5%): {n_label_winner} ({n_label_winner/len(rows):.1%})")

    coverage = {}
    for r in rows:
        for k, v in r.items():
            if v is not None:
                coverage[k] = coverage.get(k, 0) + 1
    print("\nFeature coverage:")
    for k in sorted(coverage.keys()):
        pct = coverage[k] / len(rows)
        if pct < 1.0:
            print(f"  {k}: {coverage[k]} ({pct:.1%})")

    # Write CSV
    OUT.parent.mkdir(exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
