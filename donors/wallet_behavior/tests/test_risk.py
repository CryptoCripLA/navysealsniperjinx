"""Тесты лимитов живого режима: MAX_OPEN_POSITIONS и MAX_DAILY_LOSS_SOL.

Эти ручки валидируются конфигом и печатаются в `check`, поэтому пользователь им
доверяет. Тут проверяется, что они действительно останавливают покупки — молча
не работающий лимит убытка хуже, чем его отсутствие.

Запуск: `pytest tests/` либо `python tests/test_risk.py` (без pytest).
"""

from __future__ import annotations

import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copytrade.config import Config  # noqa: E402
from copytrade.engine import Engine  # noqa: E402
from copytrade.executor import PaperExecutor  # noqa: E402
from copytrade.logging_setup import EventLogger  # noqa: E402
from copytrade.models import BotState, PartialSell, Position  # noqa: E402

W1 = "Wa11etOneAddress111111111111111111111111111"
ZERO = Decimal(0)


def make_engine(*, live: bool, max_open: int = 5, max_loss: str = "1") -> Engine:
    cfg = Config(
        rpc_http_url="https://example.invalid",
        target_wallets=[f"{W1}=кошелёк №1"],
        demo_mode=True,                 # валидацию проходим как демо: живой
        max_open_positions=max_open,    # режим потребовал бы приватный ключ
        max_daily_loss_sol=Decimal(max_loss),
    )
    cfg.validate()
    cfg.demo_mode = not live
    state = BotState(target_wallets=[W1], wallet_names={W1: "кошелёк №1"})
    return Engine(cfg, state, rpc=None, prices=None,
                  executor=PaperExecutor(), store=None,
                  events=EventLogger("copytrade.test", None))


def add_position(state: BotState, mint: str) -> Position:
    position = Position(
        mint=mint, symbol=mint[:4], decimals=6, opened_at=time.time(),
        open_signature=f"sig_{mint}", entry_price_sol=Decimal("0.000001"),
        entry_price_usd=Decimal("0.0002"), sol_price_usd_at_entry=Decimal("200"),
        cost_sol=Decimal("0.5"), tokens_initial=Decimal("500000"),
        tokens_remaining=Decimal("500000"), target_tokens_at_entry=Decimal("3000000"),
        target_buy_usd=Decimal("600"), owner_wallet=W1, owner_name="кошелёк №1",
    )
    state.open_positions[mint] = position
    return position


def add_sell(position: Position, pnl_sol: str, ts: float) -> None:
    position.sells.append(PartialSell(
        ts=ts, signature=f"sell_{len(position.sells)}", fraction=Decimal("0.5"),
        tokens_sold=Decimal("1000"), price_sol=Decimal("0.000001"),
        sol_received=Decimal("0.1"), sol_price_usd=Decimal("200"),
        realized_pnl_sol=Decimal(pnl_sol), realized_pnl_usd=Decimal(pnl_sol) * 200,
    ))


# --------------------------------------------------------------------------- #
# MAX_OPEN_POSITIONS
# --------------------------------------------------------------------------- #

def test_open_positions_under_limit_allows_buy():
    engine = make_engine(live=True, max_open=3)
    for i in range(2):
        add_position(engine.state, f"Mint{i}")
    assert engine._risk_block() == "", "две позиции при лимите три — путь должен быть свободен"


def test_open_positions_at_limit_blocks_buy():
    engine = make_engine(live=True, max_open=3)
    for i in range(3):
        add_position(engine.state, f"Mint{i}")
    reason = engine._risk_block()
    assert "MAX_OPEN_POSITIONS" in reason, f"лимит не сработал: {reason!r}"


def test_limits_do_not_apply_in_demo():
    engine = make_engine(live=False, max_open=1)
    for i in range(10):
        add_position(engine.state, f"Mint{i}")
    assert engine._risk_block() == "", "в демо кап на позиции исказил бы статистику"


# --------------------------------------------------------------------------- #
# MAX_DAILY_LOSS_SOL
# --------------------------------------------------------------------------- #

def test_daily_loss_counts_only_today():
    engine = make_engine(live=True, max_loss="1")
    position = add_position(engine.state, "MintA")
    add_sell(position, "-5", ts=time.time() - 3 * 86400)   # позавчерашний слив
    assert engine.realized_pnl_today() == ZERO, "убыток прошлых суток не должен тянуться"
    assert engine._risk_block() == ""


def test_daily_loss_over_threshold_blocks_buy():
    engine = make_engine(live=True, max_loss="1")
    position = add_position(engine.state, "MintA")
    add_sell(position, "-0.6", ts=time.time())
    add_sell(position, "-0.5", ts=time.time())
    assert engine.realized_pnl_today() == Decimal("-1.1")
    reason = engine._risk_block()
    assert "MAX_DAILY_LOSS_SOL" in reason, f"лимит убытка не сработал: {reason!r}"


def test_daily_profit_never_blocks():
    engine = make_engine(live=True, max_loss="1")
    position = add_position(engine.state, "MintA")
    add_sell(position, "3", ts=time.time())
    assert engine._risk_block() == ""


def test_daily_loss_nets_wins_against_losses():
    """Лимит на дневной результат, а не на сумму убыточных сделок."""
    engine = make_engine(live=True, max_loss="1")
    position = add_position(engine.state, "MintA")
    add_sell(position, "-2", ts=time.time())
    add_sell(position, "1.5", ts=time.time())
    assert engine.realized_pnl_today() == Decimal("-0.5")
    assert engine._risk_block() == "", "-0.5 SOL при пороге -1 — торгуем дальше"


def test_daily_loss_includes_closed_positions():
    engine = make_engine(live=True, max_loss="1")
    position = add_position(engine.state, "MintA")
    add_sell(position, "-2", ts=time.time())
    engine.state.open_positions.pop("MintA")
    engine.state.closed_positions.append(position)
    reason = engine._risk_block()
    assert "MAX_DAILY_LOSS_SOL" in reason, "продажи закрытых позиций тоже считаются"


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
