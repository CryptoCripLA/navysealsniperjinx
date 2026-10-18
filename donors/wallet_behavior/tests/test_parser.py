"""Тесты парсера — он же исполняемая спецификация распознавания сделок.

Парсер — самая рискованная часть: он единственный, кто решает, что вообще
произошло в транзакции. Фикстуры ниже — синтетические, но по форме совпадают
с ответом `getTransaction(encoding="jsonParsed")`.

Запуск: `pytest tests/` либо `python tests/test_parser.py` (без pytest).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copytrade.constants import (  # noqa: E402
    DEFAULT_SOL_DUST_LAMPORTS,
    NATIVE_SOL,
    USDC_MINT,
    WSOL_MINT,
)
from copytrade.models import EventKind  # noqa: E402
from copytrade.parser import classify, parse_wallet_activity  # noqa: E402

WALLET = "TargetWa11etAddress1111111111111111111111111"
OTHER = "OtherWa11etAddress11111111111111111111111111"
MEME = "MemeToken1111111111111111111111111111111111"
MEME2 = "MemeToken2222222222222222222222222222222222"
FEE = 5_000
SOL = 1_000_000_000

DUST = dict(sol_dust_lamports=DEFAULT_SOL_DUST_LAMPORTS, stable_dust_raw=10_000)


def tb(index: int, mint: str, owner: str, amount: int, decimals: int) -> dict:
    return {
        "accountIndex": index,
        "mint": mint,
        "owner": owner,
        "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "uiTokenAmount": {
            "amount": str(amount),
            "decimals": decimals,
            "uiAmount": amount / 10**decimals,
            "uiAmountString": str(amount / 10**decimals),
        },
    }


def make_tx(
    *,
    keys: list[str] | None = None,
    pre_balances: list[int] | None = None,
    post_balances: list[int] | None = None,
    pre_token: list[dict] | None = None,
    post_token: list[dict] | None = None,
    err=None,
    instructions: list[dict] | None = None,
    inner: list[dict] | None = None,
    signature: str = "TestSignature111",
    slot: int = 100,
) -> dict:
    keys = keys or [WALLET, "Program1111111111111111111111111111111111111"]
    pre_balances = pre_balances or [10 * SOL, 0]
    post_balances = post_balances or [10 * SOL - FEE, 0]
    return {
        "slot": slot,
        "blockTime": 1_700_000_000,
        "transaction": {
            "signatures": [signature],
            "message": {
                "accountKeys": [
                    {"pubkey": k, "signer": i == 0, "writable": True, "source": "transaction"}
                    for i, k in enumerate(keys)
                ],
                "instructions": instructions or [],
            },
        },
        "meta": {
            "err": err,
            "fee": FEE,
            "preBalances": pre_balances,
            "postBalances": post_balances,
            "preTokenBalances": pre_token or [],
            "postTokenBalances": post_token or [],
            "innerInstructions": inner or [],
        },
    }


def parse(tx: dict):
    activity = parse_wallet_activity(tx, WALLET)
    assert activity is not None
    return activity, classify(activity, **DUST)


# --------------------------------------------------------------------------- #

def test_buy_for_sol():
    """Потратил 1 SOL, получил 1 000 000 токенов -> BUY по 1e-6 SOL/шт."""
    tx = make_tx(
        pre_balances=[10 * SOL, 0],
        post_balances=[9 * SOL - FEE, 0],
        pre_token=[],
        post_token=[tb(2, MEME, WALLET, 1_000_000_000_000, 6)],
        keys=[WALLET, "Prog1111111111111111111111111111111111111111", "Ata11111111111111111111111111111111111111111"],
    )
    activity, events = parse(tx)

    assert activity.sol_delta_lamports == -SOL, "комиссия должна быть исключена"
    assert len(events) == 1
    event = events[0]
    assert event.kind is EventKind.BUY
    assert event.mint == MEME
    assert event.quote_mint == NATIVE_SOL
    assert event.quote_amount_ui == Decimal(1)
    assert event.token_amount_ui == Decimal(1_000_000)
    assert event.exact_price
    assert event.price_in_quote == Decimal(1) / Decimal(1_000_000)


def test_buy_paid_in_wsol_counts_the_same():
    """Оплата с wSOL-аккаунта, нативный баланс не менялся — всё равно покупка."""
    tx = make_tx(
        pre_balances=[10 * SOL, 0],
        post_balances=[10 * SOL - FEE, 0],
        pre_token=[tb(1, WSOL_MINT, WALLET, 2 * SOL, 9)],
        post_token=[
            tb(1, WSOL_MINT, WALLET, 1 * SOL, 9),
            tb(2, MEME, WALLET, 500_000_000, 6),
        ],
    )
    _, events = parse(tx)
    assert [e.kind for e in events] == [EventKind.BUY]
    assert events[0].quote_amount_ui == Decimal(1)


def test_wrap_unwrap_is_not_a_trade():
    """SOL -> wSOL: нативный минус, wSOL плюс. Ноль в сумме, торговли нет."""
    tx = make_tx(
        pre_balances=[10 * SOL, 0],
        post_balances=[9 * SOL - FEE, 0],
        pre_token=[],
        post_token=[tb(1, WSOL_MINT, WALLET, SOL, 9)],
    )
    activity, events = parse(tx)
    assert activity.sol_delta_lamports == 0
    assert all(e.kind is not EventKind.BUY for e in events)
    assert all(e.kind is not EventKind.SELL for e in events)


def test_partial_sell_fraction_is_of_current_balance():
    """Было 1000, стало 750 -> продал 25% ТЕКУЩЕГО остатка."""
    tx = make_tx(
        pre_balances=[10 * SOL, 0],
        post_balances=[10 * SOL + SOL // 2 - FEE, 0],
        pre_token=[tb(2, MEME, WALLET, 1_000_000_000, 6)],
        post_token=[tb(2, MEME, WALLET, 750_000_000, 6)],
    )
    _, events = parse(tx)
    assert len(events) == 1
    event = events[0]
    assert event.kind is EventKind.SELL
    assert event.sold_fraction == Decimal("0.25")
    assert not event.target_closed_out
    assert event.quote_amount_ui == Decimal("0.5")
    # цена выхода = 0.5 SOL за 250 токенов
    assert event.price_in_quote == Decimal("0.5") / Decimal(250)


def test_second_partial_sell_is_relative_to_new_balance():
    """750 -> 375: это 50% остатка, а не 25% исходного объёма."""
    tx = make_tx(
        pre_balances=[10 * SOL, 0],
        post_balances=[10 * SOL + SOL - FEE, 0],
        pre_token=[tb(2, MEME, WALLET, 750_000_000, 6)],
        post_token=[tb(2, MEME, WALLET, 375_000_000, 6)],
    )
    _, events = parse(tx)
    assert events[0].sold_fraction == Decimal("0.5")


def test_full_exit_marks_position_closed():
    tx = make_tx(
        pre_balances=[10 * SOL, 0],
        post_balances=[12 * SOL - FEE, 0],
        pre_token=[tb(2, MEME, WALLET, 375_000_000, 6)],
        post_token=[tb(2, MEME, WALLET, 0, 6)],
    )
    _, events = parse(tx)
    assert events[0].kind is EventKind.SELL
    assert events[0].target_closed_out
    assert events[0].sold_fraction == Decimal(1)


def test_transfer_out_without_incoming_sol():
    """Токен ушёл, SOL не пришёл -> это вывод, а не продажа."""
    transfer_ix = {
        "program": "spl-token",
        "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "parsed": {
            "type": "transferChecked",
            "info": {
                "authority": WALLET,
                "source": "SourceAta1111111111111111111111111111111111",
                "destination": "DestAta11111111111111111111111111111111111",
                "mint": MEME,
                "tokenAmount": {"amount": "500000000", "decimals": 6},
            },
        },
    }
    tx = make_tx(
        pre_balances=[10 * SOL, 0],
        post_balances=[10 * SOL - FEE, 0],
        pre_token=[tb(2, MEME, WALLET, 1_000_000_000, 6)],
        post_token=[tb(2, MEME, WALLET, 500_000_000, 6)],
        instructions=[transfer_ix],
    )
    activity, events = parse(tx)
    assert events[0].kind is EventKind.TRANSFER_OUT
    assert events[0].sold_fraction == Decimal("0.5")
    assert MEME in activity.transfer_out_mints


def test_buy_for_usdc():
    tx = make_tx(
        pre_balances=[10 * SOL, 0],
        post_balances=[10 * SOL - FEE, 0],
        pre_token=[tb(1, USDC_MINT, WALLET, 200_000_000, 6)],
        post_token=[
            tb(1, USDC_MINT, WALLET, 100_000_000, 6),
            tb(2, MEME, WALLET, 1_000_000_000, 6),
        ],
    )
    _, events = parse(tx)
    assert events[0].kind is EventKind.BUY
    assert events[0].quote_mint == USDC_MINT
    assert events[0].quote_amount_ui == Decimal(100)


def test_ata_rent_alone_is_not_a_buy():
    """Приход токена и трата ~0.002 SOL на аренду ATA — это не покупка."""
    rent = 2_039_280
    tx = make_tx(
        pre_balances=[10 * SOL, 0],
        post_balances=[10 * SOL - rent - FEE, 0],
        pre_token=[],
        post_token=[tb(2, MEME, WALLET, 1_000_000_000, 6)],
    )
    _, events = parse(tx)
    assert events[0].kind is EventKind.TRANSFER_IN


def test_token_to_token_swap_produces_sell_then_buy():
    tx = make_tx(
        pre_balances=[10 * SOL, 0],
        post_balances=[10 * SOL - FEE, 0],
        pre_token=[tb(2, MEME, WALLET, 1_000_000_000, 6)],
        post_token=[
            tb(2, MEME, WALLET, 0, 6),
            tb(3, MEME2, WALLET, 7_000_000_000, 6),
        ],
    )
    _, events = parse(tx)
    assert [e.kind for e in events] == [EventKind.SELL, EventKind.BUY]
    assert events[0].mint == MEME and events[0].target_closed_out
    assert events[1].mint == MEME2
    assert not events[1].exact_price, "цену такого свапа из tx не вывести"


def test_failed_transaction_yields_nothing():
    tx = make_tx(
        err={"InstructionError": [0, {"Custom": 1}]},
        pre_balances=[10 * SOL, 0],
        post_balances=[9 * SOL - FEE, 0],
        post_token=[tb(2, MEME, WALLET, 1_000_000_000, 6)],
    )
    activity, events = parse(tx)
    assert not activity.succeeded
    assert events == []


def test_other_wallets_balances_are_ignored():
    """В tx участвует чужой кошелёк — его токены не должны попасть в дельты."""
    tx = make_tx(
        pre_balances=[10 * SOL, 0],
        post_balances=[10 * SOL - FEE, 0],
        pre_token=[tb(3, MEME, OTHER, 5_000_000_000, 6)],
        post_token=[tb(3, MEME, OTHER, 1_000_000_000, 6)],
    )
    activity, events = parse(tx)
    assert activity.token_deltas == {}
    assert events == []


def test_lookup_table_addresses_are_appended():
    """Для v0-транзакций адреса из ALT приходят отдельно — индексы не должны разъехаться."""
    tx = make_tx(
        keys=["FeePayer1111111111111111111111111111111111", "Prog111111111111111111111111111111111111111"],
        pre_balances=[SOL, 0, 10 * SOL],
        post_balances=[SOL - FEE, 0, 9 * SOL],
        post_token=[tb(3, MEME, WALLET, 1_000_000_000, 6)],
    )
    tx["meta"]["loadedAddresses"] = {"writable": [WALLET], "readonly": []}
    activity, events = parse(tx)
    assert activity.native_delta_lamports == -SOL, "кошелёк найден по индексу 2 из ALT"
    assert events[0].kind is EventKind.BUY


# --------------------------------------------------------------------------- #

def _run_all() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{'ВСЁ ЗЕЛЁНОЕ' if failures == 0 else f'ПРОВАЛОВ: {failures}'}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
