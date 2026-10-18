"""Разбор списка кошельков.

Регрессия на реальный баг: все четыре адреса были вставлены в СТАРОЕ поле
TARGET_WALLET (единственное число), а оно типа str и по запятым не резалось.
Первый адрес разобрался, остальные три молча уехали в «имя» — бот стартовал с
одним кошельком и ни одной ошибки не написал. Ошибка ценой пропущенных сделок,
поэтому здесь и корректный разбор, и запрет молчать.

Запуск: `pytest tests/` либо `python tests/test_wallets.py` (без pytest).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copytrade.config import Config, ConfigError, _build_wallets  # noqa: E402

A1 = "Wa11etOneAddress1111111111111111111111111111"
A2 = "Wa11etTwoAddress2222222222222222222222222222"
A3 = "Wa11etSixAddress3333333333333333333333333333"
A4 = "Wa11etFewAddress4444444444444444444444444444"

# Та самая форма, в которой список уезжал в старое односкошелёчное поле:
# четыре записи «АДРЕС=Имя» через запятую в одной строке.
GLUED = f"{A1}=кит,{A2}=инсайдер,{A3}=скальпер,{A4}=снайпер"


def _expect(specs, addresses, names) -> None:
    assert [w.address for w in specs] == addresses, [w.address for w in specs]
    assert [w.name for w in specs] == names, [w.name for w in specs]
    assert [w.index for w in specs] == list(range(1, len(addresses) + 1))


# --------------------------------------------------------------------------- #
# разбор
# --------------------------------------------------------------------------- #

def test_legacy_field_is_split_by_commas() -> None:
    """Сам баг: весь список в TARGET_WALLET -> четыре кошелька, а не один."""
    _expect(_build_wallets([], GLUED),
            [A1, A2, A3, A4], ["кит", "инсайдер", "скальпер", "снайпер"])


def test_list_field_with_one_glued_string() -> None:
    """TARGET_WALLETS в JSON списком из одной слипшейся строки."""
    _expect(_build_wallets([GLUED], ""),
            [A1, A2, A3, A4], ["кит", "инсайдер", "скальпер", "снайпер"])


def test_normal_list_still_works() -> None:
    _expect(_build_wallets([f"{A1}=смарт мани", A2], ""),
            [A1, A2], ["смарт мани", "кошелёк №2"])


def test_semicolons_and_newlines_are_separators() -> None:
    """Многострочное значение в .env и точка с запятой — тоже список."""
    _expect(_build_wallets([f"{A1}=один;{A2}=два\n{A3}"], ""),
            [A1, A2, A3], ["один", "два", "кошелёк №3"])


def test_legacy_duplicate_of_list_entry_is_ignored() -> None:
    """Миграция со старого .env: адрес есть и в новом поле, и в старом."""
    _expect(_build_wallets([f"{A1}=кит", A2], A1), [A1, A2], ["кит", "кошелёк №2"])


def test_duplicate_inside_the_list_is_rejected() -> None:
    try:
        _build_wallets([f"{A1}=a", f"{A1}=b"], "")
    except ConfigError as exc:
        assert "дважды" in str(exc), exc
    else:
        raise AssertionError("дубль адреса должен отбиваться")


# --------------------------------------------------------------------------- #
# молчать нельзя
# --------------------------------------------------------------------------- #

def test_address_hidden_in_name_is_rejected() -> None:
    """Разделитель, который мы не поддерживаем: падаем, а не теряем кошельки."""
    try:
        _build_wallets([f"{A1}=a {A2}=b"], "")
    except ConfigError as exc:
        assert "слипшийся" in str(exc), exc
    else:
        raise AssertionError("спрятанный в имени адрес должен отбиваться")


def test_second_equals_sign_in_name_is_rejected() -> None:
    try:
        _build_wallets([f"{A1}=имя=ещё"], "")
    except ConfigError as exc:
        assert "слипшийся" in str(exc), exc
    else:
        raise AssertionError("второй '=' в имени должен отбиваться")


def test_plain_name_with_spaces_and_digits_is_fine() -> None:
    """Проверка на ложные срабатывания: живые имена ломаться не должны."""
    _expect(_build_wallets([f"{A1}=смарт мани #2 (top-10)"], ""),
            [A1], ["смарт мани #2 (top-10)"])


def test_garbage_address_is_rejected() -> None:
    try:
        _build_wallets(["не_адрес=имя"], "")
    except ConfigError as exc:
        assert "base58" in str(exc), exc
    else:
        raise AssertionError("мусор вместо адреса должен отбиваться")


# --------------------------------------------------------------------------- #
# сквозь Config
# --------------------------------------------------------------------------- #

def test_config_end_to_end_through_legacy_field() -> None:
    cfg = Config(rpc_http_url="https://example.invalid", target_wallet=GLUED)
    cfg.validate()
    assert len(cfg.wallets) == 4, cfg.wallets
    assert cfg.wallet_addresses == [A1, A2, A3, A4]
    assert cfg.wallet_names[A2] == "инсайдер"


def test_config_json_objects_still_work() -> None:
    from copytrade.config import _as_str_list

    entries = _as_str_list([{"address": A1, "name": "смартмани"}, A2], "TARGET_WALLETS")
    _expect(_build_wallets(entries, ""), [A1, A2], ["смартмани", "кошелёк №2"])


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
