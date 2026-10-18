"""One deterministic JINX state machine. Donors never own FIRE.

Research signals are proposals; one lifecycle owns entries, closes and USD P&L.
The external execution adapter must reconcile every real fill against chain state.
"""
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from .features import extract_deployment_features
from .model import score_opportunity
from .donor_kaizen256 import deployer_relationship_features
from .donor_alexanderlmoran import trailing_exit_signal
from .donor_sorghobrayan import decode_notch

class Mode(str, Enum):
    AUTO="AUTO"
    COPY="COPY"
    OFF="OFF"

class Phase(str, Enum):
    FLAT="FLAT"
    BUY_PENDING="BUY_PENDING"
    OPEN="OPEN"
    SELL_PENDING="SELL_PENDING"

@dataclass(frozen=True)
class Intent:
    action: str
    mint: str=""
    usd: float=0.0
    reason: str=""

@dataclass
class Position:
    mint: str
    qty: float
    cost_usd: float
    buy_fees_usd: float
    peak_quote_usd: float=0.0

class ProfitBrain:
    def __init__(self, *, mode="OFF", wallet_usd=0.0, reserve_usd=0.0,
                 profit_model=None, sizing_model=None, minimum_probability=0.5):
        self.mode=Mode(mode)
        self.phase=Phase.FLAT
        self.wallet_usd=float(wallet_usd)
        self.reserve_usd=float(reserve_usd)
        self.profit_model=profit_model
        self.sizing_model=sizing_model
        self.minimum_probability=float(minimum_probability)
        self.position=None
        self.pending=None
        self.realized_usd=0.0
        self.closed_trades=0
        self.historical_context={}
        if not all(isfinite(x) and x>=0 for x in
                   (self.wallet_usd,self.reserve_usd,self.minimum_probability)):
            raise ValueError("invalid financial state")

    def set_mode(self, mode):
        self.mode=Mode(mode)

    def propose_buy(self, *, source, mint, deployment=None, timestamp=None,
                    available_buy_usd=0.0, expected_net_exit_usd=0.0,
                    expected_total_fees_usd=0.0, prior_deployments=(),
                    prior_target_buys=(), leader_verified=False,
                    leader_expected_net_usd=None):
        if self.mode==Mode.OFF or Mode(source)!=self.mode:
            return Intent("WAIT", reason="mode")
        if self.phase!=Phase.FLAT or self.position or self.pending:
            return Intent("WAIT", reason="position_or_order_active")
        if not mint or any(not isfinite(float(v)) or float(v)<0 for v in
                          (available_buy_usd,expected_net_exit_usd,expected_total_fees_usd)):
            return Intent("WAIT", reason="invalid_quote")
        spend=float(available_buy_usd)
        if spend<=0 or spend+float(expected_total_fees_usd)+self.reserve_usd>self.wallet_usd:
            return Intent("WAIT", reason="budget")
        if expected_net_exit_usd-spend-expected_total_fees_usd<=0:
            return Intent("WAIT", reason="negative_expected_net")
        if self.mode==Mode.AUTO:
            if deployment is None or self.profit_model is None or timestamp is None:
                return Intent("WAIT", reason="model_or_deployment_missing")
            if int(deployment.get("blockTime",-1))>int(timestamp):
                return Intent("WAIT", reason="future_deployment")
            features=extract_deployment_features(deployment)
            self.historical_context=deployer_relationship_features(
                int(deployment["blockTime"]),list(prior_deployments),list(prior_target_buys))
            p=score_opportunity(self.profit_model,features)
            if p<self.minimum_probability:
                return Intent("WAIT", reason="model_reject")
            if self.sizing_model is not None:
                from .features import FEATURE_NAMES
                prediction=float(self.sizing_model.predict([[features[k] for k in FEATURE_NAMES]])[0])
                self.historical_context["predicted_conviction_notch"]=decode_notch(prediction)
                # USD amount remains bounded by an externally supplied executable quote.
        elif not leader_verified or leader_expected_net_usd is None or leader_expected_net_usd<=0:
            return Intent("WAIT", reason="unverified_copy_opportunity")
        intent=Intent("BUY",mint,spend,"single_brain")
        self.pending=intent
        self.phase=Phase.BUY_PENDING
        return intent

    def apply_buy_fill(self, *, mint, qty, gross_paid_usd, fees_usd):
        if self.phase!=Phase.BUY_PENDING or not self.pending or mint!=self.pending.mint:
            raise ValueError("unmatched buy fill")
        if not all(isfinite(float(x)) and float(x)>=0 for x in (qty,gross_paid_usd,fees_usd)) or qty==0:
            raise ValueError("invalid fill")
        total=float(gross_paid_usd)+float(fees_usd)
        if total>self.wallet_usd:
            raise ValueError("fill exceeds capital")
        self.wallet_usd-=total
        self.position=Position(mint,float(qty),float(gross_paid_usd),float(fees_usd))
        self.pending=None
        self.phase=Phase.OPEN

    def observe_exit(self, *, mint, executable_full_sell_usd, sell_fees_usd=0.0,
                     trailing_fraction=0.06, force=False):
        if self.phase!=Phase.OPEN or not self.position or self.position.mint!=mint:
            return Intent("WAIT", reason="not_open")
        if not all(isfinite(float(v)) and float(v)>=0 for v in (executable_full_sell_usd,sell_fees_usd)):
            return Intent("WAIT", reason="invalid_sell_quote")
        p=self.position
        current=float(executable_full_sell_usd)-float(sell_fees_usd)
        p.peak_quote_usd=max(p.peak_quote_usd,current)
        if force or (p.peak_quote_usd>0 and
                     trailing_exit_signal([p.peak_quote_usd,current],trailing_fraction)):
            self.pending=Intent("SELL",mint,0.0,"force" if force else "trailing_exit")
            self.phase=Phase.SELL_PENDING
            return self.pending
        return Intent("HOLD",mint,reason="wave_continues")

    def apply_sell_fill(self, *, mint, qty_sold, gross_received_usd, fees_usd,
                        verified_remaining_qty):
        if self.phase!=Phase.SELL_PENDING or not self.position or self.position.mint!=mint:
            raise ValueError("unmatched sell fill")
        p=self.position
        values=(qty_sold,gross_received_usd,fees_usd,verified_remaining_qty)
        if not all(isfinite(float(x)) and float(x)>=0 for x in values):
            raise ValueError("invalid sell fill")
        if float(qty_sold)>p.qty or float(qty_sold)==0:
            raise ValueError("invalid sell quantity")
        proportion=float(qty_sold)/p.qty
        basis=p.cost_usd*proportion
        buy_fee=p.buy_fees_usd*proportion
        proceeds=float(gross_received_usd)-float(fees_usd)
        self.realized_usd+=proceeds-basis-buy_fee
        self.wallet_usd+=proceeds
        p.qty-=float(qty_sold)
        p.cost_usd-=basis
        p.buy_fees_usd-=buy_fee
        self.pending=None
        if float(verified_remaining_qty)==0 and p.qty<=1e-9:
            self.position=None
            self.phase=Phase.FLAT
            self.closed_trades+=1
        else:
            self.phase=Phase.OPEN
        return self.realized_usd
