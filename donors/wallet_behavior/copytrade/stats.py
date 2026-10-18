"""Статистика: сводка, консольная таблица, экспорт в CSV."""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .constants import pad_display, short_mint, truncate_display
from .logging_setup import colorize
from .models import BotState, Position, PositionStatus

log = logging.getLogger("copytrade.stats")
ZERO = Decimal(0)


# --------------------------------------------------------------------------- #
# форматирование
# --------------------------------------------------------------------------- #

def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def iso(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _num(value: Decimal, digits: int = 4) -> str:
    return f"{value:,.{digits}f}"


def _signed(value: Decimal, digits: int = 4, *, color: bool = True) -> str:
    text = f"{value:+,.{digits}f}"
    if not color:
        return text
    if value > 0:
        return colorize(text, "green")
    if value < 0:
        return colorize(text, "red")
    return text


# --------------------------------------------------------------------------- #
# сводка
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class PositionView:
    position: Position
    mark_price_sol: Decimal | None
    unrealized_sol: Decimal
    total_sol: Decimal
    total_usd: Decimal
    pnl_pct: Decimal
    hold_sec: float


@dataclass(slots=True)
class WalletView:
    """PnL в разрезе ведущего: кого из отслеживаемых копировать выгодно."""

    wallet: str
    name: str
    positions: int = 0
    open_count: int = 0
    closed_count: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    invested_sol: Decimal = ZERO
    realized_pnl_sol: Decimal = ZERO
    realized_pnl_usd: Decimal = ZERO
    unrealized_pnl_sol: Decimal = ZERO
    unrealized_pnl_usd: Decimal = ZERO
    hold_sum_sec: float = 0.0

    @property
    def total_pnl_sol(self) -> Decimal:
        return self.realized_pnl_sol + self.unrealized_pnl_sol

    @property
    def total_pnl_usd(self) -> Decimal:
        return self.realized_pnl_usd + self.unrealized_pnl_usd

    @property
    def win_rate(self) -> Decimal:
        decided = self.wins + self.losses
        return ZERO if decided == 0 else Decimal(self.wins) / Decimal(decided) * Decimal(100)

    @property
    def roi_pct(self) -> Decimal:
        if self.invested_sol <= 0:
            return ZERO
        return self.total_pnl_sol / self.invested_sol * Decimal(100)

    @property
    def avg_hold_sec(self) -> float:
        return self.hold_sum_sec / self.positions if self.positions else 0.0


@dataclass(slots=True)
class Summary:
    generated_at: float = field(default_factory=time.time)
    sol_price_usd: Decimal = ZERO
    open_count: int = 0
    closed_count: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    invested_sol: Decimal = ZERO
    realized_pnl_sol: Decimal = ZERO
    realized_pnl_usd: Decimal = ZERO
    unrealized_pnl_sol: Decimal = ZERO
    unrealized_pnl_usd: Decimal = ZERO
    best: PositionView | None = None
    worst: PositionView | None = None
    avg_hold_sec: float = 0.0
    avg_hold_closed_sec: float = 0.0
    views: list[PositionView] = field(default_factory=list)
    wallets: list[WalletView] = field(default_factory=list)

    @property
    def total_pnl_sol(self) -> Decimal:
        return self.realized_pnl_sol + self.unrealized_pnl_sol

    @property
    def total_pnl_usd(self) -> Decimal:
        return self.realized_pnl_usd + self.unrealized_pnl_usd

    @property
    def win_rate(self) -> Decimal:
        decided = self.wins + self.losses
        return ZERO if decided == 0 else Decimal(self.wins) / Decimal(decided) * Decimal(100)

    @property
    def roi_pct(self) -> Decimal:
        return ZERO if self.invested_sol <= 0 else self.total_pnl_sol / self.invested_sol * Decimal(100)


def build_summary(
    state: BotState,
    marks: dict[str, Decimal] | None = None,
    sol_price_usd: Decimal = ZERO,
) -> Summary:
    marks = marks or {}
    now = time.time()
    summary = Summary(sol_price_usd=sol_price_usd)
    holds: list[float] = []
    closed_holds: list[float] = []
    by_wallet: dict[str, WalletView] = {}

    def wallet_view(address: str) -> WalletView:
        view = by_wallet.get(address)
        if view is None:
            # Позиции из состояния до мульти-кошельков ведущего не имеют.
            name = state.wallet_names.get(address) or (
                short_mint(address) if address else "(без ведущего)"
            )
            view = WalletView(wallet=address, name=name)
            by_wallet[address] = view
        return view

    for position in state.all_positions():
        mark = marks.get(position.mint) or position.last_price_sol
        if position.status is PositionStatus.CLOSED:
            mark = None  # у закрытой позиции нереализованной части нет

        unrealized = position.unrealized_pnl_sol(mark)
        total_sol = position.realized_pnl_sol + unrealized
        total_usd = position.realized_pnl_usd + unrealized * sol_price_usd
        hold = position.hold_seconds(now)

        view = PositionView(
            position=position,
            mark_price_sol=mark,
            unrealized_sol=unrealized,
            total_sol=total_sol,
            total_usd=total_usd,
            pnl_pct=position.pnl_pct(mark),
            hold_sec=hold,
        )
        summary.views.append(view)
        holds.append(hold)

        owner = wallet_view(position.owner_wallet)
        owner.positions += 1
        owner.hold_sum_sec += hold
        owner.invested_sol += position.cost_sol
        owner.realized_pnl_sol += position.realized_pnl_sol
        owner.realized_pnl_usd += position.realized_pnl_usd
        owner.unrealized_pnl_sol += unrealized
        owner.unrealized_pnl_usd += unrealized * sol_price_usd

        summary.invested_sol += position.cost_sol
        summary.realized_pnl_sol += position.realized_pnl_sol
        summary.realized_pnl_usd += position.realized_pnl_usd
        summary.unrealized_pnl_sol += unrealized
        summary.unrealized_pnl_usd += unrealized * sol_price_usd

        if position.status is PositionStatus.CLOSED:
            summary.closed_count += 1
            owner.closed_count += 1
            closed_holds.append(hold)
            if position.realized_pnl_sol > 0:
                summary.wins += 1
                owner.wins += 1
            elif position.realized_pnl_sol < 0:
                summary.losses += 1
                owner.losses += 1
            else:
                summary.breakeven += 1
                owner.breakeven += 1
        else:
            summary.open_count += 1
            owner.open_count += 1

        if summary.best is None or total_sol > summary.best.total_sol:
            summary.best = view
        if summary.worst is None or total_sol < summary.worst.total_sol:
            summary.worst = view

    # Кошельки без единой позиции тоже показываем: «ничего не купил» —
    # это тоже результат, и его надо видеть рядом с остальными.
    for address in state.target_wallets:
        wallet_view(address)

    summary.wallets = sorted(
        by_wallet.values(), key=lambda w: (-w.total_pnl_sol, w.name)
    )
    summary.avg_hold_sec = sum(holds) / len(holds) if holds else 0.0
    summary.avg_hold_closed_sec = sum(closed_holds) / len(closed_holds) if closed_holds else 0.0
    return summary


# --------------------------------------------------------------------------- #
# консольная таблица
# --------------------------------------------------------------------------- #

_HEADERS = (
    ("TOKEN", 12, "<"),
    ("ВЕДУЩИЙ", 12, "<"),
    ("STATUS", 7, "<"),
    ("HOLD", 8, ">"),
    ("COST SOL", 10, ">"),
    ("REAL SOL", 12, ">"),
    ("UNREAL SOL", 12, ">"),
    ("TOTAL SOL", 12, ">"),
    ("TOTAL USD", 12, ">"),
    ("PNL %", 9, ">"),
)

_WALLET_HEADERS = (
    ("ВЕДУЩИЙ", 16, "<"),
    ("АДРЕС", 12, "<"),
    ("ПОЗ", 5, ">"),
    ("ОТКР", 5, ">"),
    ("WIN%", 7, ">"),
    ("COST SOL", 10, ">"),
    ("REAL SOL", 12, ">"),
    ("UNREAL SOL", 12, ">"),
    ("TOTAL SOL", 12, ">"),
    ("TOTAL USD", 12, ">"),
    ("ROI %", 9, ">"),
)


def _rowify(headers: tuple[tuple[str, int, str], ...]):
    # pad_display считает ЯЧЕЙКИ терминала, а не символы: иероглиф в тикере
    # (富贵, 月薪喵) занимает две ячейки, ANSI-последовательности — ноль.
    def row(cells: list[str]) -> str:
        return "  ".join(
            pad_display(cell, width, align)
            for (_, width, align), cell in zip(headers, cells)
        )
    return row


def _ruler(headers: tuple[tuple[str, int, str], ...]) -> str:
    return "-" * (sum(w for _, w, _ in headers) + 2 * (len(headers) - 1))


def render_table(summary: Summary, *, color: bool = True, limit: int = 0) -> str:
    if not summary.views:
        return "Позиций пока нет."

    row = _rowify(_HEADERS)
    lines = [row([h[0] for h in _HEADERS]), _ruler(_HEADERS)]

    views = sorted(
        summary.views,
        key=lambda v: (v.position.status is PositionStatus.CLOSED, -v.hold_sec),
    )
    if limit > 0:
        views = views[:limit]

    for view in views:
        position = view.position
        status = truncate_display(position.status.value, 7)
        status_cell = colorize(status, "cyan" if position.is_open else "grey") if color else status
        owner = position.owner_name or short_mint(position.owner_wallet) or "—"
        lines.append(row([
            truncate_display(position.symbol or "?", 12),
            truncate_display(owner, 12),
            status_cell,
            human_duration(view.hold_sec),
            _num(position.cost_sol, 3),
            _signed(position.realized_pnl_sol, 4, color=color),
            _signed(view.unrealized_sol, 4, color=color),
            _signed(view.total_sol, 4, color=color),
            _signed(view.total_usd, 2, color=color),
            _signed(view.pnl_pct, 2, color=color),
        ]))

    return "\n".join(lines)


def render_wallets(summary: Summary, *, color: bool = True) -> str:
    """Кто из отслеживаемых приносит прибыль, а кто тянет в минус."""
    if not summary.wallets:
        return ""

    row = _rowify(_WALLET_HEADERS)
    lines = [row([h[0] for h in _WALLET_HEADERS]), _ruler(_WALLET_HEADERS)]
    for wallet in summary.wallets:
        lines.append(row([
            truncate_display(wallet.name, 16),
            short_mint(wallet.wallet) if wallet.wallet else "—",
            str(wallet.positions),
            str(wallet.open_count),
            f"{wallet.win_rate:.0f}" if (wallet.wins + wallet.losses) else "—",
            _num(wallet.invested_sol, 3),
            _signed(wallet.realized_pnl_sol, 4, color=color),
            _signed(wallet.unrealized_pnl_sol, 4, color=color),
            _signed(wallet.total_pnl_sol, 4, color=color),
            _signed(wallet.total_pnl_usd, 2, color=color),
            _signed(wallet.roi_pct, 2, color=color) if wallet.invested_sol > 0 else "—",
        ]))
    return "\n".join(lines)


def render_summary(summary: Summary, state: BotState, *, color: bool = True) -> str:
    counters = state.counters
    sol_usd = summary.sol_price_usd
    title = "=== COPYTRADE — СВОДКА ==="
    header = colorize(title, "bwhite") if color else title

    lines = [
        "",
        header,
        f"Кошельков в работе  : {len(state.target_wallets)}",
        f"Время               : {iso(summary.generated_at)}  (SOL ~ ${sol_usd:,.2f})",
        f"Позиций             : открыто {summary.open_count}, закрыто {summary.closed_count}",
        f"Win rate            : {summary.win_rate:.1f}%  "
        f"(+{summary.wins} / -{summary.losses} / ={summary.breakeven})",
        f"Вложено (виртуально): {_num(summary.invested_sol, 3)} SOL",
        f"Реализованный PnL   : {_signed(summary.realized_pnl_sol, 4, color=color)} SOL / "
        f"{_signed(summary.realized_pnl_usd, 2, color=color)} $",
        f"Нереализованный PnL : {_signed(summary.unrealized_pnl_sol, 4, color=color)} SOL / "
        f"{_signed(summary.unrealized_pnl_usd, 2, color=color)} $",
        f"ИТОГО               : {_signed(summary.total_pnl_sol, 4, color=color)} SOL / "
        f"{_signed(summary.total_pnl_usd, 2, color=color)} $  "
        f"({_signed(summary.roi_pct, 2, color=color)}%)",
        f"Средний холд        : {human_duration(summary.avg_hold_sec)} "
        f"(закрытые: {human_duration(summary.avg_hold_closed_sec)})",
    ]

    if summary.best is not None:
        lines.append(
            f"Лучшая сделка       : {summary.best.position.label()} "
            f"{_signed(summary.best.total_sol, 4, color=color)} SOL "
            f"({_signed(summary.best.pnl_pct, 1, color=color)}%)"
        )
    if summary.worst is not None:
        lines.append(
            f"Худшая сделка       : {summary.worst.position.label()} "
            f"{_signed(summary.worst.total_sol, 4, color=color)} SOL "
            f"({_signed(summary.worst.pnl_pct, 1, color=color)}%)"
        )

    lines += [
        f"События             : BUY {counters.buys_detected} "
        f"(копий {counters.buys_copied}, пропусков {counters.buys_skipped}) | "
        f"SELL {counters.sells_detected} (копий {counters.sells_copied}, "
        f"пропусков {counters.sells_skipped}) | "
        f"TRANSFER_OUT {counters.transfers_out} | закрыто {counters.positions_closed}",
        f"Транзакций разобрано: {counters.tx_processed} (ошибок разбора {counters.tx_failed_parse})",
    ]
    if counters.owner_reassigned:
        lines.append(
            f"Смен ведущего       : {counters.owner_reassigned} "
            f"(догон принёс более раннюю покупку)"
        )

    wallets_table = render_wallets(summary, color=color)
    if wallets_table:
        lines += ["", "--- PnL по ведущим ---", wallets_table]

    lines += ["", "--- Позиции ---", render_table(summary, color=color), ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #

POSITION_COLUMNS = [
    "mint", "symbol", "owner_wallet", "owner_name", "status", "close_reason",
    "opened_at", "closed_at", "hold_seconds", "hold_human",
    "entry_price_sol", "entry_price_usd", "sol_price_usd_at_entry",
    "cost_sol", "tokens_initial", "tokens_remaining", "sold_fraction_total",
    "mark_price_sol", "sol_returned",
    "realized_pnl_sol", "realized_pnl_usd",
    "unrealized_pnl_sol", "total_pnl_sol", "total_pnl_usd", "pnl_pct",
    "partial_sells", "target_buy_usd", "target_tokens_at_entry",
    "target_tokens_last_seen", "open_signature",
]

TRADE_COLUMNS = [
    "mint", "symbol", "owner_wallet", "owner_name", "ts", "signature",
    "fraction_of_remaining", "tokens_sold", "price_sol", "sol_received",
    "sol_price_usd", "realized_pnl_sol", "realized_pnl_usd", "price_source",
]

WALLET_COLUMNS = [
    "wallet", "name", "positions", "open", "closed", "wins", "losses", "breakeven",
    "win_rate_pct", "invested_sol", "realized_pnl_sol", "realized_pnl_usd",
    "unrealized_pnl_sol", "total_pnl_sol", "total_pnl_usd", "roi_pct",
    "avg_hold_seconds", "avg_hold_human",
    "buys_detected", "buys_copied", "buys_skipped",
    "sells_detected", "sells_copied", "sells_skipped", "transfers_out",
]


def _dec(value: Decimal | None, places: int = 9) -> str:
    """Decimal -> строка для CSV.

    Внутри считаем с точностью 40 знаков, но в отчёт такие хвосты пускать нельзя:
    Excel их всё равно обрежет, а глазами читать невозможно. Округляем до
    разумного разряда и убираем хвостовые нули, не скатываясь в E-нотацию.
    """
    if value is None:
        return ""
    quantized = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    normalized = quantized.normalize()
    return format(normalized, "f")


# Разрядность по смыслу колонки: цены мемкоинов уходят в 1e-12, SOL — лампорты.
_PRICE_PLACES = 18
_SOL_PLACES = 9
_USD_PLACES = 4


def export_summary_txt(text: str, path: str | Path) -> Path:
    """Сводка в файл: то же, что в консоли, но plain-text и всегда только свежая.

    Пишем через временный файл и os.replace: файл читают снаружи (tail, Notepad,
    другой скрипт), и застать его наполовину переписанным — хуже, чем прочитать
    сводку пятиминутной давности. replace на Windows атомарен в пределах тома,
    поэтому tmp кладём рядом с целевым файлом, а не в системный temp.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp")
    # newline="" — иначе Python на Windows превратит \n в \r\n поверх уже
    # готового текста; \n читается и Блокнотом Windows 10+, и всем остальным.
    tmp.write_text(text, encoding="utf-8", newline="")
    try:
        tmp.replace(target)
    except OSError:
        # Цель занята (открытый Excel/Блокнот на Windows) — временный файл
        # убираем за собой, иначе рядом со сводкой копится мусор.
        tmp.unlink(missing_ok=True)
        raise
    return target


def export_positions_csv(summary: Summary, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig — чтобы Excel на Windows не ломал кириллицу
    with target.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=POSITION_COLUMNS)
        writer.writeheader()
        for view in summary.views:
            p = view.position
            writer.writerow({
                "mint": p.mint,
                "symbol": p.symbol,
                "owner_wallet": p.owner_wallet,
                "owner_name": p.owner_name,
                "status": p.status.value,
                "close_reason": p.close_reason.value if p.close_reason else "",
                "opened_at": iso(p.opened_at),
                "closed_at": iso(p.closed_at),
                "hold_seconds": int(view.hold_sec),
                "hold_human": human_duration(view.hold_sec),
                "entry_price_sol": _dec(p.entry_price_sol, _PRICE_PLACES),
                "entry_price_usd": _dec(p.entry_price_usd, _PRICE_PLACES),
                "sol_price_usd_at_entry": _dec(p.sol_price_usd_at_entry, _USD_PLACES),
                "cost_sol": _dec(p.cost_sol, _SOL_PLACES),
                "tokens_initial": _dec(p.tokens_initial),
                "tokens_remaining": _dec(p.tokens_remaining),
                "sold_fraction_total": _dec(p.sold_fraction_total, 6),
                "mark_price_sol": _dec(view.mark_price_sol, _PRICE_PLACES),
                "sol_returned": _dec(p.sol_returned, _SOL_PLACES),
                "realized_pnl_sol": _dec(p.realized_pnl_sol, _SOL_PLACES),
                "realized_pnl_usd": _dec(p.realized_pnl_usd, _USD_PLACES),
                "unrealized_pnl_sol": _dec(view.unrealized_sol, _SOL_PLACES),
                "total_pnl_sol": _dec(view.total_sol, _SOL_PLACES),
                "total_pnl_usd": _dec(view.total_usd, _USD_PLACES),
                "pnl_pct": _dec(view.pnl_pct, _USD_PLACES),
                "partial_sells": len(p.sells),
                "target_buy_usd": _dec(p.target_buy_usd, _USD_PLACES),
                "target_tokens_at_entry": _dec(p.target_tokens_at_entry),
                "target_tokens_last_seen": _dec(p.target_tokens_last_seen),
                "open_signature": p.open_signature,
            })
    return target


def export_trades_csv(state: BotState, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=TRADE_COLUMNS)
        writer.writeheader()
        for position in state.all_positions():
            for sell in position.sells:
                writer.writerow({
                    "mint": position.mint,
                    "symbol": position.symbol,
                    "owner_wallet": position.owner_wallet,
                    "owner_name": position.owner_name,
                    "ts": iso(sell.ts),
                    "signature": sell.signature,
                    "fraction_of_remaining": _dec(sell.fraction, 6),
                    "tokens_sold": _dec(sell.tokens_sold),
                    "price_sol": _dec(sell.price_sol, _PRICE_PLACES),
                    "sol_received": _dec(sell.sol_received, _SOL_PLACES),
                    "sol_price_usd": _dec(sell.sol_price_usd, _USD_PLACES),
                    "realized_pnl_sol": _dec(sell.realized_pnl_sol, _SOL_PLACES),
                    "realized_pnl_usd": _dec(sell.realized_pnl_usd, _USD_PLACES),
                    "price_source": sell.price_source,
                })
    return target


def export_wallets_csv(summary: Summary, state: BotState, path: str | Path) -> Path:
    """PnL и активность в разрезе ведущего — по нему и решается, кого копировать."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=WALLET_COLUMNS)
        writer.writeheader()
        for wallet in summary.wallets:
            seen = state.wallet_counters.get(wallet.wallet)
            writer.writerow({
                "wallet": wallet.wallet,
                "name": wallet.name,
                "positions": wallet.positions,
                "open": wallet.open_count,
                "closed": wallet.closed_count,
                "wins": wallet.wins,
                "losses": wallet.losses,
                "breakeven": wallet.breakeven,
                "win_rate_pct": _dec(wallet.win_rate, 2),
                "invested_sol": _dec(wallet.invested_sol, _SOL_PLACES),
                "realized_pnl_sol": _dec(wallet.realized_pnl_sol, _SOL_PLACES),
                "realized_pnl_usd": _dec(wallet.realized_pnl_usd, _USD_PLACES),
                "unrealized_pnl_sol": _dec(wallet.unrealized_pnl_sol, _SOL_PLACES),
                "total_pnl_sol": _dec(wallet.total_pnl_sol, _SOL_PLACES),
                "total_pnl_usd": _dec(wallet.total_pnl_usd, _USD_PLACES),
                "roi_pct": _dec(wallet.roi_pct, _USD_PLACES),
                "avg_hold_seconds": int(wallet.avg_hold_sec),
                "avg_hold_human": human_duration(wallet.avg_hold_sec),
                "buys_detected": seen.buys_detected if seen else 0,
                "buys_copied": seen.buys_copied if seen else 0,
                "buys_skipped": seen.buys_skipped if seen else 0,
                "sells_detected": seen.sells_detected if seen else 0,
                "sells_copied": seen.sells_copied if seen else 0,
                "sells_skipped": seen.sells_skipped if seen else 0,
                "transfers_out": seen.transfers_out if seen else 0,
            })
    return target
