"""Цены: SOL/USD с кэшем + цена произвольного токена для нереализованного PnL.

Источники SOL/USD (по порядку, первый успешный побеждает):
    1. Jupiter Price API V3  — https://api.jup.ag/price/v3?ids=<mint>
    2. CoinGecko simple/price
    3. Binance SOLUSDT ticker
    4. FALLBACK_SOL_PRICE_USD из конфига
Цена произвольного минта берётся только у Jupiter (остальные его не знают).

Внимание: keyless-доступ к api.jup.ag ограничен 0.5 rps / 30 rpm, поэтому
запросы проходят через общий rate limiter, а минты батчатся по 50 штук.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import aiohttp

from .constants import USDC_MINT, USDT_MINT, WSOL_MINT

log = logging.getLogger("copytrade.prices")

JUP_PRICE_PATH = "/price/v3"
JUP_TOKENS_PATH = "/tokens/v2/search"
JUP_BATCH_LIMIT = 50
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"


@dataclass(slots=True)
class TokenMeta:
    mint: str
    symbol: str
    name: str = ""
    decimals: int | None = None


class _RateLimiter:
    """Простой интервальный лимитер: не чаще одного запроса в 1/rps секунд."""

    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next_at = now + self._interval


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result > 0 else None


class PriceService:
    def __init__(
        self,
        *,
        jupiter_base_url: str = "https://api.jup.ag",
        jupiter_api_key: str = "",
        jupiter_max_rps: float = 0.4,
        sol_price_ttl_sec: float = 45.0,
        token_price_ttl_sec: float = 30.0,
        timeout_sec: float = 12.0,
        fallback_sol_price_usd: Decimal = Decimal(0),
    ) -> None:
        self._base = jupiter_base_url.rstrip("/")
        self._headers = {"Accept": "application/json"}
        if jupiter_api_key:
            self._headers["x-api-key"] = jupiter_api_key
        self._limiter = _RateLimiter(jupiter_max_rps)
        self._sol_ttl = sol_price_ttl_sec
        self._token_ttl = token_price_ttl_sec
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._fallback_sol = fallback_sol_price_usd

        self._session: aiohttp.ClientSession | None = None
        self._sol_price: Decimal | None = None
        self._sol_ts = 0.0
        self._sol_lock = asyncio.Lock()
        self._token_cache: dict[str, tuple[Decimal, float]] = {}
        self._meta_cache: dict[str, TokenMeta] = {}
        self._jup_cooldown_until = 0.0

    async def __aenter__(self) -> "PriceService":
        self._session = aiohttp.ClientSession(timeout=self._timeout, headers=self._headers)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def seed_symbols(self, symbols: dict[str, str]) -> None:
        """Подгружаем уже известные символы из state, чтобы не дёргать API заново."""
        for mint, symbol in symbols.items():
            self._meta_cache.setdefault(mint, TokenMeta(mint=mint, symbol=symbol))

    def known_symbols(self) -> dict[str, str]:
        return {m: meta.symbol for m, meta in self._meta_cache.items() if meta.symbol}

    # ------------------------------------------------------------------ #
    # низкий уровень
    # ------------------------------------------------------------------ #

    async def _get_json(self, url: str, params: dict[str, str] | None = None,
                        *, jupiter: bool = False) -> Any:
        if self._session is None:
            raise RuntimeError("PriceService используется вне async with")
        if jupiter:
            if time.time() < self._jup_cooldown_until:
                raise RuntimeError("Jupiter на cooldown после 429")
            await self._limiter.acquire()
        async with self._session.get(url, params=params) as resp:
            if resp.status == 429 and jupiter:
                self._jup_cooldown_until = time.time() + 30.0
                raise RuntimeError("Jupiter 429 — cooldown 30 c")
            if resp.status == 401 and jupiter:
                raise RuntimeError(
                    "Jupiter 401 — нужен JUPITER_API_KEY либо keyless-лимит исчерпан"
                )
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ------------------------------------------------------------------ #
    # SOL/USD
    # ------------------------------------------------------------------ #

    async def sol_usd(self, *, force: bool = False) -> Decimal | None:
        """Кэшированная цена SOL. При ошибке отдаёт последнее известное значение."""
        now = time.time()
        if not force and self._sol_price is not None and now - self._sol_ts < self._sol_ttl:
            return self._sol_price

        async with self._sol_lock:
            now = time.time()
            if not force and self._sol_price is not None and now - self._sol_ts < self._sol_ttl:
                return self._sol_price

            for name, fetcher in (
                ("jupiter", self._sol_from_jupiter),
                ("coingecko", self._sol_from_coingecko),
                ("binance", self._sol_from_binance),
            ):
                try:
                    price = await fetcher()
                except Exception as exc:  # noqa: BLE001 - любой источник может отвалиться
                    log.debug("SOL/USD из %s не получен: %s", name, exc)
                    continue
                if price:
                    self._sol_price = price
                    self._sol_ts = time.time()
                    log.debug("SOL/USD = %s (%s)", price, name)
                    return price

            if self._sol_price is not None:
                log.warning(
                    "Не удалось обновить SOL/USD, используем прошлое значение %s (возраст %.0f c)",
                    self._sol_price, time.time() - self._sol_ts,
                )
                return self._sol_price
            if self._fallback_sol > 0:
                log.warning("SOL/USD недоступен, берём FALLBACK_SOL_PRICE_USD=%s", self._fallback_sol)
                self._sol_price = self._fallback_sol
                self._sol_ts = time.time()
                return self._fallback_sol

            log.error("SOL/USD недоступен ни из одного источника")
            return None

    async def _sol_from_jupiter(self) -> Decimal | None:
        prices = await self._jupiter_prices([WSOL_MINT])
        return prices.get(WSOL_MINT)

    async def _sol_from_coingecko(self) -> Decimal | None:
        data = await self._get_json(COINGECKO_URL, {"ids": "solana", "vs_currencies": "usd"})
        return _to_decimal((data or {}).get("solana", {}).get("usd"))

    async def _sol_from_binance(self) -> Decimal | None:
        data = await self._get_json(BINANCE_URL, {"symbol": "SOLUSDT"})
        return _to_decimal((data or {}).get("price"))

    # ------------------------------------------------------------------ #
    # цена произвольного токена
    # ------------------------------------------------------------------ #

    async def _jupiter_prices(self, mints: list[str]) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for i in range(0, len(mints), JUP_BATCH_LIMIT):
            chunk = mints[i : i + JUP_BATCH_LIMIT]
            data = await self._get_json(
                f"{self._base}{JUP_PRICE_PATH}", {"ids": ",".join(chunk)}, jupiter=True
            )
            if not isinstance(data, dict):
                continue
            for mint, entry in data.items():
                # Токены без надёжной цены Jupiter просто не возвращает
                price = _to_decimal((entry or {}).get("usdPrice"))
                if price:
                    out[mint] = price
        return out

    async def token_usd_many(self, mints: Iterable[str]) -> dict[str, Decimal]:
        """USD-цены пачкой. Стейблы отдаём как 1.0, не тратя лимит."""
        now = time.time()
        result: dict[str, Decimal] = {}
        to_fetch: list[str] = []

        for mint in dict.fromkeys(mints):  # dedupe, порядок сохраняем
            if mint in (USDC_MINT, USDT_MINT):
                result[mint] = Decimal(1)
                continue
            cached = self._token_cache.get(mint)
            if cached and now - cached[1] < self._token_ttl:
                result[mint] = cached[0]
            else:
                to_fetch.append(mint)

        if to_fetch:
            try:
                fresh = await self._jupiter_prices(to_fetch)
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось получить цены токенов: %s", exc)
                fresh = {}
            stamp = time.time()
            for mint, price in fresh.items():
                self._token_cache[mint] = (price, stamp)
                result[mint] = price
            # Для минтов без свежей цены отдаём протухшую, если она есть:
            # для нереализованного PnL это честнее, чем None.
            for mint in to_fetch:
                if mint not in result and mint in self._token_cache:
                    result[mint] = self._token_cache[mint][0]

        return result

    async def token_usd(self, mint: str) -> Decimal | None:
        return (await self.token_usd_many([mint])).get(mint)

    async def token_price_sol(self, mint: str) -> Decimal | None:
        """Цена токена в SOL — нужна стратегии, когда её нельзя вывести из tx."""
        prices = await self.token_prices_sol([mint])
        return prices.get(mint)

    async def token_prices_sol(self, mints: Iterable[str]) -> dict[str, Decimal]:
        mint_list = list(dict.fromkeys(mints))
        if not mint_list:
            return {}
        sol_usd, usd_prices = await asyncio.gather(self.sol_usd(), self.token_usd_many(mint_list))
        if not sol_usd or sol_usd <= 0:
            return {}
        return {mint: price / sol_usd for mint, price in usd_prices.items() if price > 0}

    # ------------------------------------------------------------------ #
    # метаданные (символ)
    # ------------------------------------------------------------------ #

    async def token_meta_many(self, mints: Iterable[str]) -> dict[str, TokenMeta]:
        wanted = [m for m in dict.fromkeys(mints) if m not in self._meta_cache]
        for i in range(0, len(wanted), 100):
            chunk = wanted[i : i + 100]
            try:
                data = await self._get_json(
                    f"{self._base}{JUP_TOKENS_PATH}", {"query": ",".join(chunk)}, jupiter=True
                )
            except Exception as exc:  # noqa: BLE001 - символ не критичен для торговли
                log.debug("Метаданные токенов недоступны: %s", exc)
                break
            for entry in data or []:
                if not isinstance(entry, dict) or not entry.get("id"):
                    continue
                self._meta_cache[entry["id"]] = TokenMeta(
                    mint=entry["id"],
                    symbol=str(entry.get("symbol") or "")[:16],
                    name=str(entry.get("name") or "")[:64],
                    decimals=entry.get("decimals"),
                )
        return {m: self._meta_cache[m] for m in dict.fromkeys(mints) if m in self._meta_cache}

    async def symbol_for(self, mint: str, default: str = "") -> str:
        meta = self._meta_cache.get(mint)
        if meta is None:
            meta = (await self.token_meta_many([mint])).get(mint)
        return meta.symbol if meta and meta.symbol else default
