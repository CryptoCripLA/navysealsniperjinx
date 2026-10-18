"""Доменные модели: события кошелька, позиции, состояние бота."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable

from .constants import DEC_LAMPORTS_PER_SOL, NATIVE_SOL, short_mint

ZERO = Decimal(0)
ONE = Decimal(1)


def _d(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _s(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


# --------------------------------------------------------------------------- #
# события
# --------------------------------------------------------------------------- #

class EventKind(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    TRANSFER_OUT = "TRANSFER_OUT"
    TRANSFER_IN = "TRANSFER_IN"
    WRAP_UNWRAP = "WRAP_UNWRAP"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True, frozen=True)
class TokenBalanceDelta:
    """Изменение баланса одного минта у кошелька-цели внутри одной транзакции.

    `pre_raw`/`post_raw` — суммарные балансы по всем token-аккаунтам с этим
    минтом, которыми владеет кошелёк. Это ground truth из самой транзакции,
    а не накопленная нами дельта — поэтому переживает любые пропуски.
    """

    mint: str
    decimals: int
    pre_raw: int
    post_raw: int

    @property
    def delta_raw(self) -> int:
        return self.post_raw - self.pre_raw

    @property
    def scale(self) -> Decimal:
        return Decimal(10) ** self.decimals

    def to_ui(self, raw: int) -> Decimal:
        return Decimal(raw) / self.scale

    @property
    def pre_ui(self) -> Decimal:
        return self.to_ui(self.pre_raw)

    @property
    def post_ui(self) -> Decimal:
        return self.to_ui(self.post_raw)

    @property
    def delta_ui(self) -> Decimal:
        return self.to_ui(self.delta_raw)

    @property
    def sold_fraction(self) -> Decimal:
        """Какую долю от ТЕКУЩЕГО остатка цель продала в этой транзакции."""
        if self.pre_raw <= 0 or self.delta_raw >= 0:
            return ZERO
        sold = min(-self.delta_raw, self.pre_raw)
        return Decimal(sold) / Decimal(self.pre_raw)


@dataclass(slots=True)
class WalletActivity:
    """Сырой результат разбора транзакции — только цифры, без интерпретации."""

    wallet: str
    signature: str
    slot: int
    block_time: int | None
    succeeded: bool
    err: Any
    fee_lamports: int
    native_delta_lamports: int          # нативный SOL, комиссия исключена
    wsol_delta_lamports: int            # wSOL как token-баланс
    stable_deltas: dict[str, TokenBalanceDelta] = field(default_factory=dict)
    token_deltas: dict[str, TokenBalanceDelta] = field(default_factory=dict)
    transfer_out_mints: set[str] = field(default_factory=set)
    transfer_in_mints: set[str] = field(default_factory=set)
    program_ids: set[str] = field(default_factory=set)

    @property
    def sol_delta_lamports(self) -> int:
        """SOL + wSOL. Сумма нейтрализует wrap/unwrap: они гасят друг друга."""
        return self.native_delta_lamports + self.wsol_delta_lamports

    @property
    def sol_delta(self) -> Decimal:
        return Decimal(self.sol_delta_lamports) / DEC_LAMPORTS_PER_SOL

    @property
    def touched_wallet(self) -> bool:
        return bool(
            self.token_deltas
            or self.stable_deltas
            or self.native_delta_lamports
            or self.wsol_delta_lamports
        )


@dataclass(slots=True)
class TradeEvent:
    """Интерпретированное событие по одному минту."""

    kind: EventKind
    signature: str
    slot: int
    block_time: int | None
    mint: str
    decimals: int
    token_pre_raw: int
    token_post_raw: int
    token_amount_ui: Decimal            # |изменение| в человеческих единицах
    quote_mint: str | None              # NATIVE_SOL / USDC / USDT / None
    quote_amount_ui: Decimal            # сколько отдал/получил, всегда >= 0
    price_in_quote: Decimal | None      # цена 1 токена в quote_mint
    exact_price: bool                   # False -> цену придётся брать из API
    note: str = ""
    wallet: str = ""                    # чей это кошелёк из списка отслеживаемых

    @property
    def order_key(self) -> tuple[int, int, str]:
        """Позиция события в блокчейне — по ней решаем, кто купил первым.

        Слот определяет порядок на цепочке; blockTime — вторичен (секундная
        гранулярность), подпись добавлена лишь для детерминизма при равенстве.
        Порядок ОБРАБОТКИ ботом здесь не участвует намеренно: после реконнекта
        или догона пропущенного он не совпадает с порядком в цепочке.
        """
        return (self.slot, self.block_time or 0, self.signature)

    @property
    def sold_fraction(self) -> Decimal:
        if self.token_pre_raw <= 0 or self.token_post_raw >= self.token_pre_raw:
            return ZERO
        sold = self.token_pre_raw - self.token_post_raw
        return Decimal(sold) / Decimal(self.token_pre_raw)

    @property
    def target_closed_out(self) -> bool:
        return self.token_post_raw <= 0

    def describe(self) -> str:
        quote = "?" if self.quote_mint is None else (
            "SOL" if self.quote_mint == NATIVE_SOL else short_mint(self.quote_mint)
        )
        return (
            f"{self.kind.value} {short_mint(self.mint)} "
            f"amount={self.token_amount_ui:.6f} quote={self.quote_amount_ui:.6f} {quote}"
        )


# --------------------------------------------------------------------------- #
# позиции
# --------------------------------------------------------------------------- #

class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class CloseReason(str, Enum):
    TARGET_SOLD_ALL = "TARGET_SOLD_ALL"
    TARGET_BALANCE_ZERO = "TARGET_BALANCE_ZERO"
    TRANSFER_OUT = "TRANSFER_OUT"
    DUST = "DUST"
    MANUAL = "MANUAL"


@dataclass(slots=True)
class PartialSell:
    ts: float
    signature: str
    fraction: Decimal                   # доля НАШЕГО остатка, которую продали
    tokens_sold: Decimal
    price_sol: Decimal
    sol_received: Decimal
    sol_price_usd: Decimal
    realized_pnl_sol: Decimal
    realized_pnl_usd: Decimal
    price_source: str = "tx"
    target_pre_raw: int = 0
    target_post_raw: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "signature": self.signature,
            "fraction": _s(self.fraction),
            "tokens_sold": _s(self.tokens_sold),
            "price_sol": _s(self.price_sol),
            "sol_received": _s(self.sol_received),
            "sol_price_usd": _s(self.sol_price_usd),
            "realized_pnl_sol": _s(self.realized_pnl_sol),
            "realized_pnl_usd": _s(self.realized_pnl_usd),
            "price_source": self.price_source,
            "target_pre_raw": self.target_pre_raw,
            "target_post_raw": self.target_post_raw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PartialSell":
        return cls(
            ts=float(data["ts"]),
            signature=data["signature"],
            fraction=_d(data["fraction"]),
            tokens_sold=_d(data["tokens_sold"]),
            price_sol=_d(data["price_sol"]),
            sol_received=_d(data["sol_received"]),
            sol_price_usd=_d(data["sol_price_usd"]),
            realized_pnl_sol=_d(data["realized_pnl_sol"]),
            realized_pnl_usd=_d(data["realized_pnl_usd"]),
            price_source=data.get("price_source", "tx"),
            target_pre_raw=int(data.get("target_pre_raw", 0)),
            target_post_raw=int(data.get("target_post_raw", 0)),
        )


@dataclass(slots=True)
class Position:
    mint: str
    symbol: str
    decimals: int
    opened_at: float
    open_signature: str
    entry_price_sol: Decimal            # SOL за 1 токен
    entry_price_usd: Decimal
    sol_price_usd_at_entry: Decimal
    cost_sol: Decimal                   # сколько SOL мы «вложили» = POSITION_SIZE_SOL
    tokens_initial: Decimal
    tokens_remaining: Decimal
    target_tokens_at_entry: Decimal
    target_buy_usd: Decimal
    target_tokens_last_seen: Decimal = ZERO
    realized_pnl_sol: Decimal = ZERO
    realized_pnl_usd: Decimal = ZERO
    sol_returned: Decimal = ZERO        # выручка от всех частичных продаж
    sells: list[PartialSell] = field(default_factory=list)
    status: PositionStatus = PositionStatus.OPEN
    closed_at: float | None = None
    close_reason: CloseReason | None = None
    last_price_sol: Decimal | None = None
    last_price_ts: float = 0.0
    # Ведущий: кошелёк, чья покупка открыла позицию. Только его продажи
    # зеркалим, остальные по этому минту игнорируем до закрытия позиции.
    owner_wallet: str = ""
    owner_name: str = ""
    owner_slot: int = 0
    owner_block_time: int | None = None
    # Произведение (1 - доля) по продажам ведущего, которые мы пропустили как
    # слишком мелкие. 1.0 = ничего не накоплено. Нужно, чтобы спам продажами по
    # 1% не рассинхронизировал наш остаток с остатком ведущего: доли копятся и
    # исполняются одной сделкой, когда суммарно перевалят за порог.
    pending_sell_remaining: Decimal = ONE
    skipped_dust_sells: int = 0

    # ---------- производные ----------

    @property
    def is_open(self) -> bool:
        return self.status is PositionStatus.OPEN

    @property
    def owner_key(self) -> tuple[int, int, str]:
        """Ключ сравнения «кто раньше» — той же формы, что TradeEvent.order_key."""
        return (self.owner_slot, self.owner_block_time or 0, self.open_signature)

    def owned_by(self, wallet: str) -> bool:
        # Позиции из состояния до появления мульти-кошельков ведущего не имеют:
        # считаем их «ничьими», чтобы старый state не сломал зеркалирование.
        return not self.owner_wallet or self.owner_wallet == wallet

    @property
    def cost_basis_remaining_sol(self) -> Decimal:
        """Себестоимость непроданного остатка (средняя цена входа)."""
        return self.tokens_remaining * self.entry_price_sol

    @property
    def sold_fraction_total(self) -> Decimal:
        if self.tokens_initial <= 0:
            return ZERO
        return (self.tokens_initial - self.tokens_remaining) / self.tokens_initial

    def unrealized_pnl_sol(self, price_sol: Decimal | None) -> Decimal:
        if price_sol is None or self.tokens_remaining <= 0:
            return ZERO
        return self.tokens_remaining * price_sol - self.cost_basis_remaining_sol

    def total_pnl_sol(self, price_sol: Decimal | None) -> Decimal:
        return self.realized_pnl_sol + self.unrealized_pnl_sol(price_sol)

    def total_pnl_usd(self, price_sol: Decimal | None, sol_price_usd: Decimal) -> Decimal:
        return self.realized_pnl_usd + self.unrealized_pnl_sol(price_sol) * sol_price_usd

    def pnl_pct(self, price_sol: Decimal | None) -> Decimal:
        if self.cost_sol <= 0:
            return ZERO
        return self.total_pnl_sol(price_sol) / self.cost_sol * Decimal(100)

    def hold_seconds(self, now: float | None = None) -> float:
        end = self.closed_at if self.closed_at else (now if now is not None else time.time())
        return max(0.0, end - self.opened_at)

    def label(self) -> str:
        return self.symbol or short_mint(self.mint)

    # ---------- сериализация ----------

    def to_dict(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "decimals": self.decimals,
            "opened_at": self.opened_at,
            "open_signature": self.open_signature,
            "entry_price_sol": _s(self.entry_price_sol),
            "entry_price_usd": _s(self.entry_price_usd),
            "sol_price_usd_at_entry": _s(self.sol_price_usd_at_entry),
            "cost_sol": _s(self.cost_sol),
            "tokens_initial": _s(self.tokens_initial),
            "tokens_remaining": _s(self.tokens_remaining),
            "target_tokens_at_entry": _s(self.target_tokens_at_entry),
            "target_tokens_last_seen": _s(self.target_tokens_last_seen),
            "target_buy_usd": _s(self.target_buy_usd),
            "realized_pnl_sol": _s(self.realized_pnl_sol),
            "realized_pnl_usd": _s(self.realized_pnl_usd),
            "sol_returned": _s(self.sol_returned),
            "sells": [s.to_dict() for s in self.sells],
            "status": self.status.value,
            "closed_at": self.closed_at,
            "close_reason": self.close_reason.value if self.close_reason else None,
            "last_price_sol": _s(self.last_price_sol),
            "last_price_ts": self.last_price_ts,
            "owner_wallet": self.owner_wallet,
            "owner_name": self.owner_name,
            "owner_slot": self.owner_slot,
            "owner_block_time": self.owner_block_time,
            "pending_sell_remaining": _s(self.pending_sell_remaining),
            "skipped_dust_sells": self.skipped_dust_sells,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Position":
        raw_price = data.get("last_price_sol")
        return cls(
            mint=data["mint"],
            symbol=data.get("symbol", ""),
            decimals=int(data.get("decimals", 0)),
            opened_at=float(data["opened_at"]),
            open_signature=data.get("open_signature", ""),
            entry_price_sol=_d(data["entry_price_sol"]),
            entry_price_usd=_d(data.get("entry_price_usd")),
            sol_price_usd_at_entry=_d(data.get("sol_price_usd_at_entry")),
            cost_sol=_d(data["cost_sol"]),
            tokens_initial=_d(data["tokens_initial"]),
            tokens_remaining=_d(data["tokens_remaining"]),
            target_tokens_at_entry=_d(data.get("target_tokens_at_entry")),
            target_tokens_last_seen=_d(data.get("target_tokens_last_seen")),
            target_buy_usd=_d(data.get("target_buy_usd")),
            realized_pnl_sol=_d(data.get("realized_pnl_sol")),
            realized_pnl_usd=_d(data.get("realized_pnl_usd")),
            sol_returned=_d(data.get("sol_returned")),
            sells=[PartialSell.from_dict(s) for s in data.get("sells", [])],
            status=PositionStatus(data.get("status", "OPEN")),
            closed_at=data.get("closed_at"),
            close_reason=CloseReason(data["close_reason"]) if data.get("close_reason") else None,
            last_price_sol=None if raw_price is None else _d(raw_price),
            last_price_ts=float(data.get("last_price_ts", 0.0)),
            owner_wallet=data.get("owner_wallet", ""),
            owner_name=data.get("owner_name", ""),
            owner_slot=int(data.get("owner_slot", 0)),
            owner_block_time=data.get("owner_block_time"),
            # Позиции из состояния до порога на мелкие продажи ничего не копили.
            pending_sell_remaining=_d(data.get("pending_sell_remaining"), "1"),
            skipped_dust_sells=int(data.get("skipped_dust_sells", 0)),
        )


# --------------------------------------------------------------------------- #
# состояние бота
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Counters:
    buys_detected: int = 0
    buys_copied: int = 0
    buys_skipped: int = 0
    sells_detected: int = 0
    sells_copied: int = 0
    sells_skipped: int = 0
    positions_closed: int = 0
    transfers_out: int = 0
    tx_processed: int = 0
    tx_failed_parse: int = 0
    owner_reassigned: int = 0

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Counters":
        return cls(**{k: int(v) for k, v in data.items() if k in cls.__slots__})


@dataclass(slots=True)
class WalletCounters:
    """Активность одного отслеживаемого кошелька — отдельно от общего итога."""

    buys_detected: int = 0
    buys_copied: int = 0
    buys_skipped: int = 0
    sells_detected: int = 0
    sells_copied: int = 0
    sells_skipped: int = 0
    transfers_out: int = 0

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WalletCounters":
        return cls(**{k: int(v) for k, v in data.items() if k in cls.__slots__})


@dataclass(slots=True)
class WalletCursor:
    """Своя позиция чтения по каждому кошельку.

    У кошельков независимые потоки подписей, поэтому общий курсор невозможен:
    `getSignaturesForAddress(until=...)` работает только в рамках одного адреса.
    """

    last_signature: str | None = None
    last_slot: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"last_signature": self.last_signature, "last_slot": self.last_slot}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WalletCursor":
        return cls(
            last_signature=data.get("last_signature"),
            last_slot=int(data.get("last_slot", 0)),
        )


STATE_VERSION = 2


@dataclass(slots=True)
class BotState:
    version: int = STATE_VERSION
    target_wallets: list[str] = field(default_factory=list)
    wallet_names: dict[str, str] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    cursors: dict[str, WalletCursor] = field(default_factory=dict)
    processed_signatures: list[str] = field(default_factory=list)
    open_positions: dict[str, Position] = field(default_factory=dict)
    closed_positions: list[Position] = field(default_factory=list)
    counters: Counters = field(default_factory=Counters)
    wallet_counters: dict[str, WalletCounters] = field(default_factory=dict)
    symbols: dict[str, str] = field(default_factory=dict)

    def all_positions(self) -> Iterable[Position]:
        yield from self.open_positions.values()
        yield from self.closed_positions

    # ---------- кошельки ----------

    def cursor(self, wallet: str) -> WalletCursor:
        cursor = self.cursors.get(wallet)
        if cursor is None:
            cursor = WalletCursor()
            self.cursors[wallet] = cursor
        return cursor

    def wallet_stats(self, wallet: str) -> WalletCounters:
        counters = self.wallet_counters.get(wallet)
        if counters is None:
            counters = WalletCounters()
            self.wallet_counters[wallet] = counters
        return counters

    def wallet_label(self, wallet: str) -> str:
        return self.wallet_names.get(wallet) or short_mint(wallet)

    # ---------- сериализация ----------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "target_wallets": self.target_wallets,
            "wallet_names": self.wallet_names,
            "started_at": self.started_at,
            "cursors": {w: c.to_dict() for w, c in self.cursors.items()},
            "processed_signatures": self.processed_signatures,
            "open_positions": {m: p.to_dict() for m, p in self.open_positions.items()},
            "closed_positions": [p.to_dict() for p in self.closed_positions],
            "counters": self.counters.to_dict(),
            "wallet_counters": {w: c.to_dict() for w, c in self.wallet_counters.items()},
            "symbols": self.symbols,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotState":
        version = int(data.get("version", 1))
        wallets = list(data.get("target_wallets") or [])
        cursors = {
            wallet: WalletCursor.from_dict(raw)
            for wallet, raw in (data.get("cursors") or {}).items()
        }

        # v1 -> v2: один кошелёк и один общий курсор превращаются в списки.
        if version < STATE_VERSION:
            legacy_wallet = data.get("target_wallet") or ""
            if legacy_wallet:
                if legacy_wallet not in wallets:
                    wallets.append(legacy_wallet)
                cursors.setdefault(legacy_wallet, WalletCursor(
                    last_signature=data.get("last_signature"),
                    last_slot=int(data.get("last_slot", 0)),
                ))

        return cls(
            version=STATE_VERSION,
            target_wallets=wallets,
            wallet_names=dict(data.get("wallet_names") or {}),
            started_at=float(data.get("started_at", time.time())),
            cursors=cursors,
            processed_signatures=list(data.get("processed_signatures", [])),
            open_positions={
                m: Position.from_dict(p) for m, p in data.get("open_positions", {}).items()
            },
            closed_positions=[Position.from_dict(p) for p in data.get("closed_positions", [])],
            counters=Counters.from_dict(data.get("counters", {})),
            wallet_counters={
                w: WalletCounters.from_dict(c)
                for w, c in (data.get("wallet_counters") or {}).items()
            },
            symbols=dict(data.get("symbols", {})),
        )
