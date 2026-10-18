"""Сводка, продублированная в текстовый файл.

Раз в STATS_INTERVAL_SEC бот печатает сводку в консоль. Консоль прокручивается
и теряется, поэтому та же сводка кладётся в SUMMARY_FILE — всегда свежая,
всегда целая. «Целая» тут важнее «свежей»: файл читают снаружи, и застать его
наполовину переписанным хуже, чем прочитать сводку пятиминутной давности.

Запуск: `pytest tests/` либо `python tests/test_summary_file.py` (без pytest).
"""

from __future__ import annotations

import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copytrade.config import Config  # noqa: E402
from copytrade.engine import Engine  # noqa: E402
from copytrade.executor import PaperExecutor  # noqa: E402
from copytrade.logging_setup import EventLogger  # noqa: E402
from copytrade.models import BotState, Position, PositionStatus  # noqa: E402
from copytrade.stats import build_summary, export_summary_txt, render_summary  # noqa: E402

W1 = "Wa11etOneAddress111111111111111111111111111"
SOL_USD = Decimal("200")
ESC = "\x1b"


def make_engine(tmp: Path, **overrides) -> Engine:
    params = {
        "rpc_http_url": "https://example.invalid",
        "target_wallets": [f"{W1}=кошелёк №1"],
        "summary_file": str(tmp / "summary.txt"),
        "stats_interval_sec": 300.0,
        "color": True,          # в консоль с цветом, в файл — без
    }
    params.update(overrides)
    cfg = Config(**params)
    cfg.validate()
    cfg.run_id = "2026-07-27_01-48-07"
    state = BotState(target_wallets=[W1], wallet_names={W1: "кошелёк №1"})
    state.open_positions["Mint1111111111111111111111111111111111111111"] = Position(
        mint="Mint1111111111111111111111111111111111111111",
        symbol="猴", decimals=6, opened_at=time.time() - 60,
        open_signature="sig1", entry_price_sol=Decimal("0.000001"),
        entry_price_usd=Decimal("0.0002"), sol_price_usd_at_entry=SOL_USD,
        cost_sol=Decimal("0.1"), tokens_initial=Decimal("100000"),
        tokens_remaining=Decimal("100000"), status=PositionStatus.OPEN,
        owner_wallet=W1, owner_name="кошелёк №1",
        target_tokens_at_entry=Decimal("3000000"), target_buy_usd=Decimal("600"),
    )
    engine = Engine(cfg, state, rpc=None, prices=None, executor=PaperExecutor(),
                    store=None, events=EventLogger("copytrade.test", None))
    return engine


def write(engine: Engine):
    summary = build_summary(engine.state, {}, SOL_USD)
    engine._write_summary_file(summary)
    return Path(engine.cfg.summary_file)


# --------------------------------------------------------------------------- #

def test_file_is_created_with_the_summary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = write(make_engine(Path(tmp)))
        text = path.read_text(encoding="utf-8")
        assert "COPYTRADE — СВОДКА" in text, text[:200]
        assert "--- Позиции ---" in text
        assert "猴" in text, "позиции должны попадать в файл"


def test_file_has_no_ansi_colors() -> None:
    """В консоль — с цветом, в файл — без: escape-коды в Блокноте нечитаемы."""
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(Path(tmp), color=True)
        text = write(engine).read_text(encoding="utf-8")
        assert ESC not in text, "в файл просочился ANSI"
        # ...при том что консольный рендер с цветом их содержит
        console = render_summary(build_summary(engine.state, {}, SOL_USD),
                                 engine.state, color=True)
        assert ESC in console, "цвет в консоли сломан — тест проверяет не то"


def test_header_names_the_interval_and_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        head = write(make_engine(Path(tmp))).read_text(encoding="utf-8").splitlines()[0]
        assert "300 c" in head, head
        assert "2026-07-27_01-48-07" in head, head


def test_file_is_overwritten_not_appended() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(Path(tmp))
        write(engine)
        path = write(engine)
        text = path.read_text(encoding="utf-8")
        assert text.count("COPYTRADE — СВОДКА") == 1, "сводка продублировалась"


def test_no_tmp_file_left_behind() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        write(make_engine(Path(tmp)))
        leftovers = list(Path(tmp).glob("*.tmp"))
        assert not leftovers, leftovers


def test_empty_path_disables_the_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(Path(tmp), summary_file="")
        engine._write_summary_file(build_summary(engine.state, {}, SOL_USD))
        assert not list(Path(tmp).iterdir()), "при пустом SUMMARY_FILE ничего не пишем"


def test_locked_file_does_not_break_the_loop() -> None:
    """Файл открыт в Excel — сводка пропускается, цикл живёт дальше."""
    with tempfile.TemporaryDirectory() as tmp:
        # Каталог вместо файла: replace на него гарантированно даёт OSError
        # на любой ОС, в отличие от блокировки, которая есть только в Windows.
        engine = make_engine(Path(tmp), summary_file=str(Path(tmp) / "busy"))
        Path(tmp, "busy").mkdir()
        engine._write_summary_file(build_summary(engine.state, {}, SOL_USD))
        assert not list(Path(tmp).glob("*.tmp")), "временный файл не убран за собой"


def test_export_writes_utf8_without_crlf_doubling() -> None:
    """\\r\\n в файле означал бы, что Python добавил перевод строки поверх \\n."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.txt"
        export_summary_txt("строка1\nстрока2\n", path)
        assert path.read_bytes() == "строка1\nстрока2\n".encode("utf-8")


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
