"""Исполнение сделок.

`PaperExecutor` — демо: считает, будто сделка исполнилась по цене цели.
`LiveExecutor` (copytrade/live.py) — реальные свапы через Jupiter. Стратегия
не знает, какой из них подставлен: интерфейс один.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

log = logging.getLogger("copytrade.executor")

BPS = Decimal(10_000)


@dataclass(slots=True)
class BuyOrder:
    mint: str
    symbol: str
    decimals: int
    size_sol: Decimal           # сколько SOL готовы потратить
    price_sol: Decimal          # ожидаемая цена 1 токена в SOL
    sol_price_usd: Decimal
    target_signature: str
    slot: int = 0
    price_source: str = "tx"


@dataclass(slots=True)
class SellOrder:
    mint: str
    symbol: str
    decimals: int
    tokens: Decimal             # сколько токенов продаём
    price_sol: Decimal
    sol_price_usd: Decimal
    target_signature: str
    fraction: Decimal = Decimal(0)
    slot: int = 0
    price_source: str = "tx"


@dataclass(slots=True)
class ExecutionResult:
    ok: bool
    tokens: Decimal = Decimal(0)     # исполненное количество токенов
    sol_amount: Decimal = Decimal(0) # потрачено (buy) / получено (sell), в SOL
    price_sol: Decimal = Decimal(0)  # фактическая цена исполнения
    simulated: bool = True
    tx_signature: str | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Executor(ABC):
    """Контракт исполнителя. Стратегия не знает, демо это или живой свап."""

    name: str = "abstract"
    live: bool = False

    @abstractmethod
    async def buy(self, order: BuyOrder) -> ExecutionResult: ...

    @abstractmethod
    async def sell(self, order: SellOrder) -> ExecutionResult: ...

    async def close(self) -> None:  # pragma: no cover - переопределяется при нужде
        return None


class PaperExecutor(Executor):
    """Виртуальное исполнение по цене транзакции цели.

    Комиссии (PRIORITY_FEE_SOL + BRIBE_SOL) списываются всегда — это прямые
    расходы, и без них демо завышает PnL. А вот SLIPPAGE_BPS по умолчанию
    НЕ применяется: это допуск «хуже какой цены не соглашаться», а не
    ожидаемый убыток. Считать 20% проскальзывания на каждой сделке значило бы
    занизить результат в разы. Включается через PAPER_APPLY_SLIPPAGE=true.
    """

    name = "paper"
    live = False

    def __init__(self, slippage_bps: int = 0, fee_sol: Decimal = Decimal(0)) -> None:
        self.slippage = Decimal(slippage_bps) / BPS
        self.fee_sol = fee_sol

    async def buy(self, order: BuyOrder) -> ExecutionResult:
        if order.price_sol <= 0:
            return ExecutionResult(ok=False, error="нулевая или отрицательная цена входа")

        spend = order.size_sol - self.fee_sol
        if spend <= 0:
            return ExecutionResult(ok=False, error="размер позиции меньше комиссии")

        effective_price = order.price_sol * (Decimal(1) + self.slippage)
        tokens = spend / effective_price
        return ExecutionResult(
            ok=True,
            tokens=tokens,
            sol_amount=order.size_sol,
            price_sol=effective_price,
            simulated=True,
            meta={"slippage_bps": str(self.slippage * BPS), "fee_sol": str(self.fee_sol)},
        )

    async def sell(self, order: SellOrder) -> ExecutionResult:
        if order.price_sol <= 0:
            return ExecutionResult(ok=False, error="нулевая или отрицательная цена выхода")
        if order.tokens <= 0:
            return ExecutionResult(ok=False, error="нечего продавать")

        effective_price = order.price_sol * (Decimal(1) - self.slippage)
        proceeds = order.tokens * effective_price - self.fee_sol
        if proceeds < 0:
            proceeds = Decimal(0)
        return ExecutionResult(
            ok=True,
            tokens=order.tokens,
            sol_amount=proceeds,
            price_sol=effective_price,
            simulated=True,
            meta={"slippage_bps": str(self.slippage * BPS), "fee_sol": str(self.fee_sol)},
        )


def build_executor(cfg: Any, rpc: Any = None, session: Any = None) -> Executor:
    """PaperExecutor в демо, LiveExecutor в бою.

    Импорт живого модуля ленивый: `solders` нужен только для живой торговли,
    и демо не должно падать из-за отсутствующей зависимости.
    """
    if cfg.demo_mode:
        return PaperExecutor(
            slippage_bps=cfg.paper_slippage_bps,
            fee_sol=cfg.fixed_fee_sol,
        )

    if rpc is None or session is None:
        raise RuntimeError("Живому executor'у нужны rpc и http-сессия")

    from .live import LiveExecutor
    return LiveExecutor(cfg, rpc, session)
