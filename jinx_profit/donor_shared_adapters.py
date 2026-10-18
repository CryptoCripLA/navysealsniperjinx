"""Unified source adapters for donors #5-#10.

Donor source files remain preserved under research/. All market/copy evidence
flows into the ONE ProfitBrain; this module never owns transaction submission.
"""
from dataclasses import dataclass
from math import isfinite

@dataclass(frozen=True)
class WalletPerformance:
    address: str
    realized_profit_usd: float
    closed_trades: int
    winning_trades: int
    last_trade_time: int

    @property
    def win_rate(self):
        return self.winning_trades/self.closed_trades if self.closed_trades else 0.0

def wallet_performance(address, closed_positions, last_trade_time=0):
    """Donor #5 wallet-behavior: independent realized USD wallet accounting."""
    outcomes=[float(p["net_profit_usd"]) for p in closed_positions]
    if not all(isfinite(x) for x in outcomes):
        raise ValueError("invalid realized P&L")
    return WalletPerformance(address,sum(outcomes),len(outcomes),
                             sum(x>0 for x in outcomes),int(last_trade_time))

def rank_copy_wallets(wallets, *, min_closed=10):
    """Donor #9: wallet profiling from realized earnings, not dashboard ranking."""
    eligible=[w for w in wallets if w.closed_trades>=min_closed]
    return sorted(eligible,key=lambda w:(w.realized_profit_usd,w.win_rate),reverse=True)

def mirror_sell_fraction(held_quantity, leader_before, leader_after):
    """Donor #5/#10: mirror cumulative leader sell fraction, not raw token amount."""
    before=float(leader_before);after=float(leader_after);held=float(held_quantity)
    if not all(isfinite(x) and x>=0 for x in (before,after,held)) or before<=0 or after>before:
        raise ValueError("invalid leader inventory")
    return held*(before-after)/before

def copier_expected_net_usd(*, expected_exit_usd, buy_usd, entry_fees_usd,
                            exit_fees_usd, leader_price_impact_usd=0.0):
    """Donor #6: copier's own entry economics, not leader's headline profit."""
    v=tuple(float(x) for x in (expected_exit_usd,buy_usd,entry_fees_usd,
                              exit_fees_usd,leader_price_impact_usd))
    if not all(isfinite(x) and x>=0 for x in v):
        raise ValueError("invalid copy economics")
    return v[0]-sum(v[1:])

def executable_net_profit_usd(*, buy_total_usd, full_exit_quote_usd,
                              expected_sell_fees_usd, extra_impact_usd=0.0):
    """Donor #7: full-position executable quote less costs, no candle-high fiction."""
    a=tuple(float(x) for x in (buy_total_usd,full_exit_quote_usd,
                               expected_sell_fees_usd,extra_impact_usd))
    if not all(isfinite(x) and x>=0 for x in a):
        raise ValueError("invalid replay cashflow")
    return a[1]-a[0]-a[2]-a[3]

def normalize_market_event(raw, *, event_timestamp, current_timestamp):
    """Donor #8: market data into a bounded, typed point-in-time observation."""
    t=int(event_timestamp);now=int(current_timestamp)
    if t>now:
        raise ValueError("cannot observe a future event")
    mint=str(raw["mint"])
    price=float(raw["full_exit_quote_usd"])
    if not mint or not isfinite(price) or price<0:
        raise ValueError("invalid market event")
    return {"mint":mint,"timestamp":t,"full_exit_quote_usd":price}
