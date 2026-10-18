"""Порог мелких продаж: MIN_SELL_FRACTION_PCT.

Ведущий может спамить продажами по 1%. Каждая такая зеркальная сделка стоит
PRIORITY_FEE + BRIBE, и на комиссии уйдёт больше, чем даст выход. Но просто
игнорировать мелочь нельзя: доли считаются от ОСТАТКА и кумулятивны по
построению, поэтому выброшенные проценты навсегда рассинхронизировали бы наш
остаток с остатком ведущего (десять продаж по 1% оставят цели 90.4%, а нам
100%). Поэтому пропущенные доли копятся произведением (1 - доля) и исполняются
одной сделкой, когда накопленное перевалит за порог.

Запуск: `pytest tests/` либо `python tests/test_min_sell.py` (без pytest).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copytrade.config import Config, ConfigError  # noqa: E402
from copytrade.constants import NATIVE_SOL  # noqa: E402
from copytrade.models import BotState, EventKind, TradeEvent  # noqa: E402
from copytrade.portfolio import Portfolio  # noqa: E402
from copytrade.storage import StateStore  # noqa: E402
from copytrade.strategy import Action, Strategy  # noqa: E402

W1 = "Wa11etOneAddress111111111111111111111111111"
MEME = "MemeToken1111111111111111111111111111111111"

DEC = 6
UNIT = 10**DEC
SOL_USD = Decimal("200")
ONE = Decimal(1)


class FakePrices:
    async def sol_usd(self, force: bool = False) -> Decimal:
        return SOL_USD

    async def token_price_sol(self, mint: str) -> Decimal:
        return Decimal("0.000001")


def setup(**overrides):
    params = {"rpc_http_url": "https://example.invalid",
              "target_wallets": [f"{W1}=кошелёк №1"]}
    params.update(overrides)
    cfg = Config(**params)
    cfg.validate()
    state = BotState(target_wallets=cfg.wallet_addresses, wallet_names=cfg.wallet_names)
    portfolio = Portfolio(state)
    strategy = Strategy(cfg, portfolio, FakePrices())

    position = portfolio.open_position(
        mint=MEME, symbol="MEME", decimals=DEC,
        tokens=Decimal("500000"), cost_sol=Decimal("0.5"),
        entry_price_sol=Decimal("0.000001"), sol_price_usd=SOL_USD,
        signature="sigBuy", target_tokens_after_buy=Decimal("3000000"),
        target_buy_usd=Decimal("600"), owner_wallet=W1, owner_name="кошелёк №1",
        owner_slot=100, owner_block_time=1_700_000_100,
    )
    return cfg, state, portfolio, strategy, position


def sell_event(*, slot: int, sig: str, pre: int, post: int) -> TradeEvent:
    return TradeEvent(
        kind=EventKind.SELL, signature=sig, slot=slot,
        block_time=1_700_000_000 + slot, mint=MEME, decimals=DEC,
        token_pre_raw=pre * UNIT, token_post_raw=post * UNIT,
        token_amount_ui=Decimal(pre - post), quote_mint=NATIVE_SOL,
        quote_amount_ui=Decimal("1"),
        price_in_quote=Decimal(1) / Decimal(max(1, pre - post)),
        exact_price=True, wallet=W1,
    )


def decide(strategy: Strategy, event: TradeEvent):
    return asyncio.run(strategy.decide(event))


def apply_skip(position, decision) -> None:
    """Мини-версия engine._defer_dust_sell."""
    position.pending_sell_remaining = Decimal(decision.details["pending_remaining"])
    position.skipped_dust_sells += 1


def apply_sell(position, decision) -> None:
    """Мини-версия engine._execute_sell в части сброса накопленного."""
    position.pending_sell_remaining = ONE
    position.skipped_dust_sells = 0


# --------------------------------------------------------------------------- #
# порог
# --------------------------------------------------------------------------- #

def test_small_sell_is_skipped() -> None:
    _cfg, _state, _pf, strategy, _pos = setup()
    # 1% остатка: 3 000 000 -> 2 970 000
    decision = decide(strategy, sell_event(slot=101, sig="s1", pre=3_000_000, post=2_970_000))
    assert decision.action is Action.SKIP_SELL, decision.reason
    assert decision.details["rule"] == "BELOW_MIN_FRACTION", decision.details


def test_sell_at_threshold_is_mirrored() -> None:
    _cfg, _state, _pf, strategy, _pos = setup()
    # ровно 10%: порог — «не меньше», а не «строго больше»
    decision = decide(strategy, sell_event(slot=101, sig="s1", pre=3_000_000, post=2_700_000))
    assert decision.action is Action.COPY_SELL, decision.reason
    assert decision.fraction == Decimal("0.1"), decision.fraction


def test_large_sell_passes_untouched() -> None:
    _cfg, _state, _pf, strategy, position = setup()
    decision = decide(strategy, sell_event(slot=101, sig="s1", pre=3_000_000, post=1_500_000))
    assert decision.action is Action.COPY_SELL
    assert decision.fraction == Decimal("0.5"), decision.fraction
    assert position.pending_sell_remaining == ONE, "нечего было копить"


def test_threshold_zero_disables_the_gate() -> None:
    _cfg, _state, _pf, strategy, _pos = setup(min_sell_fraction_pct=Decimal("0"))
    decision = decide(strategy, sell_event(slot=101, sig="s1", pre=3_000_000, post=2_997_000))
    assert decision.action is Action.COPY_SELL, "порог 0 = зеркалим всё"


def test_threshold_100_is_rejected_by_config() -> None:
    try:
        setup(min_sell_fraction_pct=Decimal("100"))
    except ConfigError as exc:
        assert "MIN_SELL_FRACTION_PCT" in str(exc)
    else:
        raise AssertionError("порог 100% должен отбиваться валидацией")


# --------------------------------------------------------------------------- #
# накопление
# --------------------------------------------------------------------------- #

def test_skipped_fractions_accumulate_and_fire() -> None:
    """Спам по 5%: две продажи копятся, третья исполняет всё разом."""
    _cfg, _state, _pf, strategy, position = setup()

    first = decide(strategy, sell_event(slot=101, sig="s1", pre=3_000_000, post=2_850_000))
    assert first.action is Action.SKIP_SELL
    apply_skip(position, first)
    assert position.pending_sell_remaining == Decimal("0.95")

    # 1 - 0.95*0.95 = 9.75% — всё ещё меньше 10%
    second = decide(strategy, sell_event(slot=102, sig="s2", pre=2_850_000, post=2_707_500))
    assert second.action is Action.SKIP_SELL, second.reason
    apply_skip(position, second)
    assert position.pending_sell_remaining == Decimal("0.9025")

    # 1 - 0.95^3 = 14.2625% — порог взят
    third = decide(strategy, sell_event(slot=103, sig="s3", pre=2_707_500, post=2_572_125))
    assert third.action is Action.COPY_SELL, third.reason
    assert third.fraction == Decimal("0.142625"), third.fraction
    assert third.details["target_fraction"] == "0.05"


def test_accumulated_fraction_keeps_us_in_sync_with_target() -> None:
    """Главное свойство: после исполнения наша доля остатка = доле ведущего."""
    _cfg, _state, _pf, strategy, position = setup()
    our_remaining = ONE          # доля от нашего остатка на момент старта
    target_remaining = ONE

    balance = 3_000_000
    for i in range(3):
        post = balance * 95 // 100
        decision = decide(strategy, sell_event(slot=101 + i, sig=f"s{i}", pre=balance, post=post))
        target_remaining *= Decimal("0.95")
        if decision.action is Action.SKIP_SELL:
            apply_skip(position, decision)
        else:
            our_remaining *= (ONE - decision.fraction)
            apply_sell(position, decision)
        balance = post

    assert our_remaining == target_remaining, (
        f"разъехались: у нас {our_remaining}, у ведущего {target_remaining}"
    )


def test_successful_sell_resets_the_accumulator() -> None:
    _cfg, _state, _pf, strategy, position = setup()
    skipped = decide(strategy, sell_event(slot=101, sig="s1", pre=3_000_000, post=2_940_000))
    apply_skip(position, skipped)
    assert position.skipped_dust_sells == 1

    big = decide(strategy, sell_event(slot=102, sig="s2", pre=2_940_000, post=1_470_000))
    assert big.action is Action.COPY_SELL
    apply_sell(position, big)
    assert position.pending_sell_remaining == ONE
    assert position.skipped_dust_sells == 0


# --------------------------------------------------------------------------- #
# полный выход порогом не блокируется
# --------------------------------------------------------------------------- #

def test_full_exit_is_never_blocked() -> None:
    """Ведущий сливает последний 1% — выйти важнее, чем сэкономить комиссию."""
    _cfg, _state, _pf, strategy, _pos = setup()
    decision = decide(strategy, sell_event(slot=101, sig="s1", pre=30_000, post=0))
    assert decision.action is Action.COPY_SELL, decision.reason
    assert decision.fraction == ONE, decision.fraction


def test_tiny_exit_after_accumulation_closes_position() -> None:
    _cfg, _state, _pf, strategy, position = setup()
    skipped = decide(strategy, sell_event(slot=101, sig="s1", pre=3_000_000, post=2_970_000))
    apply_skip(position, skipped)
    exit_all = decide(strategy, sell_event(slot=102, sig="s2", pre=2_970_000, post=0))
    assert exit_all.action is Action.COPY_SELL
    assert exit_all.fraction == ONE
    assert exit_all.close_reason is not None, "полный выход должен закрывать позицию"


# --------------------------------------------------------------------------- #
# состояние
# --------------------------------------------------------------------------- #

def test_accumulator_survives_restart() -> None:
    cfg, state, _pf, strategy, position = setup()
    skipped = decide(strategy, sell_event(slot=101, sig="s1", pre=3_000_000, post=2_850_000))
    apply_skip(position, skipped)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        store = StateStore(str(path))
        store.save(state)
        loaded = StateStore(str(path)).load(cfg.wallet_addresses, cfg.wallet_names)

    restored = loaded.open_positions[MEME]
    assert restored.pending_sell_remaining == Decimal("0.95"), restored.pending_sell_remaining
    assert restored.skipped_dust_sells == 1


def test_legacy_state_defaults_to_empty_accumulator() -> None:
    """Позиции из состояния до этой фичи не должны считаться «накопившими»."""
    from copytrade.models import Position

    _cfg, _state, _pf, _strategy, position = setup()
    raw = position.to_dict()
    del raw["pending_sell_remaining"]
    del raw["skipped_dust_sells"]
    restored = Position.from_dict(raw)
    assert restored.pending_sell_remaining == ONE
    assert restored.skipped_dust_sells == 0


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
