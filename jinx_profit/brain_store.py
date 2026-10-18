"""Snapshot JINX accounting state. Pair this with the order journal on restart.

A snapshot is local recovery evidence, not proof of wallet inventory.
Production startup must compare all restored quantities against on-chain balances.
"""
import json
import os
from dataclasses import asdict
from pathlib import Path
from .brain import ProfitBrain, Intent, Position, Phase

class BrainStore:
    def __init__(self, path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)

    def save(self, brain):
        record={"mode":brain.mode.value,"phase":brain.phase.value,
                "wallet_usd":brain.wallet_usd,"reserve_usd":brain.reserve_usd,
                "minimum_probability":brain.minimum_probability,
                "position":asdict(brain.position) if brain.position else None,
                "pending":asdict(brain.pending) if brain.pending else None,
                "realized_usd":brain.realized_usd,"closed_trades":brain.closed_trades}
        temp=self.path.with_suffix(".tmp")
        with temp.open("w",encoding="utf-8") as f:
            json.dump(record,f,sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp,self.path)

    def load(self, *, profit_model=None,sizing_model=None):
        d=json.loads(self.path.read_text(encoding="utf-8"))
        brain=ProfitBrain(mode=d["mode"],wallet_usd=d["wallet_usd"],
              reserve_usd=d["reserve_usd"],minimum_probability=d["minimum_probability"],
              profit_model=profit_model,sizing_model=sizing_model)
        brain.phase=Phase(d["phase"])
        brain.position=Position(**d["position"]) if d["position"] else None
        brain.pending=Intent(**d["pending"]) if d["pending"] else None
        brain.realized_usd=float(d["realized_usd"])
        brain.closed_trades=int(d["closed_trades"])
        if (brain.phase in (Phase.BUY_PENDING,Phase.SELL_PENDING)) != (brain.pending is not None):
            raise ValueError("inconsistent persisted pending state")
        if (brain.phase in (Phase.OPEN,Phase.SELL_PENDING)) != (brain.position is not None):
            raise ValueError("inconsistent persisted position state")
        return brain
