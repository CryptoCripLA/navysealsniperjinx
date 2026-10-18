"""Асинхронный клиент Solana JSON-RPC поверх aiohttp.

Намеренно без solana-py / solders: нужны три метода, а лишняя зависимость
тянет за собой компиляцию нативных колёс.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

log = logging.getLogger("copytrade.rpc")


def mask_url(url: str) -> str:
    """Спрятать API-ключ в RPC-URL.

    У Ankr/Helius/QuickNode ключ — последний сегмент пути, а не query-параметр,
    поэтому маскируем именно хвост, а заодно и query, если он есть.
    """
    if not url:
        return "(не задан)"
    parts = urlsplit(url)
    head, slash, tail = parts.path.rpartition("/")
    path = f"{head}/{tail[:4]}…{tail[-4:]}" if slash and len(tail) >= 8 else parts.path
    return urlunsplit((parts.scheme, parts.netloc, path, "…" if parts.query else "", ""))


class RpcError(RuntimeError):
    def __init__(self, code: int, message: str, method: str) -> None:
        super().__init__(f"RPC {method} -> {code}: {message}")
        self.code = code
        self.message = message
        self.method = method


class RpcClient:
    def __init__(
        self,
        url: str,
        *,
        timeout_sec: float = 25.0,
        max_retries: int = 4,
        max_concurrency: int = 6,
        min_interval_ms: int = 0,
        commitment: str = "confirmed",
    ) -> None:
        self.url = url
        self.safe_url = mask_url(url)
        self.commitment = commitment
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._max_retries = max_retries
        self._sem = asyncio.Semaphore(max_concurrency)
        self._min_interval = min_interval_ms / 1000.0
        self._last_call = 0.0
        self._pace_lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "RpcClient":
        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            headers={"Content-Type": "application/json"},
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------ #

    async def _pace(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._pace_lock:
            loop = asyncio.get_running_loop()
            wait = self._last_call + self._min_interval - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_running_loop().time()

    def _scrub(self, text: str) -> str:
        """Вырезать ключ из текста ошибки.

        Исключения aiohttp несут в себе RequestInfo с ПОЛНЫМ url, поэтому
        `log.debug("%r", exc)` укладывает API-ключ в файл лога — а файловый
        хендлер пишет на DEBUG всегда. Замена по точной строке, а не по
        регулярке: url нам известен, гадать не нужно.
        """
        return text.replace(self.url, self.safe_url) if self.url else text

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        if self._session is None:
            raise RuntimeError("RpcClient используется вне async with")

        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params or [],
        }

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            if attempt:
                # jitter, чтобы параллельные ретраи не били провайдера залпом
                delay = min(8.0, 0.5 * 2 ** (attempt - 1)) * (0.7 + random.random() * 0.6)
                await asyncio.sleep(delay)
            try:
                async with self._sem:
                    await self._pace()
                    async with self._session.post(self.url, json=payload) as resp:
                        if resp.status in (429, 500, 502, 503, 504):
                            body = (await resp.text())[:200]
                            last_exc = RuntimeError(f"HTTP {resp.status}: {body}")
                            log.debug("%s: HTTP %s, попытка %s", method, resp.status, attempt + 1)
                            continue
                        resp.raise_for_status()
                        data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                log.debug("%s: сетевая ошибка %s, попытка %s",
                          method, self._scrub(repr(exc)), attempt + 1)
                continue

            if isinstance(data, dict) and "error" in data:
                err = data["error"] or {}
                code = int(err.get("code", -1))
                message = str(err.get("message", ""))
                # -32005 = node behind / rate limited; -32004 = block not available
                if code in (-32004, -32005, -32014):
                    last_exc = RpcError(code, message, method)
                    continue
                raise RpcError(code, message, method)

            return data.get("result") if isinstance(data, dict) else data

        # `from None` намеренно: traceback исходного исключения тоже несёт url
        # с ключом, а наверху его логируют с exc_info=True. Причину не теряем —
        # она уходит в текст уже очищенной.
        detail = self._scrub(repr(last_exc)) if last_exc else "причина неизвестна"
        raise RuntimeError(
            f"RPC {method}: не удалось после {self._max_retries} попыток ({detail})"
        ) from None

    # ------------------------------------------------------------------ #
    # используемые методы
    # ------------------------------------------------------------------ #

    async def get_health(self) -> str:
        return await self.call("getHealth")

    async def get_slot(self) -> int:
        return int(await self.call("getSlot", [{"commitment": self.commitment}]))

    async def get_signatures_for_address(
        self,
        address: str,
        *,
        limit: int = 1000,
        until: str | None = None,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """Возвращает подписи от НОВЫХ к старым (как отдаёт RPC).

        `until` — эксклюзивная нижняя граница: последняя обработанная подпись.
        """
        opts: dict[str, Any] = {"limit": max(1, min(limit, 1000)), "commitment": self.commitment}
        if until:
            opts["until"] = until
        if before:
            opts["before"] = before
        result = await self.call("getSignaturesForAddress", [address, opts])
        return list(result or [])

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": self.commitment,
                },
            ],
        )

    async def get_transaction_with_retry(
        self,
        signature: str,
        *,
        attempts: int = 5,
        delay: float = 1.0,
    ) -> dict[str, Any] | None:
        """Свежая подпись из WS может ещё не быть доступна через getTransaction."""
        for i in range(attempts):
            tx = await self.get_transaction(signature)
            if tx is not None:
                return tx
            if i + 1 < attempts:
                await asyncio.sleep(delay * (i + 1))
        return None

    # ------------------------------------------------------------------ #
    # живая торговля
    # ------------------------------------------------------------------ #

    async def send_transaction(
        self,
        signed_base64: str,
        *,
        skip_preflight: bool = True,
        max_retries: int = 0,
    ) -> str:
        """Отправка подписанной транзакции. Возвращает подпись.

        Ретраи внутри `call` безопасны: отправляются ТЕ ЖЕ подписанные байты,
        значит и подпись та же — кластер отбросит дубликат. Опасен только
        повтор с пересборкой квоты (новая подпись = вторая покупка), поэтому
        такие ретраи живут в executor'е и считаются явно.
        """
        opts: dict[str, Any] = {
            "encoding": "base64",
            "skipPreflight": skip_preflight,
            "preflightCommitment": self.commitment,
            "maxRetries": max_retries,
        }
        return str(await self.call("sendTransaction", [signed_base64, opts]))

    async def get_signature_statuses(self, signatures: list[str]) -> list[dict[str, Any] | None]:
        result = await self.call(
            "getSignatureStatuses", [signatures, {"searchTransactionHistory": True}]
        )
        return list((result or {}).get("value") or [])

    async def get_latest_blockhash(self) -> tuple[str, int]:
        result = await self.call("getLatestBlockhash", [{"commitment": self.commitment}])
        value = (result or {}).get("value") or {}
        return str(value.get("blockhash", "")), int(value.get("lastValidBlockHeight", 0))

    async def get_block_height(self) -> int:
        return int(await self.call("getBlockHeight", [{"commitment": self.commitment}]))

    async def get_balance_lamports(self, address: str) -> int:
        result = await self.call("getBalance", [address, {"commitment": self.commitment}])
        return int((result or {}).get("value") or 0)

    async def get_token_balance_raw(self, owner: str, mint: str) -> tuple[int, int]:
        """Суммарный баланс токена по всем ATA владельца -> (raw, decimals)."""
        result = await self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint},
             {"encoding": "jsonParsed", "commitment": self.commitment}],
        )
        total = 0
        decimals = 0
        for account in (result or {}).get("value") or []:
            info = (((account.get("account") or {}).get("data") or {})
                    .get("parsed") or {}).get("info") or {}
            amount = info.get("tokenAmount") or {}
            total += int(amount.get("amount") or 0)
            decimals = int(amount.get("decimals") or decimals)
        return total, decimals
