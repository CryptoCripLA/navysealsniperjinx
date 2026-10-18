"""Загрузка конфига: config.json как база, переменные окружения поверх."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .constants import (
    DEFAULT_SOL_DUST_LAMPORTS,
    LAMPORTS_PER_SOL,
    USDC_MINT,
    USDT_MINT,
    short_mint,
)


class ConfigError(RuntimeError):
    pass


# Поля, которые бот вычисляет сам: не читаются ни из JSON, ни из окружения.
DERIVED = {"derived": True}
# Поля, которые ЗАПРЕЩЕНО класть в config.json (файл легко утекает в git).
SECRET = {"secret": True}


@dataclass(frozen=True, slots=True)
class WalletSpec:
    """Один отслеживаемый кошелёк: адрес + человекочитаемое имя."""

    address: str
    name: str
    index: int                      # 1-based, для логов «кошелёк №1»

    @property
    def label(self) -> str:
        return f"{self.name} ({short_mint(self.address)})"


@dataclass(slots=True)
class Config:
    # --- обязательное ---
    rpc_http_url: str = ""
    rpc_ws_url: str = ""
    # Список кошельков. Элемент: "АДРЕС" либо "АДРЕС=Имя".
    # В config.json допустим и объект {"address": ..., "name": ...}.
    target_wallets: list[str] = field(default_factory=list)
    # Старое односкошелёчное поле — оставлено ради совместимости со старым .env.
    target_wallet: str = ""

    # --- торговые правила ---
    min_buy_usd: Decimal = Decimal("50")
    position_size_sol: Decimal = Decimal("0.5")
    demo_mode: bool = True
    treat_transfer_as_sell: bool = False
    # Цель полностью вышла из токена (продажей ИЛИ выводом) -> закрываем и мы.
    close_on_target_balance_zero: bool = True
    # Разрешить новый вход, если позиция по этому минту уже была и закрыта.
    allow_reentry: bool = True
    # Минимальная доля остатка (в процентах), ради которой стоит продавать.
    # Цель может спамить продажами по 1% — каждая такая сделка стоит
    # PRIORITY_FEE + BRIBE, и на них уходит больше, чем спасает выход.
    # Пропущенные доли не теряются, а копятся: см. Position.pending_sell_remaining.
    # 0 = порога нет, зеркалим всё.
    min_sell_fraction_pct: Decimal = Decimal("10")

    # --- комиссии (значения по умолчанию — как в UI на скрине) ---
    slippage_bps: int = 2000                        # 20%
    priority_fee_sol: Decimal = Decimal("0.0001")   # валидаторам
    bribe_sol: Decimal = Decimal("0.0001")          # Jito-чаевые
    # В демо slippage — это ДОПУСК, а не гарантированный убыток. По умолчанию
    # его в paper-PnL не закладываем, иначе статистика врёт в минус.
    paper_apply_slippage: bool = False

    # --- живая торговля ---
    # Ключ читается ТОЛЬКО из окружения или из файла вне репозитория.
    wallet_private_key: str = field(default="", metadata=SECRET)
    wallet_private_key_file: str = ""
    # Опциональная сверка: если задан, должен совпасть с адресом из ключа.
    wallet_public_key: str = ""
    # Явное подтверждение: без него DEMO_MODE=false не стартует.
    live_i_understand_the_risk: bool = False
    max_open_positions: int = 5
    max_daily_loss_sol: Decimal = Decimal("1")
    # Не копировать покупки, случившиеся ДО старта бота. После простоя догон
    # приносит все сделки цели за время отсутствия: демо на них строит точную
    # статистику, а вживую это покупка того, что цель уже слила два часа назад.
    # Продаж не касается — из позиции выйти важнее, чем сэкономить.
    skip_buys_before_start: bool = True
    # Не копировать покупки по сигналам старше N секунд. Закрывает ту же дыру
    # при обрыве WS посреди работы, а не только на старте: опоздали на минуту
    # в мемкоине — вход уже бессмысленный, а комиссию платить придётся.
    # 0 — возраст не проверять. Обе ручки работают только в живом режиме.
    max_signal_age_sec: float = 60.0
    min_wallet_balance_sol: Decimal = Decimal("0.02")
    live_confirm_timeout_sec: float = 90.0
    live_skip_preflight: bool = True
    live_send_max_retries: int = 3
    # priority | jito | auto — см. README, комбинировать оба Jupiter не даёт.
    fee_mode: str = "priority"
    priority_level: str = "veryHigh"
    jito_endpoint: str = ""
    # Для перевода бюджета priority fee в цену за CU, когда fee_mode=jito.
    assumed_compute_units: int = 400_000
    jupiter_max_accounts: int = 64
    jupiter_restrict_intermediate: bool = True

    # --- мониторинг ---
    poll_interval_sec: float = 5.0
    # Поллер работает всегда как страховка от дыр в WS; пока WS жив,
    # интервал умножается на этот коэффициент.
    poll_idle_multiplier: float = 6.0
    ws_enabled: bool = True
    ws_reconnect_min_sec: float = 1.0
    ws_reconnect_max_sec: float = 60.0
    ws_ping_interval_sec: float = 20.0
    commitment: str = "confirmed"
    # Сколько последних подписей разобрать при самом первом запуске
    # (когда state пустой). 0 = стартуем «с нуля», историю не копируем.
    startup_backfill_limit: int = 0
    signature_fetch_limit: int = 1000
    # Буфер переупорядочивания: WS и поллер могут отдать сигнатуры вразнобой,
    # поэтому копим их и обрабатываем по возрастанию слота.
    reorder_window_ms: int = 1200

    # --- RPC ---
    rpc_timeout_sec: float = 25.0
    rpc_max_retries: int = 4
    rpc_max_concurrency: int = 6
    rpc_min_interval_ms: int = 0
    tx_fetch_retries: int = 5
    tx_fetch_retry_delay_sec: float = 1.0

    # --- цены ---
    jupiter_base_url: str = "https://api.jup.ag"
    jupiter_api_key: str = field(default="", metadata=SECRET)
    # keyless-тариф Jupiter = 0.5 rps, поэтому по умолчанию бережём лимит
    jupiter_max_rps: float = 0.4
    sol_price_ttl_sec: float = 45.0
    token_price_ttl_sec: float = 30.0
    price_http_timeout_sec: float = 12.0
    fallback_sol_price_usd: Decimal = Decimal("0")
    # Как часто переоценивать открытые позиции для нереализованного PnL
    mark_refresh_interval_sec: float = 60.0
    symbol_lookup_timeout_sec: float = 6.0

    # --- пороги распознавания ---
    sol_dust_lamports: int = DEFAULT_SOL_DUST_LAMPORTS
    stable_dust_raw: int = 10_000  # 0.01 USDC/USDT
    quote_mints: list[str] = field(default_factory=lambda: [USDC_MINT, USDT_MINT])

    # --- хранение / вывод ---
    data_dir: str = "data"
    state_file: str = "data/state.json"
    log_dir: str = "data/logs"
    # Пусто -> имя генерируется по времени старта: copytrade_ДАТА_ВРЕМЯ.log
    log_file: str = ""
    events_file: str = ""
    log_keep_runs: int = 30
    positions_csv: str = "data/positions.csv"
    trades_csv: str = "data/trades.csv"
    wallets_csv: str = "data/wallets.csv"
    # Последняя сводка в виде текста: то же, что печатается в консоль раз в
    # STATS_INTERVAL_SEC, но без ANSI-цвета и всегда только свежая. Перезапись,
    # а не дописывание: историю и так несут CSV, обновляемые в тот же момент.
    summary_file: str = "data/summary.txt"
    log_level: str = "INFO"
    color: bool = True
    stats_interval_sec: float = 300.0
    state_flush_interval_sec: float = 20.0
    max_processed_signatures: int = 5000

    # --- вычисляемое ---
    wallets: list[WalletSpec] = field(default_factory=list, metadata=DERIVED)
    run_id: str = field(default="", metadata=DERIVED)

    # ---------- производные ----------

    @property
    def all_quote_mints(self) -> frozenset[str]:
        return frozenset(self.quote_mints)

    @property
    def wallet_addresses(self) -> list[str]:
        return [w.address for w in self.wallets]

    @property
    def wallet_names(self) -> dict[str, str]:
        return {w.address: w.name for w in self.wallets}

    def wallet_name(self, address: str) -> str:
        for spec in self.wallets:
            if spec.address == address:
                return spec.name
        return short_mint(address)

    @property
    def priority_fee_lamports(self) -> int:
        return int(self.priority_fee_sol * Decimal(LAMPORTS_PER_SOL))

    @property
    def bribe_lamports(self) -> int:
        return int(self.bribe_sol * Decimal(LAMPORTS_PER_SOL))

    @property
    def fixed_fee_sol(self) -> Decimal:
        """Комиссии, которые платятся при любом исходе сделки."""
        return self.priority_fee_sol + self.bribe_sol

    @property
    def min_sell_fraction(self) -> Decimal:
        """Порог мелкой продажи как доля (0.10), а не как проценты."""
        return self.min_sell_fraction_pct / Decimal(100)

    @property
    def paper_slippage_bps(self) -> int:
        return self.slippage_bps if self.paper_apply_slippage else 0

    def resolve_paths(self, root: Path) -> None:
        """Относительные пути в конфиге считаем от каталога проекта."""
        for attr in ("data_dir", "state_file", "log_dir",
                     "positions_csv", "trades_csv", "wallets_csv", "summary_file"):
            value = Path(getattr(self, attr))
            if not value.is_absolute():
                value = root / value
            setattr(self, attr, str(value))
        for attr in ("log_file", "events_file"):
            raw = getattr(self, attr)
            if raw:
                value = Path(raw)
                if not value.is_absolute():
                    value = root / value
                setattr(self, attr, str(value))
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)

    def start_run(self, started_at: float | None = None) -> str:
        """Зафиксировать идентификатор запуска и имена файлов логов.

        Каждый запуск пишет в свой файл: бот работает сутками, и мешать прогоны
        в один лог — значит потерять границу «до перезапуска / после».
        """
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(started_at or time.time()))
        self.run_id = stamp
        log_dir = Path(self.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        if not self.log_file:
            self.log_file = str(log_dir / f"copytrade_{stamp}.log")
        if not self.events_file:
            self.events_file = str(log_dir / f"events_{stamp}.jsonl")
        # Указатель на текущий прогон: симлинки на Windows требуют прав админа.
        try:
            (log_dir / "latest.txt").write_text(
                f"{self.log_file}\n{self.events_file}\n", encoding="utf-8"
            )
        except OSError:
            pass
        return stamp

    def prune_old_logs(self) -> int:
        """Оставить только N последних прогонов. Возвращает число удалённых файлов."""
        if self.log_keep_runs <= 0:
            return 0
        directory = Path(self.log_dir)
        if not directory.is_dir():
            return 0
        current = {self.log_file, self.events_file}
        removed = 0
        for prefix, suffix in (("copytrade_", ".log"), ("events_", ".jsonl")):
            # Имя начинается с даты в ISO-порядке, поэтому сортировка по имени
            # совпадает с сортировкой по времени.
            files = sorted(p for p in directory.glob(f"{prefix}*{suffix}") if p.is_file())
            if len(files) <= self.log_keep_runs:
                continue
            for path in files[: len(files) - self.log_keep_runs]:
                if str(path) in current:
                    continue
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    # ---------- валидация ----------

    def validate(self) -> None:
        self.wallets = _build_wallets(self.target_wallets, self.target_wallet)
        if not self.wallets:
            raise ConfigError(
                "Не задан ни один кошелёк. Заполни TARGET_WALLETS "
                "(через запятую, можно 'АДРЕС=Имя') или TARGET_WALLET."
            )
        if not self.rpc_http_url:
            raise ConfigError(
                "Не задан RPC_HTTP_URL. Заполни config.json или .env "
                "(см. config.example.json)."
            )
        if not self.rpc_http_url.startswith(("http://", "https://")):
            raise ConfigError(f"RPC_HTTP_URL должен быть http(s): {self.rpc_http_url!r}")
        if self.ws_enabled and self.rpc_ws_url and not self.rpc_ws_url.startswith(("ws://", "wss://")):
            raise ConfigError(f"RPC_WS_URL должен быть ws(s): {self.rpc_ws_url!r}")
        if not self.rpc_ws_url:
            self.ws_enabled = False

        if self.position_size_sol <= 0:
            raise ConfigError("POSITION_SIZE_SOL должен быть > 0")
        if self.min_buy_usd < 0:
            raise ConfigError("MIN_BUY_USD не может быть отрицательным")
        if self.poll_interval_sec < 0.5:
            raise ConfigError("POLL_INTERVAL_SEC слишком мал (минимум 0.5)")
        if not 0 <= self.slippage_bps <= 10_000:
            raise ConfigError(f"SLIPPAGE_BPS вне диапазона 0..10000: {self.slippage_bps}")
        if self.priority_fee_sol < 0 or self.bribe_sol < 0:
            raise ConfigError("PRIORITY_FEE_SOL и BRIBE_SOL не могут быть отрицательными")
        if self.fixed_fee_sol >= self.position_size_sol:
            raise ConfigError(
                f"Комиссии ({self.fixed_fee_sol} SOL) не меньше размера позиции "
                f"({self.position_size_sol} SOL) — торговать нечем."
            )
        if not 0 <= self.min_sell_fraction_pct < 100:
            raise ConfigError(
                f"MIN_SELL_FRACTION_PCT должен быть в диапазоне 0..99.99, "
                f"получено {self.min_sell_fraction_pct}. Порог 100% означал бы, "
                "что зеркалится только полный выход."
            )
        if self.fee_mode not in ("priority", "jito", "auto"):
            raise ConfigError(f"FEE_MODE должен быть priority|jito|auto, получено {self.fee_mode!r}")

        if not self.demo_mode:
            self._validate_live()

    def _validate_live(self) -> None:
        """Живой режим: деньги настоящие, поэтому предохранителей несколько."""
        if not self.live_i_understand_the_risk:
            raise ConfigError(
                "DEMO_MODE=false требует LIVE_I_UNDERSTAND_THE_RISK=true. "
                "Бот будет тратить НАСТОЯЩИЕ SOL с твоего кошелька. "
                "Сначала прогони демо и посмотри статистику."
            )
        if not self.wallet_private_key and not self.wallet_private_key_file:
            raise ConfigError(
                "Живой режим требует ключ: задай переменную окружения "
                "WALLET_PRIVATE_KEY или WALLET_PRIVATE_KEY_FILE (путь к файлу "
                "вне репозитория). В config.json ключ класть НЕЛЬЗЯ."
            )
        if self.wallet_private_key_file and not Path(self.wallet_private_key_file).is_file():
            raise ConfigError(f"WALLET_PRIVATE_KEY_FILE не найден: {self.wallet_private_key_file}")
        if self.max_open_positions <= 0:
            raise ConfigError("MAX_OPEN_POSITIONS должен быть > 0 в живом режиме")
        if self.max_daily_loss_sol <= 0:
            raise ConfigError("MAX_DAILY_LOSS_SOL должен быть > 0 в живом режиме")
        if self.max_signal_age_sec < 0:
            raise ConfigError("MAX_SIGNAL_AGE_SEC не может быть отрицательным (0 — не проверять)")
        if not self.skip_buys_before_start and self.max_signal_age_sec <= 0:
            raise ConfigError(
                "SKIP_BUYS_BEFORE_START=false вместе с MAX_SIGNAL_AGE_SEC=0 означает, "
                "что после перезапуска бот на реальные деньги скупит всю историю "
                "цели за время простоя. Включи хотя бы одну из двух защит."
            )
        if self.fee_mode == "jito" and not self.jito_endpoint:
            raise ConfigError(
                "FEE_MODE=jito требует JITO_ENDPOINT: отправка транзакции с "
                "чаевыми через обычный RPC просто сожжёт BRIBE_SOL впустую."
            )

    def live_warnings(self) -> list[str]:
        """Некритичные, но важные предупреждения — печатаем при старте."""
        notes: list[str] = []
        if self.demo_mode:
            return notes
        if self.fee_mode == "priority" and self.bribe_sol > 0:
            notes.append(
                f"FEE_MODE=priority: BRIBE_SOL={self.bribe_sol} НЕ отправляется — "
                "Jupiter не даёт передать priority fee и Jito-чаевые одновременно. "
                "Нужны оба — включи FEE_MODE=jito и задай JITO_ENDPOINT."
            )
        if self.slippage_bps >= 1000:
            notes.append(
                f"SLIPPAGE_BPS={self.slippage_bps} ({self.slippage_bps / 100:.1f}%) — "
                "это допуск, а не цель. На неликвидных мемкоинах разницу заберут "
                "сэндвич-боты."
            )
        if self.live_skip_preflight:
            notes.append(
                "LIVE_SKIP_PREFLIGHT=true: транзакция уйдёт без предварительной "
                "симуляции — быстрее, но заведомо провальные свапы всё равно "
                "оплатят комиссию."
            )
        return notes


# --------------------------------------------------------------------------- #
# кошельки
# --------------------------------------------------------------------------- #

def _looks_like_address(value: str) -> bool:
    return 32 <= len(value) <= 44 and value.isalnum()


# Разделители списка. Пробел не годится: имя кошелька бывает из двух слов.
_LIST_SEPARATORS = (",", ";", "\n", "\r")

# Прогон из 32+ буквенно-цифровых символов внутри имени = там спрятан адрес.
_HIDDEN_ADDRESS = re.compile(r"[A-Za-z0-9]{32,}")


def _split_list(value: str) -> list[str]:
    """'A=имя, B=имя2' -> ['A=имя', 'B=имя2']."""
    parts = [str(value)]
    for separator in _LIST_SEPARATORS:
        parts = [chunk for part in parts for chunk in part.split(separator)]
    return [part.strip() for part in parts if part.strip()]


def _split_entry(text: str) -> tuple[str, str]:
    """'АДРЕС=Имя' / 'АДРЕС|Имя' / 'АДРЕС' -> (адрес, имя)."""
    separator = "=" if "=" in text else ("|" if "|" in text else "")
    if not separator:
        return text, ""
    address, _, name = text.partition(separator)
    return address.strip(), name.strip()


def _check_wallet_name(address: str, name: str) -> None:
    """Упасть, если в «имени» кошелька спрятан ещё один кошелёк.

    Так выглядит вставка всего списка в одну переменную с разделителем,
    который мы не поддерживаем: первый адрес разбирается, остальные молча
    уезжают в имя, и бот следит за одним кошельком вместо четырёх — без
    единой ошибки в логе. Лучше не стартовать, чем торговать не тем.
    """
    if not _HIDDEN_ADDRESS.search(name) and not any(ch in name for ch in "=|"):
        return
    raise ConfigError(
        f"Кошелёк {short_mint(address)}: имя {name!r} похоже на слипшийся "
        "список — внутри ещё один адрес. Разделяйте кошельки запятой:\n"
        "  TARGET_WALLETS=АДРЕС1=Имя1,АДРЕС2=Имя2"
    )


def _build_wallets(entries: list[str], legacy: str) -> list[WalletSpec]:
    raw = [item for entry in entries for item in _split_list(str(entry))]
    # Старое односкошелёчное поле — тоже разбираем как список: в него легко
    # вставить все адреса разом по образцу TARGET_WALLETS.
    known = {_split_entry(item)[0] for item in raw}
    raw.extend(item for item in _split_list(legacy)
               if _split_entry(item)[0] not in known)

    specs: list[WalletSpec] = []
    seen: set[str] = set()
    for item in raw:
        address, name = _split_entry(item)
        _check_wallet_name(address, name)
        if not _looks_like_address(address):
            raise ConfigError(
                f"{address!r} не похож на base58-адрес Solana "
                "(ожидается 32–44 буквенно-цифровых символа). "
                "Формат элемента списка: 'АДРЕС' или 'АДРЕС=Имя'."
            )
        if address in seen:
            raise ConfigError(f"Кошелёк {short_mint(address)} указан в списке дважды")
        seen.add(address)
        index = len(specs) + 1
        specs.append(WalletSpec(address=address, name=name or f"кошелёк №{index}", index=index))
    return specs


# --------------------------------------------------------------------------- #
# приведение типов
# --------------------------------------------------------------------------- #

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{name}: ожидался bool, получено {value!r}")


def _as_decimal(value: Any, name: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"{name}: ожидалось число, получено {value!r}") from exc


def _as_int(value: Any, name: str) -> int:
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидалось целое, получено {value!r}") from exc


def _as_float(value: Any, name: str) -> float:
    try:
        return float(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидалось число, получено {value!r}") from exc


def _as_str_list(value: Any, name: str) -> list[str]:
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            # В JSON кошелёк можно описать объектом: {"address": ..., "name": ...}
            if isinstance(item, dict):
                address = str(item.get("address") or item.get("wallet") or "").strip()
                label = str(item.get("name") or item.get("label") or "").strip()
                if not address:
                    raise ConfigError(f"{name}: в объекте нет поля 'address' — {item!r}")
                out.append(f"{address}={label}" if label else address)
            else:
                # Строка внутри списка тоже может нести несколько записей.
                out.extend(_split_list(str(item)))
        return out
    return _split_list(str(value))


_COERCERS = {
    bool: _as_bool,
    int: _as_int,
    float: _as_float,
    Decimal: _as_decimal,
    str: lambda v, _n: str(v).strip(),
    list: _as_str_list,
}


def _field_types() -> dict[str, Any]:
    """Имя поля -> тип для приведения. Вычисляемые поля пропускаем."""
    types: dict[str, Any] = {}
    for info in dataclass_fields(Config):
        if info.metadata.get("derived"):
            continue
        annotation = info.type if isinstance(info.type, str) else str(info.type)
        if annotation.startswith("Decimal"):
            types[info.name] = Decimal
        elif annotation.startswith("bool"):
            types[info.name] = bool
        elif annotation.startswith("int"):
            types[info.name] = int
        elif annotation.startswith("float"):
            types[info.name] = float
        elif annotation.startswith("list"):
            types[info.name] = list
        else:
            types[info.name] = str
    return types


def _secret_fields() -> set[str]:
    return {f.name for f in dataclass_fields(Config) if f.metadata.get("secret")}


def _load_dotenv(path: Path) -> None:
    """python-dotenv если есть, иначе минимальный парсер — без лишней зависимости."""
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Реальное окружение всегда важнее файла
            os.environ.setdefault(key, value)
    else:
        load_dotenv(path, override=False)


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    env_path: str | os.PathLike[str] | None = None,
    root: str | os.PathLike[str] | None = None,
) -> Config:
    """config.json -> переменные окружения -> валидация.

    Переменная окружения перекрывает значение из JSON. Имя переменной —
    UPPER_SNAKE от имени поля (`min_buy_usd` -> `MIN_BUY_USD`).
    """
    project_root = Path(root) if root else Path.cwd()
    cfg_path = Path(config_path) if config_path else project_root / "config.json"
    dotenv_path = Path(env_path) if env_path else project_root / ".env"

    _load_dotenv(dotenv_path)

    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{cfg_path}: невалидный JSON — {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{cfg_path}: ожидался объект верхнего уровня")

    types = _field_types()
    # В JSON допускаем и UPPER_CASE, и lower_case ключи
    normalized = {str(k).lower(): v for k, v in raw.items()}
    unknown = set(normalized) - set(types)
    if unknown:
        raise ConfigError(
            f"{cfg_path}: неизвестные ключи: {', '.join(sorted(unknown))}"
        )

    # Секрет в config.json — прямая дорога к утечке ключа в git. Отказываемся.
    leaked = sorted(set(normalized) & _secret_fields())
    if leaked:
        raise ConfigError(
            f"{cfg_path}: {', '.join(k.upper() for k in leaked)} нельзя хранить в "
            "config.json — этот файл слишком легко закоммитить. Используй "
            "переменную окружения или .env (он в .gitignore)."
        )

    values: dict[str, Any] = {}
    for name, typ in types.items():
        env_value = os.environ.get(name.upper())
        source = env_value if env_value not in (None, "") else normalized.get(name)
        if source is None:
            continue
        values[name] = _COERCERS[typ](source, name.upper())

    cfg = Config(**values)
    cfg.resolve_paths(project_root)
    cfg.validate()
    return cfg
