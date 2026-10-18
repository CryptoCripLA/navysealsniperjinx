"""Part 1 figures: behaviour of the target bot."""
import os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIGS = os.path.join(ROOT, "figs")
INK, ACCENT, MUTED, GOOD, BAD = "#16324f", "#d95f02", "#94a3b8", "#0f766e", "#b91c1c"

plt.rcParams.update({
    "figure.facecolor": "white", "axes.facecolor": "white",
    "axes.edgecolor": "#cbd5e1", "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": INK, "ytick.color": INK, "font.size": 9,
    "axes.titlesize": 10.5, "axes.titleweight": "bold", "axes.grid": True,
    "grid.color": "#e2e8f0", "grid.linewidth": 0.7, "axes.axisbelow": True,
    "axes.spines.top": False, "axes.spines.right": False,
})


def note(ax, txt, y=0.93):
    ax.text(0.97, y, txt, transform=ax.transAxes, ha="right", va="top", fontsize=8.5,
            bbox=dict(boxstyle="round,pad=0.4", fc="#fff7ed", ec=ACCENT, lw=0.9))


def max_drawdown(equity):
    peak = np.maximum.accumulate(equity)
    return float((equity - peak).min())


def main():
    os.makedirs(FIGS, exist_ok=True)
    t = pd.read_parquet(os.path.join(ROOT, "data", "interim", "bot_trades.parquet"))
    t = t.sort_values("first_buy_ts").reset_index(drop=True)

    # ================= FIGURE 1: ENTRY =================
    fig, ax = plt.subplots(2, 2, figsize=(11.5, 7.4))
    fig.suptitle("Bot 5brv79e: anatomy of the entry", fontsize=13.5, fontweight="bold", y=0.98)

    a = ax[0, 0]
    v = t.entry_sol.clip(upper=25)
    a.hist(v, bins=80, color=INK, alpha=.85)
    a.axvline(t.entry_sol.median(), color=ACCENT, lw=2)
    a.set_title("Size of the first entry")
    a.set_xlabel("SOL (clipped at 25)"); a.set_ylabel("tokens")
    note(a, f"median {t.entry_sol.median():.2f} SOL\nP25 to P75 {t.entry_sol.quantile(.25):.2f} to {t.entry_sol.quantile(.75):.2f}\nmax {t.entry_sol.max():,.0f} SOL")

    a = ax[0, 1]
    ls = t.latency_slots.dropna()
    zero = (ls == 0).mean()
    cats = ["0\n(zero block)", "1", "2 to 5", "6 to 50", "over 50"]
    vals = [(ls == 0).mean(), (ls == 1).mean(), ls.between(2, 5).mean(),
            ls.between(6, 50).mean(), (ls > 50).mean()]
    bars = a.bar(cats, vals, color=[ACCENT] + [INK] * 4)
    a.yaxis.set_major_formatter(PercentFormatter(1))
    a.set_title("Latency from deployment to first buy")
    a.set_ylabel("share of tokens")
    for b, v_ in zip(bars, vals):
        a.text(b.get_x() + b.get_width() / 2, v_ + .012, f"{v_:.1%}", ha="center", fontsize=8.5, fontweight="bold")
    a.set_ylim(0, max(vals) * 1.18)
    note(a, f"zero block: {zero:.1%}\nwithin 1 slot: {(ls<=1).mean():.1%}", y=0.99)

    a = ax[1, 0]
    ib = t.inblock_delta.dropna()
    a.hist(ib.clip(upper=800), bins=80, color=INK, alpha=.85)
    a.axvline(ib.median(), color=ACCENT, lw=2)
    a.set_title("Position in the block, relative to the deployment")
    a.set_xlabel("transactions after the deploy (clipped at 800)"); a.set_ylabel("tokens")
    note(a, f"median +{ib.median():.0f} tx\nwithin 5 tx: {(ib<=5).mean():.2%}\nnever before the deploy")

    a = ax[1, 1]
    pf = pd.to_numeric(t.entry_priority_fee, errors="coerce").dropna()
    a.hist(pf.clip(upper=1.0), bins=70, color=INK, alpha=.85)
    a.axvline(pf.median(), color=ACCENT, lw=2)
    a.set_title("Priority fee paid on entry")
    a.set_xlabel("SOL (clipped at 1.0)"); a.set_ylabel("tokens")
    note(a, f"median {pf.median():.3f} SOL\nP90 {pf.quantile(.9):.3f} SOL\nJito tip over 0: {(pd.to_numeric(t.entry_tip_fee,errors='coerce')>0).mean():.0%}")

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(os.path.join(FIGS, "fig1_entree.png"), dpi=160)
    plt.close(fig)

    # ================= FIGURE 2: EXIT AND P&L =================
    fig, ax = plt.subplots(2, 2, figsize=(11.5, 7.4))
    fig.suptitle("Bot 5brv79e: exit and performance", fontsize=13.5, fontweight="bold", y=0.98)

    a = ax[0, 0]
    hs = t.hold_s.dropna().clip(upper=60)
    a.hist(hs, bins=60, color=INK, alpha=.85)
    a.axvline(t.hold_s.median(), color=ACCENT, lw=2)
    a.set_title("Holding time")
    a.set_xlabel("seconds (clipped at 60)"); a.set_ylabel("tokens")
    note(a, f"median {t.hold_s.median():.0f} s\nP75 {t.hold_s.quantile(.75):.0f} s\nP95 {t.hold_s.quantile(.95):.0f} s")

    a = ax[0, 1]
    ns = t.n_sells.clip(upper=12)
    a.hist(ns, bins=np.arange(-.5, 13.5), color=INK, alpha=.85, rwidth=.85)
    a.set_title("Exit structure: sells per token")
    a.set_xlabel("sell transactions (clipped at 12)"); a.set_ylabel("tokens")
    note(a, f"median {t.n_sells.median():.0f} sells\n3 or more: {(t.n_sells>=3).mean():.0%}\npositions closed: {t.closed.mean():.0%}")

    a = ax[1, 0]
    p = t.pnl_sol.clip(-5, 15)
    a.hist(p[p >= 0], bins=60, color=GOOD, alpha=.85, label="gains")
    a.hist(p[p < 0], bins=40, color=BAD, alpha=.85, label="losses")
    a.axvline(0, color=INK, lw=1)
    a.set_title("Per-trade P&L distribution")
    a.set_xlabel("SOL (clipped to [-5, +15])"); a.set_ylabel("tokens"); a.legend(frameon=False)
    note(a, f"hit rate {t.is_win.mean():.1%}\nmean win +{t.loc[t.is_win==1,'pnl_sol'].mean():.2f}\nmean loss {t.loc[t.is_win==0,'pnl_sol'].mean():.2f}")

    a = ax[1, 1]
    eq = t.pnl_sol.cumsum().values
    ts = pd.to_datetime(t.first_buy_ts, unit="s", utc=True)
    a.plot(ts, eq, color=INK, lw=1.4)
    a.fill_between(ts, 0, eq, color=INK, alpha=.10)
    dd = max_drawdown(eq)
    a.set_title("Cumulative equity curve")
    a.set_ylabel("cumulative P&L (SOL)")
    a.tick_params(axis="x", rotation=20)
    note(a, f"total P&L {eq[-1]:,.0f} SOL\ndeployed {t.sol_in.sum():,.0f} SOL\nROI {eq[-1]/t.sol_in.sum():.1%}\nmax drawdown {dd:,.0f} SOL", y=0.35)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(os.path.join(FIGS, "fig2_sortie_pnl.png"), dpi=160)
    plt.close(fig)

    print("figs/fig1_entree.png and figs/fig2_sortie_pnl.png written")
    print(f"bot max drawdown: {dd:,.1f} SOL | total P&L {eq[-1]:,.1f} SOL")


if __name__ == "__main__":
    main()
