"""Single executor port for JINX: no donor is permitted to submit orders.

An adapter on the new Solana server will implement this protocol against an
actual signing/routing client. No fake fills or synthetic wallet profit here.
"""
from dataclasses import dataclass
from typing import Protocol
from .brain import Intent, ProfitBrain

@dataclass(frozen=True)
class ExecutionReceipt:
    mint: str
    side: str
    tx_id: str
    filled_quantity: float
    gross_usd: float
    fees_usd: float
    verified_remaining_qty: float | None = None

class TradingVenue(Protocol):
    def submit(self, intent: Intent) -> str: ...
    def get_confirmed_fill(self, tx_id: str) -> ExecutionReceipt | None: ...
    def get_remaining_quantity(self, mint: str) -> float: ...

class SingleOrderPort:
    """Adapter owns ONLY network transport and validated receipts.

    No strategy, veto, additional sniper path, or synthetic success report.
    """
    def __init__(self, brain: ProfitBrain, venue: TradingVenue):
        self.brain=brain
        self.venue=venue

    def submit_pending(self):
        intent=self.brain.pending
        if intent is None or intent.action not in ("BUY","SELL"):
            raise ValueError("no pending executable order")
        return self.venue.submit(intent)

    def reconcile(self, tx_id: str):
        receipt=self.venue.get_confirmed_fill(tx_id)
        if receipt is None:
            return False
        pending=self.brain.pending
        if pending is None or receipt.side!=pending.action or receipt.mint!=pending.mint:
            raise ValueError("receipt does not match pending order")
        if receipt.side=="BUY":
            self.brain.apply_buy_fill(mint=receipt.mint,qty=receipt.filled_quantity,
                                      gross_paid_usd=receipt.gross_usd,fees_usd=receipt.fees_usd)
        else:
            actual_qty=self.venue.get_remaining_quantity(receipt.mint)
            if receipt.verified_remaining_qty is not None and abs(actual_qty-receipt.verified_remaining_qty)>1e-9:
                raise ValueError("balance disagreement")
            self.brain.apply_sell_fill(mint=receipt.mint,qty_sold=receipt.filled_quantity,
                                       gross_received_usd=receipt.gross_usd,fees_usd=receipt.fees_usd,
                                       verified_remaining_qty=actual_qty)
        return True
