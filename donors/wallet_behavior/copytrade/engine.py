"""Оркестратор: очередь подписей -> разбор -> решение -> исполнение -> учёт."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import Config
from .constants import short_mint
from .executor import BuyOrder, Executor, SellOrder
from .logging_setup import EventLogger
from .models import BotState, CloseReason, EventKind, Position, TradeEvent, WalletActivity
from .monitor import SignatureRef, WalletMonitor
from .parser import classify, parse_wallet_activity
from .portfolio import Portfolio
from .prices import PriceService
from .rpc import RpcClient
from .stats import (
    build_summary,
    export_positions_csv,
    export_summary_txt,
    export_trades_csv,
    export_wallets_csv,
    human_duration,
    render_summary,
)
from .strategy import Action, Decision, Strategy

log = logging.getLogger("copytrade.engine")
ZERO = Decimal(0)
ONE = Decimal(1)


def solscan(signature: str) -> str:
    return f"https://solscan.io/tx/{signature}"


class Engine:
    def __init__(
        self,
        cfg: Config,
        state: BotState,
        rpc: RpcClient,
        prices: PriceService,
        executor: Executor,
        store: Any,
        events: EventLogger,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.rpc = rpc
        self.prices = prices
        self.executor = executor
        self.store = store
        self.events = events

        self.portfolio = Portfolio(state)
        self.strategy = Strategy(cfg, self.portfolio, prices)
        self.queue: asyncio.Queue[SignatureRef] = asyncio.Queue(maxsize=10_000)
        self.monitor = WalletMonitor(
            cfg, rpc, self.queue,
            cursor=lambda wallet: self.state.cursor(wallet).last_signature or None,
            on_cursor_init=self._init_cursor,
            on_ws_state=self._on_ws_state,
        )

        # Ключ дедупликации — «кошелёк:подпись», а не просто подпись: одна
        # транзакция может задеть сразу двух отслеживаемых (например, оба
        # купили в одном свапе-агрегаторе), и разобрать её нужно для каждого.
        self._processed_order: deque[str] = deque(
            state.processed_signatures, maxlen=cfg.max_processed_signatures
        )
        self._processed: set[str] = set(self._processed_order)
        self._stop = asyncio.Event()
        self._dirty = False
        # Граница «прошлое / настоящее» для живого режима: всё, что произошло
        # раньше, пришло из догона истории и покупкой не копируется.
        self._started_at = time.time()

    # ------------------------------------------------------------------ #
    # жизненный цикл
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        self._log_startup()
        tasks = [
            asyncio.create_task(self.monitor.run(), name="monitor"),
            asyncio.create_task(self._worker(), name="worker"),
            asyncio.create_task(self._mark_loop(), name="marks"),
            asyncio.create_task(self._stats_loop(), name="stats"),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        finally:
            self._stop.set()
            self.monitor.stop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()
        self.monitor.stop()

    async def shutdown(self) -> None:
        """Финальное сохранение + сводка + CSV. Вызывать всегда, даже при Ctrl+C."""
        try:
            await self.print_summary(export=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось построить финальную сводку: %s", exc)
        self._flush_state(force=True)

    def _log_startup(self) -> None:
        mode = "ДЕМО (paper trading)" if self.cfg.demo_mode else "ЖИВАЯ ТОРГОВЛЯ"
        self.events.emit(
            "STARTUP",
            f"{mode}: кошельков {len(self.cfg.wallets)}, размер позиции "
            f"{self.cfg.position_size_sol} SOL, MIN_BUY_USD ${self.cfg.min_buy_usd}, "
            f"executor={self.executor.name}",
            wallets=len(self.cfg.wallets),
            position_size_sol=self.cfg.position_size_sol,
            min_buy_usd=self.cfg.min_buy_usd,
            treat_transfer_as_sell=self.cfg.treat_transfer_as_sell,
            slippage_bps=self.cfg.slippage_bps,
            fee_sol=self.cfg.fixed_fee_sol,
            log_file=self.cfg.log_file,
        )
        if not self.cfg.demo_mode:
            guards = []
            if self.cfg.skip_buys_before_start:
                guards.append("покупки только с момента старта")
            if self.cfg.max_signal_age_sec > 0:
                guards.append(f"сигнал не старше {self.cfg.max_signal_age_sec:g} c")
            self.events.emit(
                "FRESHNESS",
                "Свежесть сигналов: " + (", ".join(guards) or "НЕ ПРОВЕРЯЕТСЯ")
                + ". Продажи из догона зеркалятся всегда",
                level=logging.INFO if guards else logging.WARNING,
                skip_buys_before_start=self.cfg.skip_buys_before_start,
                max_signal_age_sec=self.cfg.max_signal_age_sec,
            )
        for spec in self.cfg.wallets:
            cursor = self.state.cursors.get(spec.address)
            self.events.emit(
                "WALLET",
                f"№{spec.index} {spec.name}: {spec.address}"
                + (f" (курсор {cursor.last_signature[:16]}…)"
                   if cursor and cursor.last_signature else " (новый)"),
                wallet=spec.address, name=spec.name, index=spec.index,
            )

    def _on_ws_state(self, up: bool, reason: str) -> None:
        if up:
            self.events.emit("WS_UP", f"WebSocket подписан на логи кошельков ({reason})")
        else:
            self.events.emit("WS_DOWN", f"WebSocket недоступен: {reason}", level=logging.WARNING)

    def _init_cursor(self, wallet: str, signature: str, slot: int) -> None:
        cursor = self.state.cursor(wallet)
        cursor.last_signature = signature
        cursor.last_slot = slot
        self._dirty = True
        self._flush_state(force=True)

    # ------------------------------------------------------------------ #
    # обработка очереди
    # ------------------------------------------------------------------ #

    async def _worker(self) -> None:
        """Буфер переупорядочивания: WS и поллер приходят вразнобой.

        Каждая подпись отлёживается REORDER_WINDOW_MS, после чего пачка
        обрабатывается по возрастанию слота — чтобы продажа не обогнала покупку.
        """
        delay = self.cfg.reorder_window_ms / 1000.0
        buffer: list[tuple[float, SignatureRef]] = []
        loop = asyncio.get_running_loop()

        while not self._stop.is_set():
            timeout = 0.5
            if buffer:
                timeout = max(0.0, min(timeout, buffer[0][0] - loop.time()))
            try:
                ref = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                ref = None

            if ref is not None and self._key(ref) not in self._processed:
                buffer.append((loop.time() + delay, ref))

            now = loop.time()
            ready = [item for item in buffer if item[0] <= now]
            if not ready:
                continue
            buffer = [item for item in buffer if item[0] > now]
            # Порядок — как в цепочке: слот, затем подпись. Это же основа
            # выбора ведущего, поэтому сортируем строго, а не «как пришло».
            ready.sort(key=lambda item: (item[1].slot or 0, item[1].signature))

            for _, pending in ready:
                try:
                    await self._process_signature(pending)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - одна tx не должна ронять бота
                    self.state.counters.tx_failed_parse += 1
                    self.events.emit(
                        "ERROR", f"Ошибка обработки {pending.signature}: {exc}",
                        level=logging.ERROR, signature=pending.signature,
                    )
                    log.debug("Трейс:", exc_info=True)
            self._flush_state()

    @staticmethod
    def _key(ref: SignatureRef) -> str:
        return f"{ref.wallet}:{ref.signature}" if ref.wallet else ref.signature

    async def _process_signature(self, ref: SignatureRef) -> None:
        if self._key(ref) in self._processed:
            return
        if not ref.wallet:
            log.debug("Подпись %s… без кошелька — пропуск", ref.signature[:16])
            return

        tx = await self.rpc.get_transaction_with_retry(
            ref.signature,
            attempts=self.cfg.tx_fetch_retries,
            delay=self.cfg.tx_fetch_retry_delay_sec,
        )
        if tx is None:
            self.state.counters.tx_failed_parse += 1
            self.events.emit(
                "ERROR",
                f"getTransaction вернул null для {ref.signature} — транзакция пропущена",
                level=logging.ERROR, signature=ref.signature, url=solscan(ref.signature),
            )
            self._mark_processed(ref)
            return

        activity = parse_wallet_activity(tx, ref.wallet)
        if activity is None:
            self.state.counters.tx_failed_parse += 1
            self._mark_processed(ref)
            return

        self.state.counters.tx_processed += 1

        if not activity.succeeded:
            log.debug("tx %s… неуспешна (%s) — пропуск", ref.signature[:16], activity.err)
            self._mark_processed(ref, activity)
            return

        if not activity.touched_wallet:
            log.debug("tx %s… не изменила балансы %s — пропуск",
                      ref.signature[:16], short_mint(ref.wallet))
            self._mark_processed(ref, activity)
            return

        events = classify(
            activity,
            sol_dust_lamports=self.cfg.sol_dust_lamports,
            stable_dust_raw=self.cfg.stable_dust_raw,
        )

        if not events:
            self.events.emit(
                "TX_IGNORED",
                f"{ref.signature[:12]}… движение балансов без торговли "
                f"(dSOL {activity.sol_delta:+.6f})",
                level=logging.DEBUG, signature=ref.signature,
            )
            self._mark_processed(ref, activity)
            return

        for event in events:
            await self._handle_event(event, activity)

        self._mark_processed(ref, activity)

    def _mark_processed(self, ref: SignatureRef, activity: WalletActivity | None = None) -> None:
        key = self._key(ref)
        if key not in self._processed:
            if len(self._processed_order) == self._processed_order.maxlen:
                self._processed.discard(self._processed_order[0])
            self._processed_order.append(key)
            self._processed.add(key)

        # Курсор строго свой у каждого кошелька: until= в
        # getSignaturesForAddress работает в рамках одного адреса.
        cursor = self.state.cursor(ref.wallet)
        slot = (activity.slot if activity else 0) or ref.slot
        if slot >= cursor.last_slot:
            cursor.last_slot = slot
            cursor.last_signature = ref.signature
        self._dirty = True

    # ------------------------------------------------------------------ #
    # события -> решения -> исполнение
    # ------------------------------------------------------------------ #

    async def _handle_event(self, event: TradeEvent, activity: WalletActivity) -> None:
        if event.kind is EventKind.WRAP_UNWRAP:
            log.debug("tx %s…: wrap/unwrap %s SOL — игнор", event.signature[:12], event.token_amount_ui)
            return
        if event.kind is EventKind.TRANSFER_IN:
            log.debug("tx %s…: приход %s %s без траты — игнор",
                      event.signature[:12], event.token_amount_ui, short_mint(event.mint))
            return

        decision = await self.strategy.decide(event)
        counters = self.state.counters
        wallet_stats = self.state.wallet_stats(event.wallet)
        who = self._wallet(event.wallet)

        if event.kind is EventKind.BUY:
            counters.buys_detected += 1
            wallet_stats.buys_detected += 1
            self.events.emit(
                "BUY_DETECTED",
                f"{who} купил {event.token_amount_ui:,.4f} {self._label(event.mint)} "
                f"за {decision.sol_volume:.4f} SOL (${decision.usd_volume:,.2f}) | "
                f"{solscan(event.signature)}",
                signature=event.signature, mint=event.mint, wallet=event.wallet,
                usd_volume=decision.usd_volume, sol_volume=decision.sol_volume,
                price_sol=decision.price_sol, price_source=decision.price_source,
            )
        elif event.kind is EventKind.SELL:
            counters.sells_detected += 1
            wallet_stats.sells_detected += 1
            self.events.emit(
                "SELL_DETECTED",
                f"{who} продал {event.token_amount_ui:,.4f} {self._label(event.mint)} "
                f"({event.sold_fraction * 100:.2f}% остатка) за {decision.sol_volume:.4f} SOL | "
                f"{solscan(event.signature)}",
                signature=event.signature, mint=event.mint, wallet=event.wallet,
                fraction=event.sold_fraction, sol_volume=decision.sol_volume,
            )
        elif event.kind is EventKind.TRANSFER_OUT:
            counters.transfers_out += 1
            wallet_stats.transfers_out += 1
            self.events.emit(
                "TRANSFER_OUT",
                f"{who} вывел {event.token_amount_ui:,.4f} {self._label(event.mint)} "
                f"(остаток {decision.target_tokens_after:,.4f}) | {solscan(event.signature)}",
                signature=event.signature, mint=event.mint, wallet=event.wallet,
                treat_as_sell=self.cfg.treat_transfer_as_sell,
            )

        # Остаток цели обновляем только по ведущему: зеркалим-то его долю.
        position = self.portfolio.get(event.mint)
        if position is not None and position.owned_by(event.wallet):
            self.portfolio.update_target_balance(event.mint, decision.target_tokens_after)

        if decision.action is Action.COPY_BUY:
            blocked, rule = self._buy_block(event)
            if blocked:
                counters.buys_skipped += 1
                wallet_stats.buys_skipped += 1
                self.events.emit(
                    "BUY_SKIPPED",
                    f"{self._label(event.mint)}: {who} — {blocked}",
                    level=logging.WARNING, signature=event.signature,
                    mint=event.mint, wallet=event.wallet, rule=rule,
                )
            else:
                await self._execute_buy(decision, activity)
        elif decision.action is Action.SKIP_BUY:
            counters.buys_skipped += 1
            wallet_stats.buys_skipped += 1
            if decision.details.get("rule") == "OWNER_EARLIER":
                self._reassign_owner(decision)
            self.events.emit(
                "BUY_SKIPPED",
                f"{self._label(event.mint)}: {who} — {decision.reason} | "
                f"{solscan(event.signature)}",
                signature=event.signature, mint=event.mint, wallet=event.wallet,
                rule=decision.details.get("rule", ""), usd_volume=decision.usd_volume,
            )
        elif decision.action in (Action.COPY_SELL, Action.CLOSE_POSITION):
            await self._execute_sell(decision)
        elif decision.action is Action.SKIP_SELL:
            counters.sells_skipped += 1
            wallet_stats.sells_skipped += 1
            if decision.details.get("rule") == "BELOW_MIN_FRACTION":
                self._defer_dust_sell(decision)
            self.events.emit(
                "SELL_SKIPPED",
                f"{self._label(event.mint)}: {who} — {decision.reason}",
                signature=event.signature, mint=event.mint, wallet=event.wallet,
                rule=decision.details.get("rule", ""),
                fraction=decision.fraction,
                pending_fraction=decision.details.get("pending_fraction", ""),
            )
        else:
            log.debug("tx %s… %s (%s): %s",
                      event.signature[:12], event.kind.value, who, decision.reason)

    def _defer_dust_sell(self, decision: Decision) -> None:
        """Запомнить пропущенную мелкую продажу, чтобы исполнить её позже.

        Храним произведение (1 - доля), а не сумму долей: доли считаются от
        ОСТАТКА, и две продажи по 5% — это 9.75% исходного, а не 10%.
        """
        position = self.portfolio.get(decision.event.mint)
        if position is None:
            return
        position.pending_sell_remaining = Decimal(decision.details["pending_remaining"])
        position.skipped_dust_sells += 1
        self._dirty = True

    def realized_pnl_today(self, now: float | None = None) -> Decimal:
        """Реализованный PnL с начала текущих локальных суток.

        Считается по частичным продажам, а не по закрытым позициям: позиция,
        открытая вчера и распроданная сегодня, должна попасть в сегодняшний
        лимит убытка целиком, а не по дате закрытия.
        """
        midnight = time.mktime(time.localtime(now or time.time())[:3] + (0, 0, 0, 0, 0, -1))
        total = ZERO
        positions = list(self.state.open_positions.values()) + list(self.state.closed_positions)
        for position in positions:
            for sell in position.sells:
                if sell.ts >= midnight:
                    total += sell.realized_pnl_sol
        return total

    def _buy_block(self, event: TradeEvent) -> tuple[str, str]:
        """Все причины не копировать покупку. ('', '') — путь свободен.

        Возвращает пару (текст для лога, код правила для JSONL).
        """
        if self.cfg.demo_mode:
            return "", ""
        stale = self._freshness_block(event)
        if stale:
            return stale, "STALE_SIGNAL"
        risk = self._risk_block()
        if risk:
            return risk, "RISK_LIMIT"
        return "", ""

    def _freshness_block(self, event: TradeEvent) -> str:
        """Свежесть сигнала на покупку. Пустая строка — путь свободен.

        Только живой режим и только ПОКУПКИ. Продажи из догона пропускать
        нельзя: после простоя они единственный способ узнать, что цель вышла,
        пока мы сидели в реальных токенах. Асимметрия намеренная — опоздавшая
        покупка стоит денег, опоздавшая продажа их спасает.

        В демо не применяется: там догон и нужен — он даёт ту самую точную
        историческую статистику, ради которой демо запускается.
        """
        if self.cfg.demo_mode:
            return ""

        block_time = event.block_time
        if not block_time:
            # Без blockTime возраст не определить. На реальных деньгах это
            # повод не покупать, а не повод закрыть глаза.
            return "у транзакции нет blockTime — свежесть сигнала не проверить"

        if self.cfg.skip_buys_before_start and block_time < self._started_at:
            behind = self._started_at - block_time
            return (f"покупка из прошлого: за {human_duration(behind)} до старта бота. "
                    "Догон истории покупками не копируется (SKIP_BUYS_BEFORE_START)")

        age = time.time() - block_time
        if 0 < self.cfg.max_signal_age_sec < age:
            return (f"сигнал устарел: {human_duration(age)} при пороге "
                    f"{self.cfg.max_signal_age_sec:g} c (MAX_SIGNAL_AGE_SEC)")
        return ""

    def _risk_block(self) -> str:
        """Лимиты живого режима. Пустая строка — путь свободен.

        В демо не применяются намеренно: кап на число позиций исказил бы
        статистику, ради которой демо и запускается.
        """
        if self.cfg.demo_mode:
            return ""

        open_count = len(self.state.open_positions)
        if open_count >= self.cfg.max_open_positions:
            return (f"лимит открытых позиций: {open_count}/"
                    f"{self.cfg.max_open_positions} (MAX_OPEN_POSITIONS)")

        today = self.realized_pnl_today()
        if today <= -self.cfg.max_daily_loss_sol:
            return (f"дневной лимит убытка исчерпан: {today:.4f} SOL при пороге "
                    f"-{self.cfg.max_daily_loss_sol} SOL (MAX_DAILY_LOSS_SOL). "
                    "Покупки остановлены до смены суток")
        return ""

    def _reassign_owner(self, decision: Decision) -> None:
        """Более ранняя покупка приехала позже — чиним атрибуцию.

        Такое бывает после реконнекта: догон отдаёт транзакции пачками, и
        кошелёк, купивший вторым, может успеть открыть позицию первым.
        Переписываем ТОЛЬКО ведущего — вход остаётся по фактической цене,
        иначе PnL станет выдумкой.
        """
        event = decision.event
        position = self.portfolio.get(event.mint)
        if position is None or position.sells:
            return

        previous = position.owner_name or short_mint(position.owner_wallet)
        previous_slot = position.owner_slot
        self.portfolio.reassign_owner(
            position,
            wallet=event.wallet,
            name=self.cfg.wallet_name(event.wallet),
            slot=event.slot,
            block_time=event.block_time,
        )
        self.state.counters.owner_reassigned += 1
        self.events.emit(
            "OWNER_REASSIGNED",
            f"{self._label(event.mint)}: ведущий сменён {previous} -> "
            f"{position.owner_name} (его покупка в слоте {event.slot} раньше, "
            f"чем {position.open_signature[:12]}… в слоте {previous_slot}). "
            f"Цена входа не меняется — она уже исполнена.",
            level=logging.WARNING,
            mint=event.mint, wallet=event.wallet, signature=event.signature,
            previous_owner=previous, slot=event.slot,
        )
        self._flush_state(force=True)

    async def _execute_buy(self, decision: Decision, activity: WalletActivity) -> None:
        event = decision.event
        symbol = await self._resolve_symbol(event.mint)
        order = BuyOrder(
            mint=event.mint,
            symbol=symbol,
            decimals=event.decimals,
            size_sol=self.cfg.position_size_sol,
            price_sol=decision.price_sol or ZERO,
            sol_price_usd=decision.sol_price_usd,
            target_signature=event.signature,
            slot=event.slot,
            price_source=decision.price_source,
        )
        result = await self.executor.buy(order)
        if not result.ok:
            self.state.counters.buys_skipped += 1
            self.state.wallet_stats(event.wallet).buys_skipped += 1
            self.events.emit(
                "BUY_SKIPPED",
                f"{symbol}: исполнение не удалось — {result.error}",
                level=logging.WARNING, signature=event.signature, mint=event.mint,
                wallet=event.wallet,
            )
            return

        owner_name = self.cfg.wallet_name(event.wallet)
        position = self.portfolio.open_position(
            mint=event.mint,
            symbol=symbol,
            decimals=event.decimals,
            tokens=result.tokens,
            cost_sol=result.sol_amount,
            entry_price_sol=result.price_sol,
            sol_price_usd=decision.sol_price_usd,
            signature=event.signature,
            target_tokens_after_buy=decision.target_tokens_after,
            target_buy_usd=decision.usd_volume,
            owner_wallet=event.wallet,
            owner_name=owner_name,
            owner_slot=event.slot,
            owner_block_time=event.block_time,
            opened_at=float(event.block_time) if event.block_time else time.time(),
        )
        self.portfolio.set_symbol(event.mint, symbol)
        self.state.counters.buys_copied += 1
        self.state.wallet_stats(event.wallet).buys_copied += 1

        self.events.emit(
            "BUY_COPIED",
            f"{symbol}: куплено {result.tokens:,.4f} шт на {result.sol_amount:.4f} SOL "
            f"по {result.price_sol:.12f} SOL/шт "
            f"(${position.entry_price_usd:.10f}, цена: {decision.price_source}) | "
            f"ведущий — {owner_name} | {decision.reason}",
            signature=event.signature, mint=event.mint, symbol=symbol,
            wallet=event.wallet, owner=owner_name,
            tokens=result.tokens, cost_sol=result.sol_amount,
            entry_price_sol=result.price_sol, price_source=decision.price_source,
            target_buy_usd=decision.usd_volume,
        )
        self._flush_state(force=True)

    async def _execute_sell(self, decision: Decision) -> None:
        event = decision.event
        position = self.portfolio.get(event.mint)
        if position is None:
            return

        closing = decision.action is Action.CLOSE_POSITION or decision.fraction >= ONE
        fraction = ONE if closing else decision.fraction
        tokens_to_sell = position.tokens_remaining if closing else position.tokens_remaining * fraction
        if tokens_to_sell <= 0:
            self.portfolio.close(position, decision.close_reason or CloseReason.DUST)
            self._emit_closed(position, decision, note="остаток был нулевым")
            return

        order = SellOrder(
            mint=event.mint,
            symbol=position.symbol,
            decimals=event.decimals,
            tokens=tokens_to_sell,
            price_sol=decision.price_sol or position.entry_price_sol,
            sol_price_usd=decision.sol_price_usd,
            target_signature=event.signature,
            fraction=fraction,
            slot=event.slot,
            price_source=decision.price_source,
        )
        result = await self.executor.sell(order)
        if not result.ok:
            self.state.counters.sells_skipped += 1
            self.state.wallet_stats(event.wallet).sells_skipped += 1
            self.events.emit(
                "SELL_SKIPPED",
                f"{position.label()}: исполнение не удалось — {result.error}",
                level=logging.WARNING, signature=event.signature, mint=event.mint,
                wallet=event.wallet,
            )
            return

        sell = self.portfolio.register_sell(
            position,
            fraction=fraction,
            tokens_sold=result.tokens,
            sol_received=result.sol_amount,
            price_sol=result.price_sol,
            sol_price_usd=decision.sol_price_usd,
            signature=event.signature,
            price_source=decision.price_source,
            target_pre_raw=event.token_pre_raw,
            target_post_raw=event.token_post_raw,
            ts=float(event.block_time) if event.block_time else time.time(),
        )
        self.state.counters.sells_copied += 1
        self.state.wallet_stats(event.wallet).sells_copied += 1
        # Накопленное исполнено — начинаем копить заново. Сбрасываем только
        # после УСПЕШНОЙ продажи: иначе провал исполнения потерял бы долю.
        position.pending_sell_remaining = ONE
        position.skipped_dust_sells = 0

        self.events.emit(
            "SELL_COPIED",
            f"{position.label()}: продано {fraction * 100:.2f}% остатка = "
            f"{result.tokens:,.4f} шт по {result.price_sol:.12f} SOL/шт -> "
            f"{result.sol_amount:.4f} SOL, PnL {sell.realized_pnl_sol:+.4f} SOL "
            f"({sell.realized_pnl_usd:+.2f}$) | ведущий — {position.owner_name or '?'} "
            f"| цена: {decision.price_source}",
            signature=event.signature, mint=event.mint, symbol=position.symbol,
            wallet=event.wallet, owner=position.owner_wallet,
            fraction=fraction, tokens=result.tokens, sol_received=result.sol_amount,
            realized_pnl_sol=sell.realized_pnl_sol, realized_pnl_usd=sell.realized_pnl_usd,
            price_source=decision.price_source,
        )

        if closing or self.portfolio.is_dust(position):
            reason = decision.close_reason or (
                CloseReason.TARGET_SOLD_ALL if closing else CloseReason.DUST
            )
            self.portfolio.close(position, reason,
                                 closed_at=float(event.block_time) if event.block_time else None)
            self._emit_closed(position, decision)

        self._flush_state(force=True)

    def _emit_closed(self, position: Position, decision: Decision, note: str = "") -> None:
        total_sol = position.realized_pnl_sol
        total_usd = position.realized_pnl_usd
        pct = (total_sol / position.cost_sol * Decimal(100)) if position.cost_sol > 0 else ZERO
        self.events.emit(
            "POSITION_CLOSED",
            f"{position.label()}: закрыта ({(position.close_reason.value if position.close_reason else '')}"
            f"{', ' + note if note else ''}). "
            f"Итог {total_sol:+.4f} SOL / {total_usd:+.2f}$ ({pct:+.2f}%), "
            f"ведущий — {position.owner_name or '?'}, "
            f"частичных продаж {len(position.sells)}, холд {human_duration(position.hold_seconds())}",
            signature=decision.event.signature, mint=position.mint, symbol=position.symbol,
            owner=position.owner_wallet, owner_name=position.owner_name,
            pnl_sol=total_sol, pnl_usd=total_usd, pnl_pct=pct,
            close_reason=position.close_reason.value if position.close_reason else "",
            sells=len(position.sells),
        )

    # ------------------------------------------------------------------ #
    # фон: переоценка и сводки
    # ------------------------------------------------------------------ #

    async def _mark_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.cfg.mark_refresh_interval_sec
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._refresh_marks()
            except Exception as exc:  # noqa: BLE001
                log.debug("Переоценка позиций не удалась: %s", exc)

    async def _refresh_marks(self) -> dict[str, Decimal]:
        mints = list(self.portfolio.open_positions)
        if not mints:
            return {}
        marks = await self.prices.token_prices_sol(mints)
        for mint, price in marks.items():
            self.portfolio.update_mark_price(mint, price)

        # Заодно подтягиваем символы, которые не успели получить при покупке.
        unnamed = [m for m, p in self.portfolio.open_positions.items()
                   if not p.symbol or p.symbol == short_mint(m)]
        if unnamed:
            try:
                metas = await self.prices.token_meta_many(unnamed)
            except Exception:  # noqa: BLE001
                metas = {}
            for mint, meta in metas.items():
                if meta.symbol:
                    self.portfolio.set_symbol(mint, meta.symbol)
                    position = self.portfolio.get(mint)
                    if position is not None:
                        position.symbol = meta.symbol
        return marks

    async def _stats_loop(self) -> None:
        if self.cfg.stats_interval_sec <= 0:
            return
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.stats_interval_sec)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.print_summary(export=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось напечатать сводку: %s", exc)

    async def print_summary(self, *, export: bool = False) -> None:
        marks = await self._refresh_marks()
        sol_usd = await self.prices.sol_usd() or ZERO
        summary = build_summary(self.state, marks, sol_usd)
        print(render_summary(summary, self.state, color=self.cfg.color))

        # Файл пишем всегда, а не только при export: он обязан отражать
        # последнюю показанную сводку, иначе смысла в нём нет.
        self._write_summary_file(summary)

        if export:
            export_positions_csv(summary, self.cfg.positions_csv)
            export_trades_csv(self.state, self.cfg.trades_csv)
            export_wallets_csv(summary, self.state, self.cfg.wallets_csv)
            log.debug("CSV обновлены: %s, %s, %s",
                      self.cfg.positions_csv, self.cfg.trades_csv, self.cfg.wallets_csv)

    def _write_summary_file(self, summary: Any) -> None:
        if not self.cfg.summary_file:
            return
        body = render_summary(summary, self.state, color=False)
        every = (f"обновляется каждые {self.cfg.stats_interval_sec:g} c"
                 if self.cfg.stats_interval_sec > 0 else "обновляется только вручную")
        header = (f"Файл перезаписывается ботом ({every}). Запуск {self.cfg.run_id or '—'}. "
                  f"История сделок — в {Path(self.cfg.trades_csv).name}.")
        try:
            export_summary_txt(f"{header}\n{body}", self.cfg.summary_file)
        except OSError as exc:
            # Открытый в Excel/Блокноте файл на Windows блокирует замену.
            # Сводка — вспомогательный вывод, ронять из-за неё цикл нельзя.
            log.warning("Не удалось записать сводку в %s: %s", self.cfg.summary_file, exc)

    # ------------------------------------------------------------------ #
    # прочее
    # ------------------------------------------------------------------ #

    async def _resolve_symbol(self, mint: str) -> str:
        fallback = short_mint(mint)
        cached = self.state.symbols.get(mint)
        if cached:
            return cached
        try:
            symbol = await asyncio.wait_for(
                self.prices.symbol_for(mint, fallback),
                timeout=self.cfg.symbol_lookup_timeout_sec,
            )
        except Exception:  # noqa: BLE001 - символ не критичен, добьём в _refresh_marks
            return fallback
        if symbol and symbol != fallback:
            self.state.symbols[mint] = symbol
        return symbol or fallback

    def _label(self, mint: str) -> str:
        return self.state.symbols.get(mint) or short_mint(mint)

    def _wallet(self, wallet: str) -> str:
        return self.cfg.wallet_name(wallet) or short_mint(wallet)

    def _flush_state(self, *, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        self.state.processed_signatures = list(self._processed_order)
        self.state.symbols.update(self.prices.known_symbols())
        try:
            if force:
                self.store.save(self.state, max_signatures=self.cfg.max_processed_signatures)
            else:
                self.store.save_throttled(
                    self.state, self.cfg.state_flush_interval_sec,
                    max_signatures=self.cfg.max_processed_signatures,
                )
        except OSError as exc:
            log.error("Не удалось сохранить состояние: %s", exc)
            return
        self._dirty = False
