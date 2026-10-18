"""Разбор транзакции цели по ИЗМЕНЕНИЮ БАЛАНСОВ, а не по конкретной программе.

Почему так: Jupiter, Raydium, pump.fun, PumpSwap, Meteora, Orca и любой новый
агрегатор оставляют разные инструкции, но одинаковый след в
`meta.preTokenBalances` / `meta.postTokenBalances` и `meta.pre/postBalances`.
Считаем дельты кошелька — и не зависим от того, через что он торговал.

Модуль чистый: никаких сетевых вызовов, только `dict` -> модели. Это делает
парсер тестируемым на фикстурах (см. tests/test_parser.py).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Iterator

from .constants import (
    DEC_LAMPORTS_PER_SOL,
    NATIVE_SOL,
    STABLE_MINTS,
    TOKEN_PROGRAM_NAMES,
    WSOL_MINT,
)
from .models import EventKind, TokenBalanceDelta, TradeEvent, WalletActivity

log = logging.getLogger("copytrade.parser")

# Программы-«сантехника»: их наличие не означает торговлю.
PLUMBING_PROGRAMS = frozenset({
    "11111111111111111111111111111111",              # System
    "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token Account
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",   # Memo v2
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",   # Memo v1
})

_TRANSFER_TYPES = {"transfer", "transferChecked"}


# --------------------------------------------------------------------------- #
# вспомогательные извлекатели
# --------------------------------------------------------------------------- #

def _account_keys(tx: dict[str, Any]) -> list[str]:
    """Полный список аккаунтов в порядке индексации pre/postBalances.

    jsonParsed уже включает адреса из address lookup tables (source=lookupTable),
    но не все провайдеры это делают — добиваем из `meta.loadedAddresses`, если
    длина не сходится с preBalances.
    """
    message = (tx.get("transaction") or {}).get("message") or {}
    raw = message.get("accountKeys") or []
    keys = [k.get("pubkey", "") if isinstance(k, dict) else str(k) for k in raw]

    meta = tx.get("meta") or {}
    loaded = meta.get("loadedAddresses") or {}
    extra = list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    expected = len(meta.get("preBalances") or [])
    if extra and expected and len(keys) < expected and len(keys) + len(extra) == expected:
        keys += extra
    return keys


def _native_delta_lamports(tx: dict[str, Any], keys: list[str], wallet: str) -> int:
    """Изменение нативного SOL у кошелька. Комиссия исключена, если он её платил."""
    meta = tx.get("meta") or {}
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    fee = int(meta.get("fee") or 0)

    total = 0
    for idx, key in enumerate(keys):
        if key != wallet or idx >= len(pre) or idx >= len(post):
            continue
        delta = int(post[idx]) - int(pre[idx])
        if idx == 0:  # accountKeys[0] — всегда плательщик комиссии
            delta += fee
        total += delta
    return total


def _token_totals(entries: list[dict[str, Any]] | None, wallet: str) -> dict[str, tuple[int, int]]:
    """mint -> (суммарный raw-баланс по всем token-аккаунтам кошелька, decimals)."""
    totals: dict[str, tuple[int, int]] = {}
    for entry in entries or []:
        if entry.get("owner") != wallet:
            continue
        mint = entry.get("mint")
        if not mint:
            continue
        amount = entry.get("uiTokenAmount") or {}
        try:
            raw = int(amount.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        decimals = int(amount.get("decimals") or 0)
        prev_raw, prev_dec = totals.get(mint, (0, decimals))
        totals[mint] = (prev_raw + raw, prev_dec or decimals)
    return totals


def _iter_instructions(tx: dict[str, Any]) -> Iterator[dict[str, Any]]:
    message = (tx.get("transaction") or {}).get("message") or {}
    for ix in message.get("instructions") or []:
        if isinstance(ix, dict):
            yield ix
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            if isinstance(ix, dict):
                yield ix


def _token_account_maps(tx: dict[str, Any], keys: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """token-аккаунт -> mint и token-аккаунт -> owner (из pre/postTokenBalances)."""
    meta = tx.get("meta") or {}
    account_mint: dict[str, str] = {}
    account_owner: dict[str, str] = {}
    for entries in (meta.get("preTokenBalances"), meta.get("postTokenBalances")):
        for entry in entries or []:
            idx = entry.get("accountIndex")
            if idx is None or idx >= len(keys):
                continue
            pubkey = keys[idx]
            if entry.get("mint"):
                account_mint[pubkey] = entry["mint"]
            if entry.get("owner"):
                account_owner[pubkey] = entry["owner"]
    return account_mint, account_owner


def _detect_transfers(
    tx: dict[str, Any], keys: list[str], wallet: str
) -> tuple[set[str], set[str]]:
    """Минты, которые кошелёк отправил/получил обычным spl-token transfer.

    Свапы тоже содержат transfer'ы, поэтому это лишь ПОДСКАЗКА: решающим
    остаётся отсутствие встречного движения квотной валюты.
    """
    account_mint, account_owner = _token_account_maps(tx, keys)
    out_mints: set[str] = set()
    in_mints: set[str] = set()

    for ix in _iter_instructions(tx):
        if ix.get("program") not in TOKEN_PROGRAM_NAMES:
            continue
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") not in _TRANSFER_TYPES:
            continue
        info = parsed.get("info") or {}
        source = info.get("source")
        destination = info.get("destination")
        mint = info.get("mint") or account_mint.get(source) or account_mint.get(destination)
        if not mint:
            continue
        authority = info.get("authority") or info.get("multisigAuthority")

        source_is_ours = account_owner.get(source) == wallet or authority == wallet
        dest_is_ours = account_owner.get(destination) == wallet
        if source_is_ours and not dest_is_ours:
            out_mints.add(mint)
        if dest_is_ours and not source_is_ours:
            in_mints.add(mint)
    return out_mints, in_mints


def _program_ids(tx: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for ix in _iter_instructions(tx):
        program_id = ix.get("programId")
        if program_id:
            ids.add(str(program_id))
    return ids


def extract_signature(tx: dict[str, Any]) -> str:
    signatures = (tx.get("transaction") or {}).get("signatures") or []
    return str(signatures[0]) if signatures else ""


# --------------------------------------------------------------------------- #
# основной разбор
# --------------------------------------------------------------------------- #

def parse_wallet_activity(
    tx: dict[str, Any],
    wallet: str,
    *,
    stable_mints: dict[str, int] | None = None,
) -> WalletActivity | None:
    """tx (jsonParsed) -> дельты балансов кошелька. None, если tx не разобрать."""
    if not isinstance(tx, dict):
        return None
    meta = tx.get("meta")
    if meta is None:
        log.debug("tx без meta — пропуск")
        return None

    stables = STABLE_MINTS if stable_mints is None else stable_mints
    keys = _account_keys(tx)

    pre_totals = _token_totals(meta.get("preTokenBalances"), wallet)
    post_totals = _token_totals(meta.get("postTokenBalances"), wallet)

    wsol_delta = 0
    stable_deltas: dict[str, TokenBalanceDelta] = {}
    token_deltas: dict[str, TokenBalanceDelta] = {}

    for mint in set(pre_totals) | set(post_totals):
        pre_raw, pre_dec = pre_totals.get(mint, (0, 0))
        post_raw, post_dec = post_totals.get(mint, (0, 0))
        decimals = post_dec or pre_dec
        delta = TokenBalanceDelta(mint=mint, decimals=decimals, pre_raw=pre_raw, post_raw=post_raw)

        if mint == WSOL_MINT:
            wsol_delta = delta.delta_raw  # wSOL имеет 9 decimals == лампорты
        elif mint in stables:
            stable_deltas[mint] = delta
        else:
            token_deltas[mint] = delta

    out_mints, in_mints = _detect_transfers(tx, keys, wallet)

    return WalletActivity(
        wallet=wallet,
        signature=extract_signature(tx),
        slot=int(tx.get("slot") or 0),
        block_time=tx.get("blockTime"),
        succeeded=meta.get("err") is None,
        err=meta.get("err"),
        fee_lamports=int(meta.get("fee") or 0),
        native_delta_lamports=_native_delta_lamports(tx, keys, wallet),
        wsol_delta_lamports=wsol_delta,
        stable_deltas=stable_deltas,
        token_deltas=token_deltas,
        transfer_out_mints=out_mints,
        transfer_in_mints=in_mints,
        program_ids=_program_ids(tx),
    )


# --------------------------------------------------------------------------- #
# интерпретация
# --------------------------------------------------------------------------- #

def _pick_quote(
    activity: WalletActivity,
    *,
    spent: bool,
    sol_dust_lamports: int,
    stable_dust_raw: int,
) -> tuple[str, Decimal] | None:
    """Какой валютой платили (spent=True) / что получили (spent=False).

    Приоритет у SOL: подавляющее большинство мемкоин-свапов идёт через него.
    """
    sign = -1 if spent else 1
    sol_delta = activity.sol_delta_lamports
    if sign * sol_delta >= sol_dust_lamports:
        return NATIVE_SOL, Decimal(abs(sol_delta)) / DEC_LAMPORTS_PER_SOL

    best: tuple[str, Decimal] | None = None
    for mint, delta in activity.stable_deltas.items():
        raw = delta.delta_raw
        if sign * raw < stable_dust_raw:
            continue
        amount = abs(delta.delta_ui)
        if best is None or amount > best[1]:
            best = (mint, amount)
    return best


def _make_event(
    activity: WalletActivity,
    delta: TokenBalanceDelta,
    kind: EventKind,
    *,
    quote: tuple[str, Decimal] | None,
    exact: bool,
    note: str = "",
) -> TradeEvent:
    amount = abs(delta.delta_ui)
    quote_mint = quote[0] if quote else None
    quote_amount = quote[1] if quote else Decimal(0)
    price: Decimal | None = None
    if exact and quote and amount > 0:
        price = quote_amount / amount
    return TradeEvent(
        kind=kind,
        signature=activity.signature,
        slot=activity.slot,
        block_time=activity.block_time,
        mint=delta.mint,
        decimals=delta.decimals,
        token_pre_raw=delta.pre_raw,
        token_post_raw=delta.post_raw,
        token_amount_ui=amount,
        quote_mint=quote_mint,
        quote_amount_ui=quote_amount if exact else Decimal(0),
        price_in_quote=price,
        exact_price=exact,
        note=note,
        wallet=activity.wallet,
    )


def classify(
    activity: WalletActivity,
    *,
    sol_dust_lamports: int,
    stable_dust_raw: int,
) -> list[TradeEvent]:
    """Дельты -> список торговых событий. Продажи идут первыми.

    Порядок важен: если цель свапнула A -> B, а мы держим A, продажу нужно
    зафиксировать до открытия позиции по B.
    """
    if not activity.succeeded:
        return []

    tokens_in = [d for d in activity.token_deltas.values() if d.delta_raw > 0]
    tokens_out = [d for d in activity.token_deltas.values() if d.delta_raw < 0]

    if not tokens_in and not tokens_out:
        if activity.wsol_delta_lamports and abs(activity.sol_delta_lamports) < sol_dust_lamports:
            return [_wrap_event(activity)]
        return []

    quote_spent = _pick_quote(
        activity, spent=True,
        sol_dust_lamports=sol_dust_lamports, stable_dust_raw=stable_dust_raw,
    )
    quote_received = _pick_quote(
        activity, spent=False,
        sol_dust_lamports=sol_dust_lamports, stable_dust_raw=stable_dust_raw,
    )

    # Свап токен->токен: квотная валюта не двигалась (только пыль на комиссии).
    token_to_token = bool(tokens_in and tokens_out and not quote_spent and not quote_received)

    events: list[TradeEvent] = []

    # ---- продажи / отправки ----
    for delta in sorted(tokens_out, key=lambda d: d.delta_raw):
        if token_to_token:
            events.append(_make_event(
                activity, delta, EventKind.SELL, quote=None, exact=False,
                note="token->token swap, цена из Price API",
            ))
        elif quote_received:
            exact = len(tokens_out) == 1 and not tokens_in
            note = "" if exact else "несколько токенов в одной tx, цена из Price API"
            events.append(_make_event(
                activity, delta, EventKind.SELL, quote=quote_received, exact=exact, note=note,
            ))
        else:
            hinted = delta.mint in activity.transfer_out_mints
            events.append(_make_event(
                activity, delta, EventKind.TRANSFER_OUT, quote=None, exact=False,
                note="spl-token transfer" if hinted else "баланс упал без встречного прихода",
            ))

    # ---- покупки / приходы ----
    for delta in sorted(tokens_in, key=lambda d: -d.delta_raw):
        if token_to_token:
            events.append(_make_event(
                activity, delta, EventKind.BUY, quote=None, exact=False,
                note="token->token swap, цена из Price API",
            ))
        elif quote_spent:
            exact = len(tokens_in) == 1 and not tokens_out
            note = "" if exact else "несколько токенов в одной tx, цена из Price API"
            events.append(_make_event(
                activity, delta, EventKind.BUY, quote=quote_spent, exact=exact, note=note,
            ))
        else:
            events.append(_make_event(
                activity, delta, EventKind.TRANSFER_IN, quote=None, exact=False,
                note="приход без траты квотной валюты (airdrop / входящий перевод)",
            ))

    return events


def _wrap_event(activity: WalletActivity) -> TradeEvent:
    amount = Decimal(abs(activity.wsol_delta_lamports)) / DEC_LAMPORTS_PER_SOL
    return TradeEvent(
        kind=EventKind.WRAP_UNWRAP,
        wallet=activity.wallet,
        signature=activity.signature,
        slot=activity.slot,
        block_time=activity.block_time,
        mint=WSOL_MINT,
        decimals=9,
        token_pre_raw=0,
        token_post_raw=0,
        token_amount_ui=amount,
        quote_mint=NATIVE_SOL,
        quote_amount_ui=amount,
        price_in_quote=Decimal(1),
        exact_price=True,
        note="wrap/unwrap SOL — не торговля",
    )


def looks_like_plain_transfer(activity: WalletActivity) -> bool:
    """В транзакции нет ни одной торговой программы — только системные."""
    return bool(activity.program_ids) and activity.program_ids <= PLUMBING_PROGRAMS
