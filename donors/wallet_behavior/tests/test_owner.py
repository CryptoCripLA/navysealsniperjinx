"""Тесты логики ведущего (owner) при нескольких отслеживаемых кошельках.

Спецификация в исполняемом виде:
  * позицию открывает тот, кто купил первым — по СЛОТУ транзакции, а не по
    порядку обработки ботом;
  * покупки того же токена другими кошельками пропускаются;
  * зеркалим только продажи ведущего, чужие игнорируем;
  * после закрытия позиции токен снова свободен;
  * PnL считается в разрезе ведущих.

Запуск: `pytest tests/` либо `python tests/test_owner.py` (без pytest).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copytrade.config import Config  # noqa: E402
from copytrade.constants import NATIVE_SOL, display_width, pad_display  # noqa: E402
from copytrade.models import BotState, CloseReason, EventKind, TradeEvent  # noqa: E402
from copytrade.portfolio import Portfolio  # noqa: E402
from copytrade.stats import build_summary, render_wallets  # noqa: E402
from copytrade.storage import StateStore  # noqa: E402
from copytrade.strategy import Action, Strategy  # noqa: E402

W1 = "Wa11etOneAddress111111111111111111111111111"
W2 = "Wa11etTwoAddress222222222222222222222222222"
W3 = "Wa11etThreeAddress33333333333333333333333333"
MEME = "MemeToken1111111111111111111111111111111111"

DEC = 6
UNIT = 10**DEC
SOL_USD = Decimal("200")


class FakePrices:
    """Заглушка PriceService: стратегия ходит только за этими двумя методами."""

    def __init__(self, token_price: Decimal | None = Decimal("0.000001")) -> None:
        self.token_price = token_price

    async def sol_usd(self, force: bool = False) -> Decimal:
        return SOL_USD

    async def token_price_sol(self, mint: str) -> Decimal | None:
        return self.token_price


def make_config(**overrides) -> Config:
    params = {
        "rpc_http_url": "https://example.invalid",
        "target_wallets": [f"{W1}=кошелёк №1", f"{W2}=кошелёк №2", f"{W3}=кошелёк №3"],
    }
    params.update(overrides)
    cfg = Config(**params)
    cfg.validate()
    return cfg


def make_state(cfg: Config) -> BotState:
    return BotState(
        target_wallets=cfg.wallet_addresses,
        wallet_names=cfg.wallet_names,
    )


def buy_event(wallet: str, *, slot: int, sig: str, tokens: int = 3_000_000,
              sol: str = "3", block_time: int | None = None) -> TradeEvent:
    return TradeEvent(
        kind=EventKind.BUY,
        signature=sig,
        slot=slot,
        block_time=block_time if block_time is not None else 1_700_000_000 + slot,
        mint=MEME,
        decimals=DEC,
        token_pre_raw=0,
        token_post_raw=tokens * UNIT,
        token_amount_ui=Decimal(tokens),
        quote_mint=NATIVE_SOL,
        quote_amount_ui=Decimal(sol),
        price_in_quote=Decimal(sol) / Decimal(tokens),
        exact_price=True,
        wallet=wallet,
    )


def sell_event(wallet: str, *, slot: int, sig: str, pre: int, post: int,
               sol: str = "1") -> TradeEvent:
    return TradeEvent(
        kind=EventKind.SELL,
        signature=sig,
        slot=slot,
        block_time=1_700_000_000 + slot,
        mint=MEME,
        decimals=DEC,
        token_pre_raw=pre * UNIT,
        token_post_raw=post * UNIT,
        token_amount_ui=Decimal(pre - post),
        quote_mint=NATIVE_SOL,
        quote_amount_ui=Decimal(sol),
        price_in_quote=Decimal(sol) / Decimal(max(1, pre - post)),
        exact_price=True,
        wallet=wallet,
    )


def transfer_event(wallet: str, *, slot: int, sig: str, pre: int, post: int) -> TradeEvent:
    return TradeEvent(
        kind=EventKind.TRANSFER_OUT,
        signature=sig,
        slot=slot,
        block_time=1_700_000_000 + slot,
        mint=MEME,
        decimals=DEC,
        token_pre_raw=pre * UNIT,
        token_post_raw=post * UNIT,
        token_amount_ui=Decimal(pre - post),
        quote_mint=None,
        quote_amount_ui=Decimal(0),
        price_in_quote=None,
        exact_price=False,
        wallet=wallet,
    )


def setup(**cfg_overrides):
    cfg = make_config(**cfg_overrides)
    state = make_state(cfg)
    portfolio = Portfolio(state)
    strategy = Strategy(cfg, portfolio, FakePrices())
    return cfg, state, portfolio, strategy


def decide(strategy: Strategy, event: TradeEvent):
    return asyncio.run(strategy.decide(event))


def open_from(portfolio: Portfolio, cfg: Config, event: TradeEvent, decision) -> None:
    """Мини-версия engine._execute_buy — только то, что нужно тестам."""
    portfolio.open_position(
        mint=event.mint,
        symbol="MEME",
        decimals=event.decimals,
        tokens=cfg.position_size_sol / decision.price_sol,
        cost_sol=cfg.position_size_sol,
        entry_price_sol=decision.price_sol,
        sol_price_usd=decision.sol_price_usd,
        signature=event.signature,
        target_tokens_after_buy=decision.target_tokens_after,
        target_buy_usd=decision.usd_volume,
        owner_wallet=event.wallet,
        owner_name=cfg.wallet_name(event.wallet),
        owner_slot=event.slot,
        owner_block_time=event.block_time,
    )


# --------------------------------------------------------------------------- #
# кто первый — тот и ведущий
# --------------------------------------------------------------------------- #

def test_first_buyer_becomes_owner() -> None:
    cfg, _state, portfolio, strategy = setup()
    event = buy_event(W1, slot=100, sig="sigA")
    decision = decide(strategy, event)
    assert decision.action is Action.COPY_BUY, decision.reason
    open_from(portfolio, cfg, event, decision)

    position = portfolio.get(MEME)
    assert position.owner_wallet == W1
    assert position.owner_name == "кошелёк №1"
    assert position.owner_slot == 100


def test_second_wallet_buy_is_skipped_with_owner_in_reason() -> None:
    cfg, _state, portfolio, strategy = setup()
    first = buy_event(W1, slot=100, sig="sigA")
    open_from(portfolio, cfg, first, decide(strategy, first))

    decision = decide(strategy, buy_event(W2, slot=101, sig="sigB"))
    assert decision.action is Action.SKIP_BUY
    assert decision.details["rule"] == "NOT_OWNER"
    assert decision.details["owner"] == W1
    assert "уже держим, ведущий — кошелёк №1" in decision.reason


def test_owner_rebuy_is_marked_as_rebuy_not_as_foreign() -> None:
    cfg, _state, portfolio, strategy = setup()
    first = buy_event(W1, slot=100, sig="sigA")
    open_from(portfolio, cfg, first, decide(strategy, first))

    decision = decide(strategy, buy_event(W1, slot=105, sig="sigC"))
    assert decision.action is Action.SKIP_BUY
    assert decision.details["rule"] == "TARGET_RE_BUY"


def test_earlier_buy_arriving_late_flags_owner_reassign() -> None:
    # Догон после реконнекта: покупка из слота 90 приезжает после слота 100.
    cfg, _state, portfolio, strategy = setup()
    late = buy_event(W2, slot=100, sig="sigLate")
    open_from(portfolio, cfg, late, decide(strategy, late))

    decision = decide(strategy, buy_event(W1, slot=90, sig="sigEarly"))
    assert decision.action is Action.SKIP_BUY
    assert decision.details["rule"] == "OWNER_EARLIER", "слот 90 < 100 — ведущего надо сменить"
    assert "переназначаем" in decision.reason


def test_reassign_changes_attribution_but_not_entry_economics() -> None:
    cfg, _state, portfolio, strategy = setup()
    late = buy_event(W2, slot=100, sig="sigLate")
    open_from(portfolio, cfg, late, decide(strategy, late))
    position = portfolio.get(MEME)
    entry_before = (position.entry_price_sol, position.cost_sol, position.tokens_initial)

    portfolio.reassign_owner(position, wallet=W1, name="кошелёк №1", slot=90,
                             block_time=1_700_000_090)
    assert position.owner_wallet == W1
    assert position.owner_slot == 90
    assert (position.entry_price_sol, position.cost_sol, position.tokens_initial) == entry_before, (
        "цена входа исполнена по факту — переписывать её задним числом нельзя"
    )


def test_reassign_not_offered_after_first_sell() -> None:
    # Продажу уже зеркалили от текущего ведущего: менять атрибуцию поздно.
    cfg, _state, portfolio, strategy = setup()
    late = buy_event(W2, slot=100, sig="sigLate")
    open_from(portfolio, cfg, late, decide(strategy, late))
    position = portfolio.get(MEME)
    portfolio.register_sell(
        position, fraction=Decimal("0.25"), tokens_sold=position.tokens_remaining / 4,
        sol_received=Decimal("0.2"), price_sol=Decimal("0.0000012"),
        sol_price_usd=SOL_USD, signature="sigSell",
    )

    decision = decide(strategy, buy_event(W1, slot=90, sig="sigEarly"))
    assert decision.details["rule"] == "NOT_OWNER"


# --------------------------------------------------------------------------- #
# продажи
# --------------------------------------------------------------------------- #

def test_owner_sell_is_mirrored() -> None:
    cfg, _state, portfolio, strategy = setup()
    first = buy_event(W1, slot=100, sig="sigA")
    open_from(portfolio, cfg, first, decide(strategy, first))

    decision = decide(strategy, sell_event(W1, slot=110, sig="sigS1",
                                           pre=3_000_000, post=2_250_000))
    assert decision.action is Action.COPY_SELL
    assert decision.fraction == Decimal("0.25")


def test_non_owner_sell_is_skipped() -> None:
    cfg, _state, portfolio, strategy = setup()
    first = buy_event(W1, slot=100, sig="sigA")
    open_from(portfolio, cfg, first, decide(strategy, first))

    decision = decide(strategy, sell_event(W2, slot=111, sig="sigS2",
                                           pre=5_000_000, post=0))
    assert decision.action is Action.SKIP_SELL
    assert decision.details["rule"] == "NOT_OWNER"
    assert decision.details["owner"] == W1


def test_non_owner_full_exit_does_not_close_position() -> None:
    # Кошелёк №2 вышел из токена полностью — на нашу позицию это не влияет.
    cfg, _state, portfolio, strategy = setup()
    first = buy_event(W1, slot=100, sig="sigA")
    open_from(portfolio, cfg, first, decide(strategy, first))

    decision = decide(strategy, sell_event(W2, slot=111, sig="sigS2",
                                           pre=5_000_000, post=0))
    assert decision.close_reason is None
    assert portfolio.has_open(MEME), "позиция ведущего осталась открытой"


def test_non_owner_transfer_out_is_log_only() -> None:
    cfg, _state, portfolio, strategy = setup(treat_transfer_as_sell=True)
    first = buy_event(W1, slot=100, sig="sigA")
    open_from(portfolio, cfg, first, decide(strategy, first))

    decision = decide(strategy, transfer_event(W2, slot=112, sig="sigT", pre=1_000, post=0))
    assert decision.action is Action.LOG_ONLY
    assert decision.details["rule"] == "NOT_OWNER"


def test_owner_full_exit_closes_position() -> None:
    cfg, _state, portfolio, strategy = setup()
    first = buy_event(W1, slot=100, sig="sigA")
    open_from(portfolio, cfg, first, decide(strategy, first))

    decision = decide(strategy, sell_event(W1, slot=120, sig="sigS3",
                                           pre=3_000_000, post=0))
    assert decision.action is Action.COPY_SELL
    assert decision.fraction == Decimal(1)
    assert decision.close_reason is CloseReason.TARGET_SOLD_ALL


# --------------------------------------------------------------------------- #
# токен освобождается после закрытия
# --------------------------------------------------------------------------- #

def test_token_is_free_again_after_close() -> None:
    cfg, _state, portfolio, strategy = setup()
    first = buy_event(W1, slot=100, sig="sigA")
    open_from(portfolio, cfg, first, decide(strategy, first))
    portfolio.close(portfolio.get(MEME), CloseReason.TARGET_SOLD_ALL)

    second = buy_event(W2, slot=200, sig="sigD")
    decision = decide(strategy, second)
    assert decision.action is Action.COPY_BUY, decision.reason
    open_from(portfolio, cfg, second, decision)
    assert portfolio.get(MEME).owner_wallet == W2, "новая позиция — новый ведущий"


def test_reentry_blocked_flag_still_works() -> None:
    cfg, _state, portfolio, strategy = setup(allow_reentry=False)
    first = buy_event(W1, slot=100, sig="sigA")
    open_from(portfolio, cfg, first, decide(strategy, first))
    portfolio.close(portfolio.get(MEME), CloseReason.TARGET_SOLD_ALL)

    decision = decide(strategy, buy_event(W2, slot=200, sig="sigD"))
    assert decision.details["rule"] == "REENTRY_BLOCKED"


def test_legacy_position_without_owner_is_mirrored_for_anyone() -> None:
    # Состояние из версии до мульти-кошельков: ведущего нет, зеркалим как раньше.
    cfg, _state, portfolio, strategy = setup()
    first = buy_event(W1, slot=100, sig="sigA")
    open_from(portfolio, cfg, first, decide(strategy, first))
    position = portfolio.get(MEME)
    position.owner_wallet = ""
    position.owner_name = ""

    decision = decide(strategy, sell_event(W2, slot=110, sig="sigS",
                                           pre=3_000_000, post=1_500_000))
    assert decision.action is Action.COPY_SELL


# --------------------------------------------------------------------------- #
# статистика в разрезе кошельков
# --------------------------------------------------------------------------- #

def test_summary_splits_pnl_by_owner() -> None:
    cfg, state, portfolio, strategy = setup()
    for wallet, slot, sig, pnl in ((W1, 100, "sigA", Decimal("0.3")),
                                   (W2, 200, "sigB", Decimal("-0.1"))):
        event = buy_event(wallet, slot=slot, sig=sig)
        event.mint = f"{MEME[:-1]}{slot}"       # разные минты — разные позиции
        decision = decide(strategy, event)
        open_from(portfolio, cfg, event, decision)
        position = portfolio.get(event.mint)
        position.realized_pnl_sol = pnl
        position.realized_pnl_usd = pnl * SOL_USD
        portfolio.close(position, CloseReason.TARGET_SOLD_ALL)

    summary = build_summary(state, {}, SOL_USD)
    by_wallet = {view.wallet: view for view in summary.wallets}
    assert by_wallet[W1].realized_pnl_sol == Decimal("0.3")
    assert by_wallet[W1].wins == 1
    assert by_wallet[W2].realized_pnl_sol == Decimal("-0.1")
    assert by_wallet[W2].losses == 1
    assert W3 in by_wallet, "кошелёк без сделок тоже должен быть в отчёте"
    assert by_wallet[W3].positions == 0
    assert summary.wallets[0].wallet == W1, "сортировка по PnL: прибыльный сверху"


def test_wallet_table_columns_do_not_shift_on_cjk_names() -> None:
    cfg = make_config(target_wallets=[f"{W1}=富贵", f"{W2}=обычный"])
    state = make_state(cfg)
    portfolio = Portfolio(state)
    strategy = Strategy(cfg, portfolio, FakePrices())
    event = buy_event(W1, slot=100, sig="sigA")
    open_from(portfolio, cfg, event, decide(strategy, event))

    lines = render_wallets(build_summary(state, {}, SOL_USD), color=False).splitlines()
    body = [line for line in lines if line.strip()]
    widths = {display_width(line.rstrip()) for line in body}
    assert len(widths) <= 2, f"строки таблицы разъехались: ширины {sorted(widths)}"
    assert display_width("富贵") == 4 and display_width(pad_display("富贵", 6)) == 6


# --------------------------------------------------------------------------- #
# состояние
# --------------------------------------------------------------------------- #

def test_state_roundtrip_keeps_owner_and_per_wallet_cursors() -> None:
    cfg, state, portfolio, strategy = setup()
    event = buy_event(W1, slot=100, sig="sigA")
    open_from(portfolio, cfg, event, decide(strategy, event))
    state.cursor(W1).last_signature = "sigA"
    state.cursor(W1).last_slot = 100
    state.cursor(W2).last_signature = "sigZ"
    state.wallet_stats(W1).buys_copied = 1

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        store = StateStore(str(path))
        store.save(state)
        loaded = store.load(cfg.wallet_addresses, cfg.wallet_names)

    assert loaded.cursor(W1).last_signature == "sigA"
    assert loaded.cursor(W1).last_slot == 100
    assert loaded.cursor(W2).last_signature == "sigZ"
    assert loaded.wallet_stats(W1).buys_copied == 1
    assert loaded.open_positions[MEME].owner_wallet == W1
    assert loaded.open_positions[MEME].owner_slot == 100


def test_v1_state_migrates_to_multi_wallet() -> None:
    legacy = {
        "version": 1,
        "target_wallet": W1,
        "last_signature": "sigOld",
        "last_slot": 42,
        "processed_signatures": ["sigOld"],
        "open_positions": {},
        "closed_positions": [],
        "counters": {},
        "symbols": {},
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = StateStore(str(path)).load([W1], {W1: "кошелёк №1"})

    assert loaded.target_wallets == [W1]
    assert loaded.cursor(W1).last_signature == "sigOld", "курсор старого формата не потерян"
    assert loaded.cursor(W1).last_slot == 42


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
