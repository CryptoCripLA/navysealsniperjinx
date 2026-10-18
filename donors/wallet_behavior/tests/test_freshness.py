"""Свежесть сигнала: живой режим не покупает прошлое.

После простоя догон приносит все сделки цели за время отсутствия. Демо на них
строит точную историческую статистику — это правильно и трогать нельзя. Вживую
то же самое означает купить на реальные деньги токен, который цель слила два
часа назад, и тут же его продать, заплатив две комиссии.

Асимметрия намеренная: покупки из догона блокируются, ПРОДАЖИ — никогда. После
простоя продажа из догона единственный способ узнать, что цель вышла, пока мы
сидели в реальных токенах.

Запуск: `pytest tests/` либо `python tests/test_freshness.py` (без pytest).
"""

from __future__ import annotations

import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copytrade.config import Config, ConfigError  # noqa: E402
from copytrade.constants import NATIVE_SOL  # noqa: E402
from copytrade.engine import Engine  # noqa: E402
from copytrade.executor import PaperExecutor  # noqa: E402
from copytrade.logging_setup import EventLogger  # noqa: E402
from copytrade.models import BotState, EventKind, TradeEvent  # noqa: E402

W1 = "Wa11etOneAddress111111111111111111111111111"
MEME = "MemeToken1111111111111111111111111111111111"
NOW = 1_800_000_000.0


class FakePrices:
    async def sol_usd(self, force: bool = False) -> Decimal:
        return Decimal("200")

    async def token_price_sol(self, mint: str) -> Decimal:
        return Decimal("0.0000002")


def make_engine(*, live: bool, **overrides) -> Engine:
    params = {
        "rpc_http_url": "https://example.invalid",
        "target_wallets": [f"{W1}=кошелёк №1"],
        "demo_mode": True,          # валидацию проходим как демо: живой режим
    }                               # потребовал бы приватный ключ
    params.update(overrides)
    cfg = Config(**params)
    cfg.validate()
    cfg.demo_mode = not live
    state = BotState(target_wallets=[W1], wallet_names={W1: "кошелёк №1"})
    engine = Engine(cfg, state, rpc=None, prices=None, executor=PaperExecutor(),
                    store=None, events=EventLogger("copytrade.test", None))
    engine._started_at = NOW
    return engine


def buy_event(*, block_time: float | None) -> TradeEvent:
    return TradeEvent(
        kind=EventKind.BUY, signature="sigBuy", slot=100,
        block_time=int(block_time) if block_time else None,
        mint=MEME, decimals=6,
        token_pre_raw=0, token_post_raw=3_000_000_000_000,
        token_amount_ui=Decimal("3000000"), quote_mint=NATIVE_SOL,
        quote_amount_ui=Decimal("1"), price_in_quote=Decimal("0.0000003"),
        exact_price=True, wallet=W1,
    )


# --------------------------------------------------------------------------- #
# демо не трогаем
# --------------------------------------------------------------------------- #

def test_demo_copies_the_backlog_as_before() -> None:
    """Главное требование: в демо всё остаётся как было."""
    engine = make_engine(live=False)
    old = buy_event(block_time=NOW - 7200)          # два часа до старта
    assert engine._buy_block(old) == ("", ""), engine._buy_block(old)


def test_demo_ignores_the_knobs_entirely() -> None:
    engine = make_engine(live=False, max_signal_age_sec=1.0,
                         skip_buys_before_start=True)
    assert engine._buy_block(buy_event(block_time=NOW - 86400)) == ("", "")


# --------------------------------------------------------------------------- #
# живой режим: покупки
# --------------------------------------------------------------------------- #

def test_live_skips_buy_from_before_start() -> None:
    engine = make_engine(live=True)
    reason, rule = engine._buy_block(buy_event(block_time=NOW - 7200))
    assert rule == "STALE_SIGNAL", (reason, rule)
    assert "из прошлого" in reason, reason
    assert "SKIP_BUYS_BEFORE_START" in reason, reason


def test_live_copies_fresh_buy_after_start(monkeypatched_now=None) -> None:
    engine = make_engine(live=True)
    engine._started_at = time.time() - 300      # бот работает 5 минут
    fresh = buy_event(block_time=time.time() - 2)
    assert engine._buy_block(fresh) == ("", ""), engine._buy_block(fresh)


def test_live_skips_stale_buy_even_after_start() -> None:
    """Обрыв WS посреди работы: сигнал свежее старта, но всё равно протухший."""
    engine = make_engine(live=True, max_signal_age_sec=60.0)
    engine._started_at = time.time() - 3600
    stale = buy_event(block_time=time.time() - 900)     # 15 минут назад
    reason, rule = engine._buy_block(stale)
    assert rule == "STALE_SIGNAL", (reason, rule)
    assert "MAX_SIGNAL_AGE_SEC" in reason, reason


def test_live_without_block_time_does_not_buy() -> None:
    """Возраст неизвестен -> на реальных деньгах это повод не покупать."""
    engine = make_engine(live=True)
    reason, rule = engine._buy_block(buy_event(block_time=None))
    assert rule == "STALE_SIGNAL", (reason, rule)
    assert "blockTime" in reason, reason


