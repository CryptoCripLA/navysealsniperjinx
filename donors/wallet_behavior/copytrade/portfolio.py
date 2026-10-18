"""Учёт виртуальных позиций и PnL.

Модель PnL:
  * себестоимость остатка = остаток токенов x цена входа (средняя, она же
    единственная — докупки цели мы не копируем);
  * реализованный PnL в SOL = выручка - себестоимость проданной части;
  * реализованный PnL в USD считается ЧЕСТНО: выручка по курсу SOL на момент
    выхода минус вложение по курсу SOL на момент входа. Поэтому позиция может
    быть в плюсе по SOL и в минусе по USD, если SOL упал, — это не баг.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Iterable

from .models import (
    BotState,
    CloseReason,
    PartialSell,
    Position,
    PositionStatus,
)

log = logging.getLogger("copytrade.portfolio")

ZERO = Decimal(0)
# Ниже этой доли остаток считаем пылью и закрываем позицию: иначе после
# «продал всё» из-за округления Decimal остаётся 1e-30 токена.
DUST_FRACTION = Decimal("0.000001")


class Portfolio:
    def __init__(self, state: BotState) -> None:
        self._state = state

    # ------------------------------------------------------------------ #
    # доступ
    # ------------------------------------------------------------------ #

    @property
    def open_positions(self) -> dict[str, Position]:
        return self._state.open_positions

    @property
    def closed_positions(self) -> list[Position]:
        return self._state.closed_positions

    def get(self, mint: str) -> Position | None:
        return self._state.open_positions.get(mint)

    def has_open(self, mint: str) -> bool:
        return mint in self._state.open_positions

    def ever_traded(self, mint: str) -> bool:
        return self.has_open(mint) or any(p.mint == mint for p in self._state.closed_positions)

    def all_positions(self) -> Iterable[Position]:
        return self._state.all_positions()

    # ------------------------------------------------------------------ #
    # мутации
    # ------------------------------------------------------------------ #

    def open_position(
        self,
        *,
        mint: str,
        symbol: str,
        decimals: int,
        tokens: Decimal,
        cost_sol: Decimal,
        entry_price_sol: Decimal,
        sol_price_usd: Decimal,
        signature: str,
        target_tokens_after_buy: Decimal,
        target_buy_usd: Decimal,
        owner_wallet: str = "",
        owner_name: str = "",
        owner_slot: int = 0,
        owner_block_time: int | None = None,
        opened_at: float | None = None,
    ) -> Position:
        position = Position(
            mint=mint,
            symbol=symbol,
            decimals=decimals,
            opened_at=opened_at if opened_at is not None else time.time(),
            open_signature=signature,
            owner_wallet=owner_wallet,
            owner_name=owner_name,
            owner_slot=owner_slot,
            owner_block_time=owner_block_time,
            entry_price_sol=entry_price_sol,
            entry_price_usd=entry_price_sol * sol_price_usd,
            sol_price_usd_at_entry=sol_price_usd,
            cost_sol=cost_sol,
            tokens_initial=tokens,
            tokens_remaining=tokens,
            target_tokens_at_entry=target_tokens_after_buy,
            target_tokens_last_seen=target_tokens_after_buy,
            target_buy_usd=target_buy_usd,
            last_price_sol=entry_price_sol,
            last_price_ts=time.time(),
        )
        self._state.open_positions[mint] = position
        return position

    def register_sell(
        self,
        position: Position,
        *,
        fraction: Decimal,
        tokens_sold: Decimal,
        sol_received: Decimal,
        price_sol: Decimal,
        sol_price_usd: Decimal,
        signature: str,
        price_source: str = "tx",
        target_pre_raw: int = 0,
        target_post_raw: int = 0,
        ts: float | None = None,
    ) -> PartialSell:
        cost_sol = tokens_sold * position.entry_price_sol
        pnl_sol = sol_received - cost_sol
        pnl_usd = sol_received * sol_price_usd - cost_sol * position.sol_price_usd_at_entry

        sell = PartialSell(
            ts=ts if ts is not None else time.time(),
            signature=signature,
            fraction=fraction,
            tokens_sold=tokens_sold,
            price_sol=price_sol,
            sol_received=sol_received,
            sol_price_usd=sol_price_usd,
            realized_pnl_sol=pnl_sol,
            realized_pnl_usd=pnl_usd,
            price_source=price_source,
            target_pre_raw=target_pre_raw,
            target_post_raw=target_post_raw,
        )

        position.sells.append(sell)
        position.tokens_remaining -= tokens_sold
        if position.tokens_remaining < ZERO:
            position.tokens_remaining = ZERO
        position.sol_returned += sol_received
        position.realized_pnl_sol += pnl_sol
        position.realized_pnl_usd += pnl_usd
        position.last_price_sol = price_sol
        position.last_price_ts = sell.ts
        return sell

    def reassign_owner(
        self,
        position: Position,
        *,
        wallet: str,
        name: str,
        slot: int,
        block_time: int | None,
    ) -> None:
        """Переназначить ведущего, когда пришла более ранняя покупка.

        Меняется ТОЛЬКО атрибуция: кому засчитать позицию и чьи продажи
        зеркалить. Экономика входа (цена, стоимость, количество) остаётся
        как есть — мы действительно вошли по той цене, и переписать её
        задним числом значило бы соврать в PnL.
        """
        position.owner_wallet = wallet
        position.owner_name = name
        position.owner_slot = slot
        position.owner_block_time = block_time

    def owner_of(self, mint: str) -> str:
        position = self._state.open_positions.get(mint)
        return position.owner_wallet if position else ""

    def is_dust(self, position: Position) -> bool:
        if position.tokens_initial <= 0:
            return True
        return position.tokens_remaining / position.tokens_initial <= DUST_FRACTION

    def close(
        self,
        position: Position,
        reason: CloseReason,
        *,
        closed_at: float | None = None,
    ) -> Position:
        position.status = PositionStatus.CLOSED
        position.closed_at = closed_at if closed_at is not None else time.time()
        position.close_reason = reason
        position.tokens_remaining = ZERO
        self._state.open_positions.pop(position.mint, None)
        self._state.closed_positions.append(position)
        self._state.counters.positions_closed += 1
        return position

    def update_target_balance(self, mint: str, tokens: Decimal) -> None:
        position = self._state.open_positions.get(mint)
        if position is not None:
            position.target_tokens_last_seen = tokens

    def update_mark_price(self, mint: str, price_sol: Decimal) -> None:
        position = self._state.open_positions.get(mint)
        if position is not None and price_sol > 0:
            position.last_price_sol = price_sol
            position.last_price_ts = time.time()

    def set_symbol(self, mint: str, symbol: str) -> None:
        if not symbol:
            return
        self._state.symbols[mint] = symbol
        position = self._state.open_positions.get(mint)
        if position is not None and not position.symbol:
            position.symbol = symbol
