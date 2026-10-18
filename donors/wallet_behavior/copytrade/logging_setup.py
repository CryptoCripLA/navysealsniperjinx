"""Логи: цветная консоль + плоский файл + машиночитаемый JSONL событий."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

try:  # colorama нужен, чтобы ANSI работал в cmd.exe / PowerShell
    import colorama

    if hasattr(colorama, "just_fix_windows_console"):
        colorama.just_fix_windows_console()
    else:  # colorama < 0.4.6
        colorama.init()
    _COLORAMA = True
except ImportError:  # pragma: no cover - зависит от окружения
    _COLORAMA = False


RESET = "\033[0m"
_C = {
    "grey": "\033[90m",
    "red": "\033[31m",
    "bred": "\033[1;31m",
    "green": "\033[32m",
    "bgreen": "\033[1;32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "bblue": "\033[1;34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "bwhite": "\033[1;37m",
}

LEVEL_COLORS = {
    logging.DEBUG: _C["grey"],
    logging.INFO: _C["white"],
    logging.WARNING: _C["yellow"],
    logging.ERROR: _C["red"],
    logging.CRITICAL: _C["bred"],
}

# Событие -> цвет. Имена стабильны и не переводятся: по ним грепают лог.
EVENT_COLORS = {
    "BUY_DETECTED": _C["cyan"],
    "BUY_COPIED": _C["bgreen"],
    "BUY_SKIPPED": _C["yellow"],
    "SELL_DETECTED": _C["magenta"],
    "SELL_COPIED": _C["bgreen"],
    "SELL_SKIPPED": _C["yellow"],
    "POSITION_CLOSED": _C["bblue"],
    "TRANSFER_OUT": _C["yellow"],
    "TRANSFER_IN": _C["grey"],
    "TX_SEEN": _C["grey"],
    "TX_IGNORED": _C["grey"],
    "WS_UP": _C["green"],
    "WS_DOWN": _C["red"],
    "STARTUP": _C["bwhite"],
    "STATS": _C["bwhite"],
    "ERROR": _C["bred"],
}

_colors_enabled = True


def colorize(text: str, color: str) -> str:
    if not _colors_enabled:
        return text
    return f"{_C.get(color, '')}{text}{RESET}"


class _ConsoleFormatter(logging.Formatter):
    def __init__(self, use_color: bool) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime(self.datefmt, time.localtime(record.created))
        event = getattr(record, "event", None)
        message = record.getMessage()

        if event:
            tag = f"[{event}]"
            if self.use_color:
                tag = f"{EVENT_COLORS.get(event, '')}{tag}{RESET}"
            body = f"{tag} {message}"
        else:
            level = record.levelname
            if self.use_color:
                level = f"{LEVEL_COLORS.get(record.levelno, '')}{level}{RESET}"
            body = f"{level} {message}"

        prefix = f"{_C['grey']}{ts}{RESET}" if self.use_color else ts
        line = f"{prefix} {body}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class _FileFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", None)
        if event:
            record.msg = f"[{event}] {record.getMessage()}"
            record.args = ()
        return super().format(record)


def setup_logging(
    level: str = "INFO",
    log_file: str | Path | None = None,
    use_color: bool = True,
) -> None:
    global _colors_enabled

    # Windows: при перенаправлении вывода Python берёт кодировку локали (cp1251),
    # и любой символ вне неё роняет print сводки. Кодировку не меняем, чтобы не
    # ломать кириллицу в legacy-консоли, — только запрещаем падать.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass

    _colors_enabled = bool(use_color and (sys.stdout.isatty() or _COLORAMA))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(_ConsoleFormatter(_colors_enabled))
    root.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(_FileFormatter())
        root.addHandler(file_handler)

    # HTTP-библиотеки очень болтливы на DEBUG
    for noisy in ("aiohttp", "aiohttp.client", "websockets", "websockets.client", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class EventLogger:
    """Тонкая обёртка: одно событие -> строка в лог + строка в JSONL."""

    def __init__(self, name: str, events_file: str | Path | None = None) -> None:
        self._log = logging.getLogger(name)
        self._events_path = Path(events_file) if events_file else None
        if self._events_path:
            self._events_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        event: str,
        message: str,
        *,
        level: int = logging.INFO,
        **fields: Any,
    ) -> None:
        self._log.log(level, message, extra={"event": event})
        if not self._events_path:
            return
        payload: dict[str, Any] = {"ts": time.time(), "event": event, "message": message}
        payload.update({k: _jsonable(v) for k, v in fields.items()})
        try:
            with self._events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as exc:  # не роняем бота из-за проблем с диском
            self._log.warning("Не удалось записать events.jsonl: %s", exc)

    # шорткаты
    def debug(self, msg: str, *args: Any) -> None:
        self._log.debug(msg, *args)

    def info(self, msg: str, *args: Any) -> None:
        self._log.info(msg, *args)

    def warning(self, msg: str, *args: Any) -> None:
        self._log.warning(msg, *args)

    def error(self, msg: str, *args: Any, exc_info: bool = False) -> None:
        self._log.error(msg, *args, exc_info=exc_info)


def _jsonable(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return value.value
    return value