def test_age_zero_disables_only_the_age_check() -> None:
    engine = make_engine(live=True, max_signal_age_sec=0.0)
    engine._started_at = time.time() - 3600
    old_but_after_start = buy_event(block_time=time.time() - 900)
    assert engine._buy_block(old_but_after_start) == ("", "")
    # ...а защита от истории до старта продолжает работать
    engine._started_at = NOW
    assert engine._buy_block(buy_event(block_time=NOW - 10))[1] == "STALE_SIGNAL"


def test_freshness_is_checked_before_risk_limits() -> None:
    """Причина в логе должна быть точной, а не первой попавшейся."""
    engine = make_engine(live=True, max_open_positions=1)
    reason, rule = engine._buy_block(buy_event(block_time=NOW - 7200))
    assert rule == "STALE_SIGNAL", "устаревший сигнал важнее, чем кап позиций"
    assert "MAX_OPEN_POSITIONS" not in reason, reason


# --------------------------------------------------------------------------- #
# продажи не блокируются никогда
# --------------------------------------------------------------------------- #

def test_sell_branch_never_consults_freshness() -> None:
    """Гейт висит ровно на COPY_BUY и больше нигде в диспетчере решений."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(Engine._handle_event)))
    called = [node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]

    assert called.count("_buy_block") == 1, called
    assert "_freshness_block" not in called, "гейт свежести просочился в диспетчер"
    assert "_execute_sell" in called, "тест смотрит не на тот метод"


def test_stale_sell_reaches_the_executor() -> None:
    """Продажа двухчасовой давности исполняется: цель вышла — выходим и мы.

    Именно этот случай ломался бы, повесь мы фильтр возраста на все события:
    после простоя бот остался бы в реальных токенах, которые цель уже слила.
    """
    import asyncio

    from copytrade.models import WalletActivity
    from copytrade.strategy import Strategy

    engine = make_engine(live=True)
    engine.strategy = Strategy(engine.cfg, engine.portfolio, FakePrices())
    engine.portfolio.open_position(
        mint=MEME, symbol="MEME", decimals=6,
        tokens=Decimal("500000"), cost_sol=Decimal("0.1"),
        entry_price_sol=Decimal("0.0000002"), sol_price_usd=Decimal("200"),
        signature="sigBuy", target_tokens_after_buy=Decimal("3000000"),
        target_buy_usd=Decimal("600"), owner_wallet=W1, owner_name="кошелёк №1",
        owner_slot=100, owner_block_time=int(NOW - 7300),
    )

    fired: list[str] = []

    async def fake_execute_sell(decision):
        fired.append(decision.event.signature)

    engine._execute_sell = fake_execute_sell  # type: ignore[assignment]

    # Полный выход цели двухчасовой давности: token_post_raw = 0
    sell = TradeEvent(
        kind=EventKind.SELL, signature="sigOldSell", slot=101,
        block_time=int(NOW - 7200), mint=MEME, decimals=6,
        token_pre_raw=3_000_000_000_000, token_post_raw=0,
        token_amount_ui=Decimal("3000000"), quote_mint=NATIVE_SOL,
        quote_amount_ui=Decimal("1"), price_in_quote=Decimal("0.0000003"),
        exact_price=True, wallet=W1,
    )
    assert sell.target_closed_out, "фикстура должна изображать полный выход"
    activity = WalletActivity(
        wallet=W1, signature="sigOldSell", slot=101, block_time=int(NOW - 7200),
        succeeded=True, err=None, fee_lamports=5000,
        native_delta_lamports=1_000_000_000, wsol_delta_lamports=0,
    )
    asyncio.run(engine._handle_event(sell, activity))

    assert fired == ["sigOldSell"], "устаревшая продажа не должна блокироваться"


# --------------------------------------------------------------------------- #
# конфиг
# --------------------------------------------------------------------------- #

def test_negative_age_is_rejected() -> None:
    try:
        make_engine(live=True, demo_mode=False, live_i_understand_the_risk=True,
                    wallet_private_key="x", max_signal_age_sec=-1.0)
    except ConfigError as exc:
        assert "MAX_SIGNAL_AGE_SEC" in str(exc), exc
    else:
        raise AssertionError("отрицательный порог должен отбиваться")


def test_disabling_both_guards_is_rejected() -> None:
    """Обе защиты выключены = после рестарта скупим всю историю за реальные SOL."""
    try:
        make_engine(live=True, demo_mode=False, live_i_understand_the_risk=True,
                    wallet_private_key="x", max_signal_age_sec=0.0,
                    skip_buys_before_start=False)
    except ConfigError as exc:
        assert "хотя бы одну" in str(exc), exc
    else:
        raise AssertionError("выключение обеих защит должно отбиваться")


def test_demo_may_disable_both_guards() -> None:
    """В демо эти ручки не действуют, поэтому и запрещать нечего."""
    engine = make_engine(live=False, max_signal_age_sec=0.0,
                         skip_buys_before_start=False)
    assert engine._buy_block(buy_event(block_time=NOW - 86400)) == ("", "")


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
