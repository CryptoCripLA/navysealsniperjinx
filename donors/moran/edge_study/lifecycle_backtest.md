# Lifecycle Backtest Grid
_Sample: 608 trades across cached mints. Stake: $1. Round-trip slippage: 3.0%. Hold cap: 4h._

## Caveats
- Candle close = assumed fillable price; reality is worse in thin pools.
- No_pool/rug exits treated at last captured price — may overstate recovery value.
- Strategies that skip heavily (low `taken`) may be cherry-picking; evaluate P&L against volume.
- Post-exit window cached = 4h; hold-beyond-4h strategies truncate at last candle.

## Top 20 strategies (min taken ≥ 182 / 608)

| rank | entry | exit | taken | skip% | total P&L | avg ROI | win% |
|---:|---|---|---:|---:|---:|---:|---:|
| 1 | pullback_<=0.8x_lb3_win20m | tp30_sl-8 | 248 | 59% | $+2.30 | +0.9% | 31% |
| 2 | t+0m | tp30_sl-8 | 608 | 0% | $+1.85 | +0.3% | 28% |
| 3 | pullback_<=0.8x_lb3_win20m | tp50_sl-10 | 248 | 59% | $+0.20 | +0.1% | 21% |
| 4 | pullback_<=0.8x_lb3_win20m | trail15%_arm+20%_sl-10 | 248 | 59% | $-0.25 | -0.1% | 38% |
| 5 | pullback_<=0.8x_lb3_win20m | tp30_sl-15 | 248 | 59% | $-0.73 | -0.3% | 38% |
| 6 | t+1m | tp30_sl-8 | 608 | 0% | $-0.75 | -0.1% | 27% |
| 7 | t+0m | trail15%_arm+20%_sl-10 | 608 | 0% | $-2.32 | -0.4% | 37% |
| 8 | consol_3b_5%_win15m | tp100_sl-15 | 197 | 68% | $-2.32 | -1.2% | 12% |
| 9 | pullback_<=0.8x_lb3_win20m | tp50_sl-15 | 248 | 59% | $-3.00 | -1.2% | 25% |
| 10 | t+0m | trail20%_arm+30%_sl-10 | 608 | 0% | $-3.05 | -0.5% | 30% |
| 11 | consol_3b_5%_win15m | trail20%_arm+30%_sl-15 | 197 | 68% | $-3.73 | -1.9% | 18% |
| 12 | consol_3b_8%_win20m | trail20%_arm+30%_sl-10 | 260 | 57% | $-3.95 | -1.5% | 17% |
| 13 | consol_3b_5%_win15m | trail20%_arm+30%_sl-10 | 197 | 68% | $-4.08 | -2.1% | 14% |
| 14 | pullback_<=0.8x_lb3_win20m | tp100_sl-15 | 248 | 59% | $-4.82 | -1.9% | 14% |
| 15 | consol_3b_5%_win15m | time_30m | 197 | 68% | $-5.10 | -2.6% | 16% |
| 16 | consol_3b_8%_win20m | trail20%_arm+30%_sl-15 | 260 | 57% | $-5.49 | -2.1% | 20% |
| 17 | pullback_<=0.8x_lb3_win20m | trail20%_arm+30%_sl-10 | 248 | 59% | $-5.87 | -2.4% | 33% |
| 18 | consol_3b_5%_win15m | tp50_sl-15 | 197 | 68% | $-5.90 | -3.0% | 14% |
| 19 | consol_3b_5%_win15m | time_15m | 197 | 68% | $-5.95 | -3.0% | 16% |
| 20 | pullback_<=0.8x_lb3_win20m | time_5m | 248 | 59% | $-5.96 | -2.4% | 39% |

## Best exit for each entry rule

