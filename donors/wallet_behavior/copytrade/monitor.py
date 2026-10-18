"""Мониторинг отслеживаемых кошельков: WebSocket logsSubscribe + polling.

Поллер работает ВСЕГДА, а не только когда WS упал: `logsSubscribe` не даёт
гарантии доставки, и единственный способ не потерять транзакцию — периодически
сверяться с `getSignaturesForAddress` от последней обработанной подписи.
Пока WS жив, интервал поллера умножается на POLL_IDLE_MULTIPLIER.

Кошельков может быть несколько. Каждому нужен СВОЙ курсор: `until=` работает
в рамках одного адреса. А вот WS-соединение общее — `logsSubscribe` принимает
ровно один адрес в `mentions`, поэтому подписок делаем N, и по номеру подписки
из уведомления понимаем, чья это транзакция.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Callable

import websockets

from .config import Config
from .constants import short_mint
from .rpc import RpcClient, mask_url

log = logging.getLogger("copytrade.monitor")

MAX_CATCHUP_PAGES = 10


@dataclass(slots=True, frozen=True)
class SignatureRef:
    signature: str
    slot: int
    source: str                      # ws | poll | backfill | reconnect
    wallet: str = ""
    block_time: int | None = None


class WalletMonitor:
    def __init__(
        self,
        cfg: Config,
        rpc: RpcClient,
        queue: asyncio.Queue[SignatureRef],
        *,
        cursor: Callable[[str], str | None],
        on_cursor_init: Callable[[str, str, int], None],
        on_ws_state: Callable[[bool, str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.rpc = rpc
        self.queue = queue
        self._cursor = cursor
        self._on_cursor_init = on_cursor_init
        self._on_ws_state = on_ws_state or (lambda _up, _reason: None)

        self._wallets = list(cfg.wallet_addresses)
        self._stop = asyncio.Event()
        self._ws_healthy = False
        self._ws_supported = cfg.ws_enabled and bool(cfg.rpc_ws_url)
        self._catchup_lock = asyncio.Lock()
        self._seen_in_flight: set[str] = set()

    @property
    def ws_healthy(self) -> bool:
        return self._ws_healthy

    def _scrub(self, text: str) -> str:
        """Ключ живёт и в ws-url, а исключения библиотеки цитируют его целиком."""
        url = self.cfg.rpc_ws_url
        return text.replace(url, mask_url(url)) if url else text

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._poll_loop(), name="poller")]
        if self._ws_supported:
            tasks.append(asyncio.create_task(self._ws_loop(), name="ws"))
        else:
            log.warning("WebSocket отключён — работаем только на polling")
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ #
    # очередь
    # ------------------------------------------------------------------ #

    async def _emit(self, ref: SignatureRef) -> None:
        # Дешёвая защита от дублей до попадания в engine: одна и та же подпись
        # почти всегда приходит и из WS, и из поллера. Ключ включает кошелёк:
        # одна tx может задеть сразу двух отслеживаемых, и разбирать её нужно
        # для каждого — балансы у них разные.
        key = f"{ref.wallet}:{ref.signature}"
        if key in self._seen_in_flight:
            return
        self._seen_in_flight.add(key)
        if len(self._seen_in_flight) > 20_000:
            self._seen_in_flight.clear()
            self._seen_in_flight.add(key)
        await self.queue.put(ref)

    # ------------------------------------------------------------------ #
    # polling / догон
    # ------------------------------------------------------------------ #

    async def _poll_loop(self) -> None:
        # Первый проход сразу: поднимаем всё, что накопилось, пока бот стоял.
        await self._catch_up_all("backfill")
        while not self._stop.is_set():
            interval = self.cfg.poll_interval_sec
            if self._ws_healthy:
                interval *= self.cfg.poll_idle_multiplier
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._catch_up_all("poll")
            except Exception as exc:  # noqa: BLE001 - поллер не должен умирать
                log.warning("Ошибка догона через getSignaturesForAddress: %s", exc)

    async def _catch_up_all(self, source: str) -> None:
        async with self._catchup_lock:
            for wallet in self._wallets:
                if self._stop.is_set():
                    return
                try:
                    await self._catch_up_wallet(wallet, source)
                except Exception as exc:  # noqa: BLE001 - один кошелёк не валит остальные
                    log.warning("Догон по %s не удался: %s", short_mint(wallet), exc)

    async def _catch_up_wallet(self, wallet: str, source: str) -> None:
        last_signature = self._cursor(wallet)
        if last_signature is None:
            await self._cold_start(wallet)
            return

        rows = await self._fetch_since(wallet, last_signature)
        if not rows:
            return

        rows.reverse()  # RPC отдаёт от новых к старым, нам нужен хронологический порядок
        skipped = 0
        for row in rows:
            signature = row.get("signature")
            if not signature:
                continue
            if row.get("err") is not None:
                skipped += 1
                continue
            await self._emit(SignatureRef(
                signature=signature,
                slot=int(row.get("slot") or 0),
                source=source,
                wallet=wallet,
                block_time=row.get("blockTime"),
            ))
        log.debug(
            "Догон %s (%s): %s новых подписей, %s неуспешных пропущено",
            short_mint(wallet), source, len(rows) - skipped, skipped,
        )

    async def _cold_start(self, wallet: str) -> None:
        """Первый запуск без сохранённого состояния по этому кошельку."""
        limit = max(1, self.cfg.startup_backfill_limit)
        rows = await self.rpc.get_signatures_for_address(wallet, limit=limit)
        if not rows:
            log.warning("У кошелька %s нет транзакций", wallet)
            return

        if self.cfg.startup_backfill_limit <= 0:
            newest = rows[0]
            self._on_cursor_init(wallet, newest["signature"], int(newest.get("slot") or 0))
            log.info(
                "%s: старт с чистого листа, курсор на %s… (слот %s). "
                "История не копируется (STARTUP_BACKFILL_LIMIT=0).",
                self.cfg.wallet_name(wallet), newest["signature"][:16], newest.get("slot"),
            )
            return

        rows.reverse()
        log.info(
            "%s: разбираем %s последних транзакций (STARTUP_BACKFILL_LIMIT)",
            self.cfg.wallet_name(wallet), len(rows),
        )
        for row in rows:
            if row.get("err") is not None:
                continue
            await self._emit(SignatureRef(
                signature=row["signature"],
                slot=int(row.get("slot") or 0),
                source="backfill",
                wallet=wallet,
                block_time=row.get("blockTime"),
            ))

    async def _fetch_since(self, wallet: str, until_signature: str) -> list[dict[str, Any]]:
        """Все подписи новее `until_signature`, с пагинацией на случай большой дыры."""
        collected: list[dict[str, Any]] = []
        before: str | None = None
        limit = self.cfg.signature_fetch_limit

        for page in range(MAX_CATCHUP_PAGES):
            rows = await self.rpc.get_signatures_for_address(
                wallet, limit=limit, until=until_signature, before=before,
            )
            if not rows:
                break
            collected.extend(rows)
            if len(rows) < limit:
                break
            before = rows[-1]["signature"]
            if page == MAX_CATCHUP_PAGES - 1:
                log.warning(
                    "%s: разрыв больше %s транзакций — часть истории могла быть "
                    "пропущена. Уменьши простой бота или подними SIGNATURE_FETCH_LIMIT.",
                    self.cfg.wallet_name(wallet), MAX_CATCHUP_PAGES * limit,
                )
        return collected

    # ------------------------------------------------------------------ #
    # WebSocket
    # ------------------------------------------------------------------ #

    async def _ws_loop(self) -> None:
        backoff = self.cfg.ws_reconnect_min_sec
        while not self._stop.is_set() and self._ws_supported:
            try:
                await self._ws_session()
                backoff = self.cfg.ws_reconnect_min_sec
            except asyncio.CancelledError:
                raise
            except _UnsupportedSubscription as exc:
                self._set_ws_state(False, str(exc))
                self._ws_supported = False
                log.error(
                    "RPC не поддерживает logsSubscribe (%s). Переходим на polling с "
                    "интервалом %.1f c.", exc, self.cfg.poll_interval_sec,
                )
                return
            except Exception as exc:  # noqa: BLE001 - реконнект на любую ошибку
                detail = self._scrub(repr(exc))
                self._set_ws_state(False, detail)
                if self._stop.is_set():
                    return
                jittered = backoff * (0.7 + random.random() * 0.6)
                log.warning("WS оборвался (%s). Переподключение через %.1f c", detail, jittered)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=jittered)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(self.cfg.ws_reconnect_max_sec, max(backoff, 0.5) * 2)

    async def _ws_session(self) -> None:
        async with websockets.connect(
            self.cfg.rpc_ws_url,
            ping_interval=self.cfg.ws_ping_interval_sec,
            ping_timeout=self.cfg.ws_ping_interval_sec * 2,
            close_timeout=5,
            max_size=16 * 1024 * 1024,
            open_timeout=20,
        ) as ws:
            subscriptions = await self._subscribe_all(ws)
            self._set_ws_state(True, f"подписок {len(subscriptions)}")

            # После (пере)подключения обязательно догоняем: пока WS лежал,
            # транзакции никуда не делись.
            await self._catch_up_all("reconnect")

            async for raw in ws:
                if self._stop.is_set():
                    return
                await self._handle_ws_message(raw, subscriptions)

    async def _subscribe_all(self, ws: Any) -> dict[int, str]:
        """Подписка на каждый кошелёк. Возвращает номер подписки -> адрес."""
        pending: dict[int, str] = {}
        for index, wallet in enumerate(self._wallets, start=1):
            pending[index] = wallet
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": index,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [wallet]},
                    {"commitment": self.cfg.commitment},
                ],
            }))

        subscriptions: dict[int, str] = {}
        deadline = asyncio.get_running_loop().time() + 25
        while pending:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise _UnsupportedSubscription(
                    f"нет ответа на logsSubscribe за 25 c (ждали {len(pending)} подтверждений)"
                )
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            request_id = message.get("id")
            if request_id not in pending:
                # Уведомления могут опередить подтверждения — не теряем их.
                await self._handle_ws_message(raw, subscriptions)
                continue
            if "error" in message:
                raise _UnsupportedSubscription(str(message["error"]))
            subscriptions[int(message.get("result", 0))] = pending.pop(request_id)

        log.debug("WS: активных подписок %s", len(subscriptions))
        return subscriptions

    async def _handle_ws_message(self, raw: str | bytes, subscriptions: dict[int, str]) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if message.get("method") != "logsNotification":
            return
        params = message.get("params") or {}
        result = params.get("result") or {}
        value = result.get("value") or {}
        signature = value.get("signature")
        if not signature:
            return
        if value.get("err") is not None:
            log.debug("WS: tx %s… завершилась ошибкой — пропуск", signature[:16])
            return

        wallet = subscriptions.get(params.get("subscription"), "")
        if not wallet:
            # Подписку не опознали (например, уведомление пришло раньше ответа).
            # Отдать её без адреса нельзя: непонятно, чьи балансы считать —
            # поллер подберёт эту транзакцию по курсору.
            log.debug("WS: уведомление по неизвестной подписке %s — ждём поллер",
                      params.get("subscription"))
            return
        slot = int((result.get("context") or {}).get("slot") or 0)
        await self._emit(SignatureRef(
            signature=signature, slot=slot, source="ws", wallet=wallet,
        ))

    def _set_ws_state(self, up: bool, reason: str) -> None:
        if up != self._ws_healthy:
            self._on_ws_state(up, reason)
        self._ws_healthy = up


class _UnsupportedSubscription(RuntimeError):
    """Провайдер не умеет logsSubscribe — реконнект бессмыслен."""
