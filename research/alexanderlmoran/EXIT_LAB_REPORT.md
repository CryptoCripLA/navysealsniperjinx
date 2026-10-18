# Exit Lab — Post-`constants.py` Baseline

_Generated: 2026-04-22 (P7.3 of IMPLEMENTATION_PLAN.md)_
_Source: `python3 exit_lab.py` against current `data/features.csv` and history.json_

This report establishes the post-Phase-4 (constants.py consolidation) exit-strategy
ranking so that later configuration changes can be diffed against it. Rankings are
**relative** — absolute $ figures are MFE-based counterfactuals and are only valid
for comparing strategies to each other, not for projecting real P&L.

## Ranking on ALL QUALIFYING TRADES (n=325)

| Rank | Strategy                      | Win%  | Avg ROI | ~P&L    | vs base   |
|------|-------------------------------|-------|---------|---------|-----------|
| 1    | Fixed -6% trail               | 60.6% | +14.5%  | $+47.16 | **$+54.47** |
| 2    | Partial TP@+20% + -6% trail   | 60.6% | +12.3%  | $+40.01 | $+47.32   |
| 3    | Fixed -8% trail               | 53.5% | +12.0%  | $+38.96 | $+46.28   |
| 4    | Time tighten (-10%/60s→-6%)   | 52.9% | +11.7%  | $+38.03 | $+45.34   |
| 5    | Tiered trail (20/50% MFE)     | 52.6% | +10.4%  | $+33.69 | $+41.00   |
| —    | Baseline (as-traded)          | 38.2% |  -2.3%  | $-7.31  | —         |

## Ranking on LAST 20 TRADES (recent regime, n=20)

| Rank | Strategy                      | Win%  | Avg ROI | ~P&L   | vs base   |
|------|-------------------------------|-------|---------|--------|-----------|
| 1    | Fixed -6% trail               | 55.0% | +9.2%   | $+1.78 | **$+2.19** |
| 2    | Partial TP@+20% + -6% trail   | 55.0% | +8.5%   | $+1.65 | $+2.06    |
| 3    | Tiered trail (20/50% MFE)     | 50.0% | +8.1%   | $+1.57 | $+1.98    |
| 4    | Fixed -8% trail               | 50.0% | +6.8%   | $+1.32 | $+1.73    |
| 5    | Time tighten (-10%/60s→-6%)   | 45.0% | +6.7%   | $+1.28 | $+1.70    |
| —    | Baseline (as-traded)          | 30.0% |  -2.1%  | $-0.41 | —         |

## Ranking on MFE ≥ 20% subset (n=125)

All five counterfactuals capture 100% win-rate here (by construction — these are
trades that touched +20% at least once). Ranking by avg ROI:

1. Fixed -6% trail — +40.9%
2. Tiered trail (20/50% MFE) — +39.0%
3. Time tighten — +37.9%
4. Fixed -8% trail — +37.8%
5. Partial TP@+20% + -6% trail — +35.2%

## Take-aways

- **Fixed -6% trail is #1 in all three slices.** Both the full-population and
  the recent-20 sample agree, which reduces regime-change risk.
- Partial-TP@+20% + -6% trail is competitive (#2 overall, #2 recent) but gives
  up ROI in the MFE-rich slice because the partial locks in early.
- The difference between #1 and #5 on "vs base" is ~$13 across 325 trades (~$0.04
  per trade). Rankings are stable but the gap is small; small sample shifts can
  reorder the middle of the table.
- Current live config already uses a trailing-stop (`TRAIL_STOP_PCT=0.06`) that
  matches the winning strategy, so no code change is prescribed — this report
  serves as the **frozen baseline** for future P7.4 re-runs.

## How to re-run

```
python3 replay_v2.py   # regenerates REPLAY_REPORT_V2.md and data/features.csv
python3 exit_lab.py    # prints the tables above
```

Compare the "Rank" column across runs. Any re-ordering of the top two (Fixed -6%
trail vs Partial TP@+20%) warrants investigation before adjusting live exit logic.