| entry | best exit | taken | total P&L | avg ROI | win% |
|---|---|---:|---:|---:|---:|
| combo_wait2m_then_consol3b_5% | trail20%_arm+30%_sl-10 | 299 | $-10.09 | -3.4% | 15% |
| consol_3b_5%_win15m | tp100_sl-15 | 197 | $-2.32 | -1.2% | 12% |
| consol_3b_8%_win20m | trail20%_arm+30%_sl-10 | 260 | $-3.95 | -1.5% | 17% |
| momentum_2green | tp30_sl-8 | 502 | $-11.54 | -2.3% | 23% |
| pullback_<=0.8x_lb3_win20m | tp30_sl-8 | 248 | $+2.30 | +0.9% | 31% |
| pullback_<=1.1x_lb3_win15m | tp30_sl-8 | 595 | $-15.70 | -2.6% | 20% |
| skip_pump>2.0x_t+30 | trail20%_arm+30%_sl-10 | 518 | $-17.40 | -3.4% | 16% |
| skip_pump>3.0x_t+30 | trail20%_arm+30%_sl-10 | 562 | $-19.70 | -3.5% | 17% |
| skip_pump>5.0x_t+30 | trail20%_arm+30%_sl-10 | 588 | $-20.50 | -3.5% | 18% |
| t+0m | tp30_sl-8 | 608 | $+1.85 | +0.3% | 28% |
| t+10m | tp30_sl-8 | 608 | $-13.70 | -2.3% | 20% |
| t+1m | tp30_sl-8 | 608 | $-0.75 | -0.1% | 27% |
| t+2m | tp30_sl-8 | 608 | $-8.88 | -1.5% | 23% |
| t+5m | tp30_sl-8 | 608 | $-14.12 | -2.3% | 20% |
| vol_cool_0.5x_lb5_win15m | tp50_sl-10 | 541 | $-11.99 | -2.2% | 17% |

## Best entry for each exit rule

| exit | best entry | taken | total P&L | avg ROI | win% |
|---|---|---:|---:|---:|---:|
| lad+trail_25@+25_25@+50_25@+100_trail30% | consol_3b_5%_win15m | 197 | $-11.30 | -5.7% | 18% |
| lad+trail_33@+30_33@+80_trail30% | consol_3b_5%_win15m | 197 | $-9.41 | -4.8% | 18% |
| lad+trail_50@+50_trail20% | consol_3b_5%_win15m | 197 | $-12.79 | -6.5% | 18% |
| lad_33@+30_33@+80_34@+200 | consol_3b_5%_win15m | 197 | $-13.55 | -6.9% | 18% |
| lad_50@+50_50@+100 | consol_3b_5%_win15m | 197 | $-10.70 | -5.4% | 17% |
| time_15m | consol_3b_5%_win15m | 197 | $-5.95 | -3.0% | 16% |
| time_30m | consol_3b_5%_win15m | 197 | $-5.10 | -2.6% | 16% |
| time_5m | pullback_<=0.8x_lb3_win20m | 248 | $-5.96 | -2.4% | 39% |
| time_60m | consol_3b_5%_win15m | 197 | $-6.04 | -3.1% | 16% |
| tp100_sl-15 | consol_3b_5%_win15m | 197 | $-2.32 | -1.2% | 12% |
| tp30_sl-15 | pullback_<=0.8x_lb3_win20m | 248 | $-0.73 | -0.3% | 38% |
| tp30_sl-8 | pullback_<=0.8x_lb3_win20m | 248 | $+2.30 | +0.9% | 31% |
| tp50_sl-10 | pullback_<=0.8x_lb3_win20m | 248 | $+0.20 | +0.1% | 21% |
| tp50_sl-15 | pullback_<=0.8x_lb3_win20m | 248 | $-3.00 | -1.2% | 25% |
| trail15%_arm+20%_sl-10 | pullback_<=0.8x_lb3_win20m | 248 | $-0.25 | -0.1% | 38% |
| trail20%_arm+30%_sl-10 | t+0m | 608 | $-3.05 | -0.5% | 30% |
| trail20%_arm+30%_sl-15 | consol_3b_5%_win15m | 197 | $-3.73 | -1.9% | 18% |
| trail30%_arm+50%_sl-15 | consol_3b_5%_win15m | 197 | $-6.50 | -3.3% | 14% |

## Baseline (current bot behavior approximation)

- `t+0m × trail20%_arm+30%_sl-15`: taken 608, P&L **$-10.76**, avg ROI -1.8%, win 34%