"""Solana copy-trading bot (demo / paper-trading first).

Пакет разбит по слоям:
    monitor   -> подписка на кошелёк-цель (WS + polling fallback)
    parser    -> tx JSON -> изменения балансов -> торговые события
    strategy  -> правила «копировать / пропустить», пропорциональные продажи
    executor  -> исполнение (paper сейчас, Jupiter swap — интерфейс на будущее)
    portfolio -> позиции и PnL
    storage   -> JSON-состояние, переживающее рестарт
    stats     -> сводная статистика, консольная таблица, CSV
"""

from __future__ import annotations

import decimal

# Денежная математика — только Decimal. 40 знаков с запасом покрывают
# токены с 9 decimals и ценами вида 1e-12 SOL за штуку.
decimal.getcontext().prec = 40

__version__ = "0.1.0"
__all__ = ["__version__"]
