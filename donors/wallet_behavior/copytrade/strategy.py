"""Правила копирования.

ПОКУПКА — копируем, если:
  * событие действительно покупка (потрачена квотная валюта, получен токен);
  * объём покупки цели в USD >= MIN_BUY_USD;
  * по этому минту у нас НЕТ открытой позиции (докупки цели только логируем).

ВЕДУЩИЙ (owner). Кошельков несколько, поэтому у каждой позиции есть ведущий —
тот, чья покупка её открыла. Копируем только его: покупки того же токена
другими отслеживаемыми пропускаем, его продажи зеркалим, чужие игнорируем.
Кто первый — решает позиция транзакции В ЦЕПОЧКЕ (слот/blockTime), а не порядок
обработки ботом: после реконнекта догон идёт вперемешку. Если более ранняя
покупка приезжает уже после открытия позиции, стратегия помечает это правилом
OWNER_EARLIER, а переназначает ведущего engine.

ПРОДАЖА — зеркалим ДОЛЮ, а не количество:
  * доля = (баланс цели ДО tx - баланс ПОСЛЕ) / баланс ДО, берётся из самой
    транзакции, поэтому она кумулятивна по построению и не ломается, если
    цель докупала токен без нас;
  * ту же долю применяем к НАШЕМУ текущему остатку;
  * баланс цели упал в ноль -> закрываем позицию целиком.

Стратегия ничего не исполняет и не мутирует портфель — только возвращает
решение. Так её можно прогонять на исторических данных отдельно от executor'а.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from .config import Config
from .constants import NATIVE_SOL, short_mint
from .models import CloseReason, EventKind, Position, TradeEvent
from .portfolio import Portfolio
from .prices import PriceService

log = logging.getLogger("copytrade.strategy")
ZERO = Decimal(0)
ONE = Decimal(1)


class Action(str, Enum):
    COPY_BUY = "COPY_BUY"
    SKIP_BUY = "SKIP_BUY"
    COPY_SELL = "COPY_SELL"
    SKIP_SELL = "SKIP_SELL"
    CLOSE_POSITION = "CLOSE_POSITION"
    LOG_ONLY = "LOG_ONLY"


@dataclass(slots=True)
class Decision:
    action: Action
    event: TradeEvent
    reason: str = ""
    price_sol: Decimal | None = None
    price_source: str = "tx"
    sol_volume: Decimal = ZERO       # объём операции ЦЕЛИ в SOL
    usd_volume: Decimal = ZERO       # объём операции ЦЕЛИ в USD
    fraction: Decimal = ZERO         # доля остатка, которую зеркалим
    sol_price_usd: Decimal = ZERO
    close_reason: CloseReason | None = None
    target_tokens_after: Decimal = ZERO
    details: dict[str, str] = field(default_factory=dict)

    @property
    def is_copy(self) -> bool:
        return self.action in (Action.COPY_BUY, Action.COPY_SELL)


class Strategy:
    def __init__(self, cfg: Config, portfolio: Portfolio, prices: PriceService) -> None:
        self.cfg = cfg
        self.portfolio = portfolio
        self.prices = prices

    async def decide(self, event: TradeEvent) -> Decision:
        if event.kind is EventKind.BUY:
            return await self._decide_buy(event)
        if event.kind is EventKind.SELL:
            return await self._decide_sell(event, close_reason=CloseReason.TARGET_SOLD_ALL)
        if event.kind is EventKind.TRANSFER_OUT:
            return await self._decide_transfer_out(event)
        return Decision(
            action=Action.LOG_ONLY,
            event=event,
            reason=event.note or event.kind.value.lower(),
        )

    # ------------------------------------------------------------------ #
    # покупка
    # ------------------------------------------------------------------ #

    async def _decide_buy(self, event: TradeEvent) -> Decision:
        sol_usd = await self.prices.sol_usd()
        if not sol_usd or sol_usd <= 0:
            return Decision(
                action=Action.SKIP_BUY, event=event,
                reason="нет курса SOL/USD — не с чем сравнивать MIN_BUY_USD",
            )

        target_tokens_after = event.token_amount_ui if event.token_post_raw <= 0 else (
            Decimal(event.token_post_raw) / (Decimal(10) ** event.decimals)
        )

        # 1. Уже держим -> либо докупка ведущего, либо покупка «не того» кошелька.
        held = self.portfolio.get(event.mint)
        if held is not None:
            if held.owned_by(event.wallet):
                return Decision(
                    action=Action.SKIP_BUY, event=event, sol_price_usd=sol_usd,
                    reason="докупка ведущего: позиция по токену уже открыта",
                    target_tokens_after=target_tokens_after,
                    details={"rule": "TARGET_RE_BUY", "owner": held.owner_wallet},
                )
            # Покупка пришла из блока РАНЬШЕ той, что открыла позицию: значит
            # ведущего назначили неверно (догон/реконнект переставил порядок).
            earlier = event.order_key < held.owner_key and not held.sells
            return Decision(
                action=Action.SKIP_BUY, event=event, sol_price_usd=sol_usd,
                reason=(
                    f"уже держим, ведущий — {held.owner_name or short_mint(held.owner_wallet)}"
                    + (" (его покупка оказалась позже — переназначаем)" if earlier else "")
                ),
                target_tokens_after=target_tokens_after,
                details={
                    "rule": "OWNER_EARLIER" if earlier else "NOT_OWNER",
                    "owner": held.owner_wallet,
                },
            )

        if not self.cfg.allow_reentry and self.portfolio.ever_traded(event.mint):
            return Decision(
                action=Action.SKIP_BUY, event=event, sol_price_usd=sol_usd,
                reason="повторный вход в уже закрытый токен запрещён (ALLOW_REENTRY=false)",
                target_tokens_after=target_tokens_after,
                details={"rule": "REENTRY_BLOCKED"},
            )

        price_sol, price_source = await self._price_for(event, sol_usd)

        # 2. Объём покупки цели в USD.
        sol_volume, usd_volume = self._quote_volume(event, sol_usd)
        if usd_volume <= 0:
            if price_sol is None or price_sol <= 0:
                return Decision(
                    action=Action.SKIP_BUY, event=event, sol_price_usd=sol_usd,
                    reason="не удалось оценить объём покупки (нет цены токена)",
                    target_tokens_after=target_tokens_after,
                    details={"rule": "NO_PRICE"},
                )
            sol_volume = event.token_amount_ui * price_sol
            usd_volume = sol_volume * sol_usd

        if usd_volume < self.cfg.min_buy_usd:
            return Decision(
                action=Action.SKIP_BUY, event=event, sol_price_usd=sol_usd,
                reason=(f"объём ${usd_volume:.2f} < MIN_BUY_USD ${self.cfg.min_buy_usd:.2f}"),
                sol_volume=sol_volume, usd_volume=usd_volume,
                target_tokens_after=target_tokens_after,
                details={"rule": "BELOW_MIN_USD"},
            )

        # 3. Цена входа обязательна: без неё не посчитать наш размер в токенах.
        if price_sol is None or price_sol <= 0:
            return Decision(
                action=Action.SKIP_BUY, event=event, sol_price_usd=sol_usd,
                reason="не удалось определить цену входа",
                sol_volume=sol_volume, usd_volume=usd_volume,
                target_tokens_after=target_tokens_after,
                details={"rule": "NO_PRICE"},
            )

        return Decision(
            action=Action.COPY_BUY, event=event, sol_price_usd=sol_usd,
            reason=f"первая покупка токена на ${usd_volume:.2f}",
            price_sol=price_sol, price_source=price_source,
            sol_volume=sol_volume, usd_volume=usd_volume,
            target_tokens_after=target_tokens_after,
        )

    # ------------------------------------------------------------------ #
    # продажа
    # ------------------------------------------------------------------ #

    async def _decide_sell(
        self, event: TradeEvent, *, close_reason: CloseReason
    ) -> Decision:
        position = self.portfolio.get(event.mint)
        sol_usd = await self.prices.sol_usd() or ZERO
        target_tokens_after = Decimal(max(0, event.token_post_raw)) / (Decimal(10) ** event.decimals)

        if position is None:
            return Decision(
                action=Action.LOG_ONLY, event=event, sol_price_usd=sol_usd,
                reason="продажа токена, который мы не копировали",
                target_tokens_after=target_tokens_after,
                details={"rule": "NOT_HELD"},
            )

        if not position.owned_by(event.wallet):
            # Позицию открыл другой кошелёк — его остаток мы и зеркалим.
            # Чужая продажа ничего не говорит о нашей доле в этой позиции.
            return Decision(
                action=Action.SKIP_SELL, event=event, sol_price_usd=sol_usd,
                reason=(f"продажа не от ведущего — позицию ведёт "
                        f"{position.owner_name or short_mint(position.owner_wallet)}"),
                target_tokens_after=target_tokens_after,
                details={"rule": "NOT_OWNER", "owner": position.owner_wallet},
            )

        fraction = ONE if event.target_closed_out else event.sold_fraction
        if fraction <= 0:
            return Decision(
                action=Action.SKIP_SELL, event=event, sol_price_usd=sol_usd,
                reason="баланс цели не уменьшился", target_tokens_after=target_tokens_after,
                details={"rule": "NO_DECREASE"},
            )
        fraction = min(fraction, ONE)

        # Доли кумулятивны по построению, поэтому пропущенные мелкие продажи
        # нельзя просто выбросить — иначе наш остаток навсегда разойдётся с
        # остатком ведущего. Копим (1 - доля) произведением и исполняем всё
        # разом, когда накопленное перевалит за порог.
        remaining_after = position.pending_sell_remaining * (ONE - fraction)
        effective = ONE - remaining_after
        threshold = self.cfg.min_sell_fraction

        # Полный выход ведущего порогом не блокируется никогда: закрыть позицию
        # важнее, чем сэкономить на комиссии.
        if not event.target_closed_out and threshold > 0 and effective < threshold:
            return Decision(
                action=Action.SKIP_SELL, event=event, sol_price_usd=sol_usd,
                reason=(
                    f"мелкая продажа {fraction * 100:.2f}% — накоплено "
                    f"{effective * 100:.2f}% при пороге {threshold * 100:g}%, "
                    "ждём, комиссию не жжём"
                ),
                fraction=fraction,
                target_tokens_after=target_tokens_after,
                details={"rule": "BELOW_MIN_FRACTION",
                         "pending_remaining": str(remaining_after),
                         "pending_fraction": str(effective)},
            )

        price_sol, price_source = await self._exit_price(event, position, sol_usd)
        sol_volume, usd_volume = self._quote_volume(event, sol_usd)

        if effective > fraction:
            reason = (
                f"цель продала {fraction * 100:.2f}% остатка; с учётом "
                f"{position.skipped_dust_sells} накопленных мелких продаж "
                f"зеркалим {effective * 100:.2f}%"
            )
        else:
            reason = (
                f"цель продала {fraction * 100:.2f}% своего остатка "
                f"({event.token_amount_ui:.4f} шт)"
            )

        return Decision(
            action=Action.COPY_SELL, event=event, sol_price_usd=sol_usd,
            reason=reason,
            price_sol=price_sol, price_source=price_source,
            sol_volume=sol_volume, usd_volume=usd_volume,
            fraction=effective,
            close_reason=close_reason if event.target_closed_out else None,
            target_tokens_after=target_tokens_after,
            details={"target_fraction": str(fraction)},
        )

    # ------------------------------------------------------------------ #
    # перевод на другой кошелёк
    # ------------------------------------------------------------------ #

    async def _decide_transfer_out(self, event: TradeEvent) -> Decision:
        position = self.portfolio.get(event.mint)
        sol_usd = await self.prices.sol_usd() or ZERO
        target_tokens_after = Decimal(max(0, event.token_post_raw)) / (Decimal(10) ** event.decimals)

        if position is None:
            return Decision(
                action=Action.LOG_ONLY, event=event, sol_price_usd=sol_usd,
                reason="вывод токена, который мы не копировали",
                target_tokens_after=target_tokens_after,
                details={"rule": "NOT_HELD"},
            )

        if not position.owned_by(event.wallet):
            return Decision(
                action=Action.LOG_ONLY, event=event, sol_price_usd=sol_usd,
                reason=(f"вывод не от ведущего — позицию ведёт "
                        f"{position.owner_name or short_mint(position.owner_wallet)}"),
                target_tokens_after=target_tokens_after,
                details={"rule": "NOT_OWNER", "owner": position.owner_wallet},
            )

        if self.cfg.treat_transfer_as_sell:
            decision = await self._decide_sell(event, close_reason=CloseReason.TRANSFER_OUT)
            decision.reason = "вывод токена засчитан как продажа (TREAT_TRANSFER_AS_SELL=true)"
            decision.price_source = decision.price_source or "market"
            return decision

        # Частичный вывод игнорируем, но полное обнуление баланса цели означает,
        # что зеркалить больше нечего — закрываем позицию по рынку.
        if event.target_closed_out and self.cfg.close_on_target_balance_zero:
            price_sol, price_source = await self._exit_price(event, position, sol_usd)
            return Decision(
                action=Action.CLOSE_POSITION, event=event, sol_price_usd=sol_usd,
                reason="баланс токена у цели обнулился выводом — закрываем позицию",
                price_sol=price_sol, price_source=price_source, fraction=ONE,
                close_reason=CloseReason.TRANSFER_OUT,
                target_tokens_after=ZERO,
            )

        return Decision(
            action=Action.LOG_ONLY, event=event, sol_price_usd=sol_usd,
            reason="частичный вывод, TREAT_TRANSFER_AS_SELL=false — только лог",
            target_tokens_after=target_tokens_after,
            details={"rule": "TRANSFER_IGNORED"},
        )

    # ------------------------------------------------------------------ #
    # цены
    # ------------------------------------------------------------------ #

    def _quote_volume(self, event: TradeEvent, sol_usd: Decimal) -> tuple[Decimal, Decimal]:
        """Объём операции цели -> (в SOL, в USD)."""
        if not event.quote_mint or event.quote_amount_ui <= 0:
            return ZERO, ZERO
        if event.quote_mint == NATIVE_SOL:
            sol_amount = event.quote_amount_ui
            return sol_amount, sol_amount * sol_usd
        usd_amount = event.quote_amount_ui  # USDC/USDT ~ 1 USD
        sol_amount = usd_amount / sol_usd if sol_usd > 0 else ZERO
        return sol_amount, usd_amount

    async def _price_for(
        self, event: TradeEvent, sol_usd: Decimal
    ) -> tuple[Decimal | None, str]:
        """Цена 1 токена в SOL. Приоритет — сама транзакция цели."""
        if event.exact_price and event.price_in_quote and event.price_in_quote > 0:
            if event.quote_mint == NATIVE_SOL:
                return event.price_in_quote, "tx"
            if sol_usd > 0:
                return event.price_in_quote / sol_usd, "tx"

        try:
            price = await self.prices.token_price_sol(event.mint)
        except Exception as exc:  # noqa: BLE001 - цена не должна ронять бота
            log.debug("Price API для %s недоступен: %s", short_mint(event.mint), exc)
            price = None
        return (price, "jupiter") if price and price > 0 else (None, "none")

    async def _exit_price(
        self, event: TradeEvent, position: Position, sol_usd: Decimal
    ) -> tuple[Decimal | None, str]:
        price, source = await self._price_for(event, sol_usd)
        if price and price > 0:
            return price, source
        # Цель вышла, а цены нет: лучше зафиксировать по последней известной,
        # чем оставить висящую позицию с неизвестным результатом.
        if position.last_price_sol and position.last_price_sol > 0:
            return position.last_price_sol, "last_known"
        return position.entry_price_sol, "entry_fallback"
