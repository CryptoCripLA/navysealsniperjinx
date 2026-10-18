"""JINX donor #4: use Alexander Moran's ORIGINAL exit strategies directly.

The source is imported under research/alexanderlmoran/exit_lab.py; no reimplementation
of its strategy formulas here. Historical counterfactuals are research only.
"""
from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

UPSTREAM = Path(__file__).resolve().parents[1] / "research" / "alexanderlmoran" / "exit_lab.py"
_MODULE_NAME = "jinx_profit_original_alexander_exit_lab"
_STRATEGIES = {
    "baseline": "strategy_baseline",
    "trail_6": "strategy_fixed_trail_6",
    "trail_8": "strategy_fixed_trail_8",
    "tiered": "strategy_tiered_trail",
    "time_tighten": "strategy_time_tighten",
    "partial_trail": "strategy_partial_tp_trail",
}

def original_exit_module():
    """Load original donor file without executing its CLI/main."""
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    if not UPSTREAM.is_file():
        raise FileNotFoundError(f"missing original Moran donor: {UPSTREAM}")
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, UPSTREAM)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return mod

def compare_historical_exits(envelope):
    """Run donor's exact strategy functions on its original TradeEnvelope.

    The donor's MFE-envelope simulator is OPTIMISTIC and cannot be used as an
    executable fill quote or live ROI report. Use for relative research only.
    """
    mod = original_exit_module()
    if not isinstance(envelope, mod.TradeEnvelope):
        raise TypeError("expected original Moran TradeEnvelope")
    return {key: float(getattr(mod, fn)(envelope)) for key, fn in _STRATEGIES.items()}

def trailing_exit_signal(prices_usd, trail_fraction=0.06):
    """Time-ordered peak-drawdown decision, not Moran's hindsight MFE estimate.

    Only received observations count. An exit signal requests a real executable
    sell quote and the central lifecycle handles the order.
    """
    if not 0 < trail_fraction < 1:
        raise ValueError("invalid trailing fraction")
    peak = None
    for price in prices_usd:
        p = float(price)
        if not 0 < p < float("inf"):
            raise ValueError("invalid observed price")
        peak = max(peak or p, p)
        if p <= peak * (1.0 - trail_fraction):
            return True
    return False
