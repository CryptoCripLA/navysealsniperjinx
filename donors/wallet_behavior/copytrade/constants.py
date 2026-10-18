"""Адреса и константы Solana, которые нужны парсеру и стратегии."""

from __future__ import annotations

import unicodedata
from decimal import Decimal

# Синтетический «минт» для нативного SOL — в token-балансах его нет,
# поэтому нужен отдельный ключ, чтобы не путать с wSOL.
NATIVE_SOL = "SOL"

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# Квотные валюты: то, чем платят за токен. Всё остальное считаем «торгуемым».
STABLE_MINTS: dict[str, int] = {
    USDC_MINT: 6,
    USDT_MINT: 6,
}

SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAMS = frozenset({SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM})

# jsonParsed отдаёт имя программы, а не только id
TOKEN_PROGRAM_NAMES = frozenset({"spl-token", "spl-token-2022"})

LAMPORTS_PER_SOL = 1_000_000_000
DEC_LAMPORTS_PER_SOL = Decimal(LAMPORTS_PER_SOL)

# Аренда ATA ≈ 0.00203928 SOL. Всё, что меньше порога ниже, — шум от
# создания/закрытия token-аккаунтов, а не торговля.
DEFAULT_SOL_DUST_LAMPORTS = 3_000_000  # 0.003 SOL

SYMBOL_OVERRIDES: dict[str, str] = {
    NATIVE_SOL: "SOL",
    WSOL_MINT: "wSOL",
    USDC_MINT: "USDC",
    USDT_MINT: "USDT",
}


def short_mint(mint: str, head: int = 4, tail: int = 4) -> str:
    """`EPjFWdd5...TDt1v` — для компактного вывода в таблицах и логах."""
    if len(mint) <= head + tail + 1:
        return mint
    return f"{mint[:head]}..{mint[-tail:]}"


# --------------------------------------------------------------------------- #
# ширина текста в терминале
# --------------------------------------------------------------------------- #
#
# Тикеры мемкоинов бывают какими угодно: 富贵, 月薪喵, 悲報, эмодзи. Иероглиф
# занимает ДВЕ ячейки терминала, а len() считает его за один — из-за этого
# колонки в таблице разъезжаются. Здесь считаем реальную ширину.

# Эмодзи, которые Unicode всё ещё помечает как узкие, хотя терминалы рисуют
# их в две ячейки.
_WIDE_RANGES = (
    (0x1F300, 0x1FAFF),  # символы, пиктограммы, эмодзи
    (0x1F000, 0x1F0FF),  # маджонг, карты
    (0x2600, 0x27BF),    # разное + дингбаты
    (0xFE0F, 0xFE0F),    # variation selector-16 рисуется вместе с базой
)


def char_width(char: str) -> int:
    code = ord(char)
    if code < 32 or 0x7F <= code < 0xA0:
        return 0
    if unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    for low, high in _WIDE_RANGES:
        if low <= code <= high:
            return 2 if code != 0xFE0F else 0
    return 1


def display_width(text: str) -> int:
    """Сколько ячеек терминала займёт строка. ANSI-последовательности — ноль."""
    width, in_escape = 0, False
    for char in text:
        if in_escape:
            if char == "m":
                in_escape = False
            continue
        if char == "\033":
            in_escape = True
            continue
        width += char_width(char)
    return width


def truncate_display(text: str, width: int) -> str:
    """Обрезка по ШИРИНЕ, а не по количеству символов."""
    if display_width(text) <= width:
        return text
    out, used = [], 0
    for char in text:
        step = char_width(char)
        if used + step > width:
            break
        out.append(char)
        used += step
    return "".join(out)


def pad_display(text: str, width: int, align: str = "<") -> str:
    """Дополнение пробелами до нужной ширины с учётом двухклеточных символов."""
    pad = max(0, width - display_width(text))
    if align == ">":
        return " " * pad + text
    if align == "^":
        left = pad // 2
        return " " * left + text + " " * (pad - left)
    return text + " " * pad
