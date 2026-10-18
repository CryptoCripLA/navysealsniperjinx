#!/usr/bin/env python3
"""Read data/trades.csv → engineer buckets/labels → data/features.csv.

P4.8: bucket boundary rationale — each bucket function below now carries a
source note (edge-study, doctrine, or convention). If you change a boundary,
either (a) cite a fresh edge study that justifies it, or (b) pull boundaries
from quantiles of the current dataset rather than hand-picking them.

Labels are conservative:
- rug_label: roi ≤ −50% OR close_reason in {HARD_CAP_EXIT, FAILSAFE*, GHOST_BUY_NO_TOKENS, RAPID_DUMP}
- roi_class: loss (≤−20%), neutral (−20% to +10%), win (>+10%)
- good_entry_bad_exit: mfe_pct ≥ 30 AND roi_pct < 5
Pandas only.
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
IN = ROOT / "data" / "trades.csv"
OUT = ROOT / "data" / "features.csv"


def _delay_bucket(x):
    # Rationale (P4.8): boundaries anchored on the 2026-04-18 edge study that
    # showed the 32–35s bucket had +3.78% avg ROI vs 30–32s at −13.59% (see
    # memory/project_constants.md). DELAYED_ENTRY_SECONDS=30 is the production
    # trigger, so lt_28/28_30/30_32 cover "fired early", 32_36 is the sweet
    # spot, 36_45/gte_45 capture oversleeping. Do NOT shift these without a
    # fresh study — they are the axis the entry timer is validated against.
    if pd.isna(x):
        return "unknown"
    if x < 28:
        return "lt_28"
    if x < 30:
        return "28_30"
    if x < 32:
        return "30_32"
    if x < 36:
        return "32_36"  # edge-study sweet spot (see project_constants)
    if x < 45:
        return "36_45"
    return "gte_45"


def _hold_bucket(x):
    # Rationale (P4.8): doctrinal thresholds, not data-derived — 60s/180s
    # align with the rapid-dump window + TP1 arrival, 600s/1800s bracket the
    # TTL cutoff (900s) and the "idle moonbag" regime. When sample size grows
    # past ~2k closes, consider quantile-based buckets instead.
    if pd.isna(x):
        return "unknown"
    if x < 60:
        return "lt_60s"
    if x < 180:
        return "60_180s"
    if x < 600:
        return "180_600s"
    if x < 1800:
        return "600_1800s"
    return "gte_1800s"


def _exit_bucket(reason):
    if not isinstance(reason, str):
        return "unknown"
    r = reason.upper()
    if "HARD_CAP" in r or "FAILSAFE" in r:
        return "hard_cap"
    if "RAPID" in r:
        return "rapid_dump"
    if "TRAIL" in r or "SL" in r:
        return "trail_sl"
    if "QUOTEABILITY" in r or "STALE" in r:
        return "liquidity"
    if "TP" in r or "TAKE" in r:
        return "take_profit"
    if "GHOST" in r:
        return "ghost"
    if "TTL" in r or "TIME" in r:
        return "time_exit"
    return "other"


def _liq_bucket(size_usd):
    # WARNING: size_usd is a SESSION-ENV ARTIFACT, not a per-trade feature.
    # _build_execution_intent sets size_usd = min(LIVE_BASE_TRADE_USD,
    # MAX_POSITION_SIZE_SOL * sol_usd) once per session, so the $0.50/$1.00/
    # $3.00 cohorts in trades.csv reflect which env was loaded at session
    # start, NOT a per-candidate decision. The "$0.50 wins more" split is
    # confounded with telemetry schema evolution (older sessions had NULL
    # entry_score/top5/jito_tip). Do NOT treat liq_bucket as predictive —
    # use it only for session/rung labeling, and control for session_id when
    # slicing. See memory/project_size_usd_cohorts.md.
    if pd.isna(size_usd):
        return "unknown"
    if size_usd < 0.75:
        return "micro"
    if size_usd < 1.25:
        return "rung1"
    if size_usd < 3:
        return "rung2"
    return "rung3_plus"


def _roi_class(roi):
    if pd.isna(roi):
        return "unknown"
    if roi <= -20:
        return "loss"
    if roi < 10:
        return "neutral"
    return "win"


def _is_rug(row):
    roi = row.get("roi_pct")
    reason = str(row.get("close_reason") or "").upper()
    if pd.notna(roi) and roi <= -50:
        return 1
    if any(tok in reason for tok in ("HARD_CAP", "FAILSAFE", "GHOST_BUY_NO_TOKENS", "RAPID_DUMP", "RAPID DUMP")):
        return 1
    return 0


def main():
    df = pd.read_csv(IN)
    if df.empty:
        print("[feature_builder] empty trades.csv — nothing to do")
        OUT.write_text("")
        return 0

    for c in ("roi_pct", "mfe_pct", "mae_pct", "pnl_usd", "hold_time_s",
              "delay_s", "size_usd", "entry_score", "holder_pct_top5",
              "detect_to_exec_ms", "execution_latency_ms",
              "entry_jito_tip_lamports", "executable_mark_observations",
              "candidate_age_ms", "opportunity_age_ms", "entry_price",
              "exit_price"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df["delay_bucket"] = df["delay_s"].apply(_delay_bucket)
    df["hold_bucket"] = df["hold_time_s"].apply(_hold_bucket)
    df["exit_bucket"] = df["close_reason"].apply(_exit_bucket)
    df["liq_bucket"] = df["size_usd"].apply(_liq_bucket)
    df["roi_class"] = df["roi_pct"].apply(_roi_class)

    # Session tagging — so every downstream slice can control for env/schema
    # drift. session_date = UTC day of close_ts. session_id = gap-based
    # cluster: any gap > 30min between consecutive entry_ts in sorted order
    # starts a new session. This recovers session boundaries even for the
    # portion of history that predates variable_changes.jsonl logging.
    if "close_ts" in df.columns:
        _ts = pd.to_numeric(df["close_ts"], errors="coerce")
        df["session_date"] = pd.to_datetime(
            _ts, unit="s", utc=True, errors="coerce"
        ).dt.strftime("%Y-%m-%d").fillna("unknown")
    else:
        df["session_date"] = "unknown"

    if "entry_ts" in df.columns:
        _ets = pd.to_numeric(df["entry_ts"], errors="coerce").fillna(0.0)
        order = _ets.sort_values().index.tolist()
        session_id_map = {}
        cur_sid = 0
        prev_ts = None
        for idx in order:
            t = float(_ets.loc[idx])
            if prev_ts is None or (t - prev_ts) > 7200 or t <= 0:
                cur_sid += 1
            session_id_map[idx] = f"S{cur_sid:03d}"
            if t > 0:
                prev_ts = t
        df["session_id"] = df.index.map(session_id_map).fillna("S000")
    else:
        df["session_id"] = "S000"
    df["rug_label"] = df.apply(_is_rug, axis=1)
    # P4.2: canonical win = pnl_usd > 0 (matches HUD, scorecard, replay).
    # big_win_label keeps the old roi>10% target for ML class-balance purposes.
    df["win_label"] = (df["pnl_usd"].fillna(0) > 0).astype(int)
    df["big_win_label"] = (df["roi_pct"].fillna(0) > 10).astype(int)
    df["good_entry_bad_exit"] = (
        (df["mfe_pct"].fillna(0) >= 30) & (df["roi_pct"].fillna(0) < 5)
    ).astype(int)

    # Log-scaled numerics for downstream modeling (safe for zeros/negs).
    def _signed_log1p(x):
        if pd.isna(x):
            return math.nan
        return math.copysign(math.log1p(abs(float(x))), float(x))

    for c in ("pnl_usd", "mfe_pct", "mae_pct", "roi_pct"):
        if c in df.columns:
            df[f"{c}_log"] = df[c].apply(_signed_log1p)

    # Latency-gap proxy: detect_to_exec above 1500ms is worth flagging.
    df["slow_exec"] = (df["detect_to_exec_ms"].fillna(0) >= 1500).astype(int)
    df["fast_exec"] = (df["detect_to_exec_ms"].fillna(99999) < 800).astype(int)

    # Entry-score availability flag (sparse field; treat missingness as a signal).
    df["has_entry_score"] = df["entry_score"].notna().astype(int)
    df["has_delay"] = df["delay_s"].notna().astype(int)

    df.to_csv(OUT, index=False)

    print(f"[feature_builder] wrote {len(df)} rows → {OUT}")
    print("  rugs:", int(df["rug_label"].sum()), "/", len(df))
    print("  wins:", int(df["win_label"].sum()), "/", len(df))
    print("  good_entry_bad_exit:", int(df["good_entry_bad_exit"].sum()))
    print("  delay bucket counts:")
    for k, v in df["delay_bucket"].value_counts().items():
        print(f"    {k}: {v}")
    print("  exit bucket counts:")
    for k, v in df["exit_bucket"].value_counts().items():
        print(f"    {k}: {v}")
    print(f"  sessions: {df['session_id'].nunique()} unique "
          f"({df['session_date'].nunique()} UTC days)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
