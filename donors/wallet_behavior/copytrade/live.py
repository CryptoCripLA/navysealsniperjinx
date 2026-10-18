"""Живое исполнение через Jupiter Swap API v1.

Цепочка: quote -> swap (сервер отдаёт готовую VersionedTransaction) ->
подпись локально (solders) -> sendTransaction -> подтверждение -> ПЕРЕЧИТЫВАНИЕ
своей же транзакции, чтобы записать ФАКТИЧЕСКУЮ цену исполнения, а не ту,
что обещала квота. На мемкоинах разница между обещанным и полученным — норма.

Про комиссии (проверено по докам Jupiter):
  * `prioritizationFeeLamports` умеет ЛИБО priority fee, ЛИБО Jito-чаевые,
    но не оба сразу — для комбинации нужен /swap-instructions;
  * `computeUnitPriceMicroLamports` — отдельное поле, поэтому режим `jito`
    задаёт priority через него, а чаевые через `jitoTipLamports`;
  * чаевые Jito имеют смысл ТОЛЬКО при отправке в block-engine Jito. Через
    обычный RPC они просто сгорают — поэтому JITO_ENDPOINT обязателен.

Ключ читается только из окружения или файла вне репозитория и никогда
не попадает в логи.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp

from .config import Config
from .constants import DEC_LAMPORTS_PER_SOL, LAMPORTS_PER_SOL, WSOL_MINT
from .executor import BuyOrder, ExecutionResult, Executor, SellOrder
from .parser import parse_wallet_activity
from .rpc import RpcClient

log = logging.getLogger("copytrade.live")

ZERO = Decimal(0)
QUOTE_PATH = "/swap/v1/quote"
SWAP_PATH = "/swap/v1/swap"


class LiveTradeError(RuntimeError):
    """Ошибка на любом шаге живого свапа. Позиция при этом НЕ открывается."""


# --------------------------------------------------------------------------- #
# ключ
# --------------------------------------------------------------------------- #

def load_keypair(cfg: Config) -> Any:
    """Keypair из WALLET_PRIVATE_KEY или WALLET_PRIVATE_KEY_FILE.

    Поддержаны оба ходовых формата: base58-строка (Phantom «export private
    key») и JSON-массив из 64 байт (`solana-keygen`, id.json).
    """
    try:
        from solders.keypair import Keypair
    except ImportError as exc:  # pragma: no cover
        raise LiveTradeError(
            "Для живой торговли нужен пакет solders: pip install -r requirements.txt"
        ) from exc

    raw = cfg.wallet_private_key.strip()
    if not raw and cfg.wallet_private_key_file:
        path = Path(cfg.wallet_private_key_file).expanduser()
        if not path.is_file():
            raise LiveTradeError(f"Файл ключа не найден: {path}")
        raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise LiveTradeError(
            "Приватный ключ не задан. WALLET_PRIVATE_KEY в .env или "
            "WALLET_PRIVATE_KEY_FILE с путём вне репозитория."
        )

    try:
        if raw.startswith("["):
            import json
            data = bytes(json.loads(raw))
            keypair = Keypair.from_bytes(data)
        else:
            keypair = Keypair.from_base58_string(raw)
    except Exception:  # noqa: BLE001
        # `from None` намеренно: текст исходной ошибки может содержать сам ключ.
        raise LiveTradeError(
            "Не удалось разобрать приватный ключ: ожидается base58-строка "
            "или JSON-массив из 64 байт"
        ) from None

    expected = cfg.wallet_public_key.strip()
    actual = str(keypair.pubkey())
    if expected and expected != actual:
        raise LiveTradeError(
            f"Ключ даёт адрес {actual}, а в конфиге WALLET_PUBLIC_KEY={expected}. "
            f"Проверь, тот ли ключ подставлен."
        )
    return keypair


# --------------------------------------------------------------------------- #
# Jupiter
# --------------------------------------------------------------------------- #

class JupiterSwapClient:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self._session = session
        self._base = cfg.jupiter_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.cfg.jupiter_api_key:
            headers["x-api-key"] = self.cfg.jupiter_api_key
        return headers

    async def quote(
        self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int
    ) -> dict[str, Any]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(slippage_bps),
            "swapMode": "ExactIn",
            "restrictIntermediateTokens": "true" if self.cfg.jupiter_restrict_intermediate else "false",
            "maxAccounts": str(self.cfg.jupiter_max_accounts),
        }
        async with self._session.get(
            f"{self._base}{QUOTE_PATH}", params=params, headers=self._headers()
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise LiveTradeError(f"quote HTTP {resp.status}: {body[:300]}")
            data = _json(body, "quote")
        if not data.get("outAmount"):
            raise LiveTradeError(f"quote без маршрута: {body[:300]}")
        return data

    async def swap(self, quote: dict[str, Any], user_pubkey: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
        }
        payload.update(self._fee_payload())

        async with self._session.post(
            f"{self._base}{SWAP_PATH}", json=payload, headers=self._headers()
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise LiveTradeError(f"swap HTTP {resp.status}: {body[:300]}")
            data = _json(body, "swap")
        if not data.get("swapTransaction"):
            raise LiveTradeError(f"swap без транзакции: {body[:300]}")
        if data.get("simulationError"):
            raise LiveTradeError(f"симуляция свапа провалилась: {data['simulationError']}")
        return data

    def _fee_payload(self) -> dict[str, Any]:
        """Комиссии в терминах Jupiter — см. заметку в шапке модуля."""
        if self.cfg.fee_mode == "auto":
            return {"prioritizationFeeLamports": "auto"}
        if self.cfg.fee_mode == "jito":
            # priority отдельным полем, чаевые — через prioritizationFeeLamports.
            micro_lamports = (
                self.cfg.priority_fee_lamports * 1_000_000 // max(1, self.cfg.assumed_compute_units)
            )
            return {
                "computeUnitPriceMicroLamports": int(micro_lamports),
                "prioritizationFeeLamports": {"jitoTipLamports": self.cfg.bribe_lamports},
            }
        return {
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": self.cfg.priority_fee_lamports,
                    "priorityLevel": self.cfg.priority_level,
                    "global": False,
                }
            }
        }


def _json(body: str, what: str) -> dict[str, Any]:
    import json
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LiveTradeError(f"{what}: ответ не JSON ({body[:200]})") from exc
    if not isinstance(data, dict):
        raise LiveTradeError(f"{what}: неожиданный формат ответа")
    return data


# --------------------------------------------------------------------------- #
# executor
# --------------------------------------------------------------------------- #

class LiveExecutor(Executor):
    """Реальные свапы. Каждый вызов тратит настоящие деньги."""

    name = "jupiter-live"
    live = True

    def __init__(self, cfg: Config, rpc: RpcClient, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.rpc = rpc
        self.keypair = load_keypair(cfg)
        self.pubkey = str(self.keypair.pubkey())
        self.session = session
        self.jupiter = JupiterSwapClient(cfg, session)
        # По одному свапу на минт: две параллельные покупки одного токена
        # (WS + догон) открыли бы двойную позицию за реальные деньги.
        self._locks: dict[str, asyncio.Lock] = {}
        self._done: set[str] = set()

    def _lock(self, mint: str) -> asyncio.Lock:
        lock = self._locks.get(mint)
        if lock is None:
            lock = self._locks.setdefault(mint, asyncio.Lock())
        return lock

    # ------------------------------------------------------------------ #

    async def buy(self, order: BuyOrder) -> ExecutionResult:
        guard = f"BUY:{order.mint}:{order.target_signature}"
        async with self._lock(order.mint):
            if guard in self._done:
                return ExecutionResult(ok=False, error="эта покупка уже исполнялась")
            try:
                result = await self._buy(order)
            except LiveTradeError as exc:
                return ExecutionResult(ok=False, simulated=False, error=str(exc))
            except Exception as exc:  # noqa: BLE001 - деньги: любая ошибка = не купили
                log.debug("Живая покупка упала:", exc_info=True)
                return ExecutionResult(ok=False, simulated=False, error=repr(exc))
            self._done.add(guard)
            return result

    async def _buy(self, order: BuyOrder) -> ExecutionResult:
        lamports = int(self.cfg.position_size_sol * DEC_LAMPORTS_PER_SOL)
        if lamports <= 0:
            raise LiveTradeError("POSITION_SIZE_SOL = 0")

        balance = await self.rpc.get_balance_lamports(self.pubkey)
        reserve = int(self.cfg.min_wallet_balance_sol * DEC_LAMPORTS_PER_SOL)
        need = lamports + self.cfg.priority_fee_lamports + self.cfg.bribe_lamports + reserve
        if balance < need:
            raise LiveTradeError(
                f"на кошельке {balance / LAMPORTS_PER_SOL:.4f} SOL, нужно "
                f"{need / LAMPORTS_PER_SOL:.4f} (позиция + комиссии + резерв "
                f"MIN_WALLET_BALANCE_SOL)"
            )

        quote = await self.jupiter.quote(WSOL_MINT, order.mint, lamports, self.cfg.slippage_bps)
        signature = await self._submit(quote)
        activity = await self._read_back(signature)

        tokens = _token_gain(activity, order.mint)
        spent = _sol_spent(activity)
        if tokens <= 0:
            raise LiveTradeError(
                f"транзакция {signature} прошла, но токенов не прибавилось — "
                f"проверь вручную: https://solscan.io/tx/{signature}"
            )
        price = spent / tokens if tokens > 0 else ZERO
        return ExecutionResult(
            ok=True, tokens=tokens, sol_amount=spent, price_sol=price,
            simulated=False, tx_signature=signature,
            meta={"quote_out": str(quote.get("outAmount")), "slippage_bps": str(self.cfg.slippage_bps)},
        )

    # ------------------------------------------------------------------ #

    async def sell(self, order: SellOrder) -> ExecutionResult:
        async with self._lock(order.mint):
            try:
                result = await self._sell(order)
            except LiveTradeError as exc:
                return ExecutionResult(ok=False, simulated=False, error=str(exc))
            except Exception as exc:  # noqa: BLE001
                log.debug("Живая продажа упала:", exc_info=True)
                return ExecutionResult(ok=False, simulated=False, error=repr(exc))
            return result

    async def _sell(self, order: SellOrder) -> ExecutionResult:
        on_chain_raw, on_chain_dec = await self.rpc.get_token_balance_raw(self.pubkey, order.mint)
        if on_chain_raw <= 0:
            raise LiveTradeError("на кошельке нет этого токена (уже продан вручную?)")

        decimals = on_chain_dec or order.decimals
        want_raw = int(order.tokens * (Decimal(10) ** decimals))
        # Учёт мог разойтись с цепочкой (ручная сделка, пыль округления) —
        # продаём не больше, чем реально есть, иначе свап просто не соберётся.
        amount_raw = min(want_raw, on_chain_raw)
        if order.fraction >= 1:
            amount_raw = on_chain_raw  # выход целиком — не оставляем хвост
        if amount_raw <= 0:
            raise LiveTradeError("нечего продавать после сверки с балансом")

        quote = await self.jupiter.quote(order.mint, WSOL_MINT, amount_raw, self.cfg.slippage_bps)
        signature = await self._submit(quote)
        activity = await self._read_back(signature)

        sold = -_token_gain(activity, order.mint)
        received = -_sol_spent(activity)
        if sold <= 0:
            raise LiveTradeError(
                f"транзакция {signature} прошла, но баланс токена не уменьшился — "
                f"проверь вручную: https://solscan.io/tx/{signature}"
            )
        price = received / sold if sold > 0 else ZERO
        return ExecutionResult(
            ok=True, tokens=sold, sol_amount=max(ZERO, received), price_sol=price,
            simulated=False, tx_signature=signature,
            meta={"quote_out": str(quote.get("outAmount")), "sold_raw": str(amount_raw)},
        )

    # ------------------------------------------------------------------ #
    # подпись и отправка
    # ------------------------------------------------------------------ #

    async def _submit(self, quote: dict[str, Any]) -> str:
        from solders.transaction import VersionedTransaction

        swap = await self.jupiter.swap(quote, self.pubkey)
        raw = base64.b64decode(swap["swapTransaction"])
        unsigned = VersionedTransaction.from_bytes(raw)
        # Пересборка с нашим ключом сохраняет message как есть (в т.ч. blockhash):
        # подписываем ровно то, что собрал Jupiter, ничего не подменяя.
        signed = VersionedTransaction(unsigned.message, [self.keypair])
        payload = base64.b64encode(bytes(signed)).decode("ascii")
        signature = str(signed.signatures[0])
        last_valid = int(swap.get("lastValidBlockHeight") or 0)

        log.info("Отправляю свап %s (fee_mode=%s, slippage=%s bps)",
                 signature, self.cfg.fee_mode, self.cfg.slippage_bps)
        await self._send(payload)

        error = await self._confirm(signature, payload, last_valid)
        if error:
            raise LiveTradeError(f"{error} (tx {signature})")
        return signature

    async def _send(self, payload: str) -> None:
        """Куда отправлять: обычный RPC или block-engine Jito.

        Чаевые Jito считаются только тем, кто их собирает. Если tx с
        `jitoTipLamports` уйдёт в обычный RPC, чаевые всё равно спишутся —
        просто без всякой пользы. Поэтому режим jito шлёт строго в свой
        endpoint (`/api/v1/transactions`, JSON-RPC sendTransaction,
        encoding=base64 — по докам Jito).
        """
        if self.cfg.fee_mode != "jito" or not self.cfg.jito_endpoint:
            await self.rpc.send_transaction(
                payload, skip_preflight=self.cfg.live_skip_preflight
            )
            return

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [payload, {"encoding": "base64"}],
        }
        async with self.session.post(
            self.cfg.jito_endpoint, json=body,
            headers={"Content-Type": "application/json"},
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise LiveTradeError(f"Jito HTTP {resp.status}: {text[:300]}")
            data = _json(text, "jito sendTransaction")
        if data.get("error"):
            raise LiveTradeError(f"Jito отказал: {data['error']}")

    async def _confirm(self, signature: str, payload: str, last_valid_height: int) -> str | None:
        """Ждём подтверждения, попутно переотправляя ТЕ ЖЕ байты.

        Переотправка безопасна: подпись не меняется, кластер отбросит дубль.
        Пересборка квоты — нет, поэтому её здесь нет и быть не должно.
        """
        deadline = time.monotonic() + self.cfg.live_confirm_timeout_sec
        resends = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            try:
                statuses = await self.rpc.get_signature_statuses([signature])
            except Exception as exc:  # noqa: BLE001 - сеть моргнула, ждём дальше
                log.debug("getSignatureStatuses: %s", exc)
                continue

            status = statuses[0] if statuses else None
            if status:
                if status.get("err") is not None:
                    return f"транзакция отклонена сетью: {status['err']}"
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return None

            if last_valid_height:
                try:
                    height = await self.rpc.get_block_height()
                except Exception:  # noqa: BLE001
                    height = 0
                if height and height > last_valid_height:
                    return "blockhash истёк, транзакция не попала в блок"

            if resends < self.cfg.live_send_max_retries:
                resends += 1
                try:
                    await self.rpc.send_transaction(
                        payload, skip_preflight=self.cfg.live_skip_preflight
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug("Переотправка %s: %s", signature[:16], exc)
        return "не дождались подтверждения"

    async def _read_back(self, signature: str) -> Any:
        """Фактическое исполнение берём из своей же транзакции, не из квоты."""
        tx = await self.rpc.get_transaction_with_retry(
            signature, attempts=max(5, self.cfg.tx_fetch_retries), delay=1.5
        )
        if tx is None:
            raise LiveTradeError(
                f"транзакция {signature} подтверждена, но getTransaction её не отдал — "
                f"учёт разошёлся с цепочкой, сверь вручную"
            )
        activity = parse_wallet_activity(tx, self.pubkey)
        if activity is None or not activity.succeeded:
            raise LiveTradeError(f"своя транзакция {signature} не разобралась или неуспешна")
        return activity


def _token_gain(activity: Any, mint: str) -> Decimal:
    delta = activity.token_deltas.get(mint) or activity.stable_deltas.get(mint)
    return delta.delta_ui if delta else ZERO


def _sol_spent(activity: Any) -> Decimal:
    """Сколько SOL реально ушло: дельта с обратным знаком ПЛЮС комиссия сети."""
    fee = Decimal(activity.fee_lamports) / DEC_LAMPORTS_PER_SOL
    return -activity.sol_delta + fee
