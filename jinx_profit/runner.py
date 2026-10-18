"""Deterministic JINX runner: one ingress, one brain, one order port.

A market gateway supplies real normalized events and executable quotes. This
module does not invent prices or submit an order without a supplied venue.
"""
from dataclasses import dataclass
from .brain import ProfitBrain
from .execution_port import SingleOrderPort

@dataclass(frozen=True)
class MarketOpportunity:
    source: str
    mint: str
    buy_usd: float
    full_exit_usd: float
    total_fees_usd: float
    timestamp: int
    deployment: dict | None = None
    prior_deployments: tuple = ()
    prior_target_buys: tuple = ()
    leader_verified: bool = False
    leader_expected_net_usd: float | None = None

class JinxRunner:
    def __init__(self, brain: ProfitBrain, venue):
        self.brain = brain
        self.port = SingleOrderPort(brain, venue)
        self.submitted_tx = None

    def on_opportunity(self, event: MarketOpportunity):
        if self.submitted_tx is not None:
            return None
        decision = self.brain.propose_buy(
            source=event.source, mint=event.mint,
            deployment=event.deployment, timestamp=event.timestamp,
            available_buy_usd=event.buy_usd,
            expected_net_exit_usd=event.full_exit_usd,
            expected_total_fees_usd=event.total_fees_usd,
            prior_deployments=event.prior_deployments,
            prior_target_buys=event.prior_target_buys,
            leader_verified=event.leader_verified,
            leader_expected_net_usd=event.leader_expected_net_usd)
        if decision.action != "BUY":
            return decision
        self.submitted_tx = self.port.submit_pending()
        return decision

    def on_position_quote(self, *, mint, full_exit_usd, fees_usd, force=False):
        if self.submitted_tx is not None:
            return None
        decision = self.brain.observe_exit(
            mint=mint, executable_full_sell_usd=full_exit_usd,
            sell_fees_usd=fees_usd, force=force)
        if decision.action == "SELL":
            self.submitted_tx = self.port.submit_pending()
        return decision

    def resume_pending_liquidation(self):
        """Submit the next remainder after a confirmed partial exit."""
        if self.submitted_tx is not None:
            return None
        pending = self.brain.pending
        if pending is None or pending.action != "SELL":
            return None
        self.submitted_tx = self.port.submit_pending()
        return self.submitted_tx

    def reconcile(self):
        if self.submitted_tx is None:
            return False
        tx = self.submitted_tx
        settled = self.port.reconcile(tx)
        if settled:
            self.submitted_tx = None
        return settled
