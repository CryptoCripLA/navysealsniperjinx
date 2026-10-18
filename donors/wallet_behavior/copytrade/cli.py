"""CLI: run / stats / export / check."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from decimal import Decimal
from pathlib import Path

import aiohttp
import websockets

from . import __version__
from .config import Config, ConfigError, load_config
from .engine import Engine
from .executor import build_executor
from .logging_setup import EventLogger, setup_logging
from .prices import PriceService
from .rpc import RpcClient, mask_url
from .stats import (
    build_summary,
    export_positions_csv,
    export_trades_csv,
    export_wallets_csv,
    render_summary,
)
from .storage import StateStore

log = logging.getLogger("copytrade.cli")
ZERO = Decimal(0)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="copytrade",
        description="Копитрейд-бот для Solana (демо / paper trading).",
    )
    parser.add_argument("--version", action="version", version=f"copytrade {__version__}")
    parser.add_argument("-c", "--config", default=None, help="путь к config.json")
    parser.add_argument("-e", "--env", default=None, help="путь к .env")
    parser.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR")
    parser.add_argument("--no-color", action="store_true", help="отключить цвета")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="запустить мониторинг (по умолчанию)")
    sub.add_parser("stats", help="напечатать сводку по сохранённому состоянию")
    sub.add_parser("export", help="выгрузить positions.csv и trades.csv")
    sub.add_parser("check", help="проверить конфиг, RPC, WebSocket и Price API")
    return parser


def _make_price_service(cfg: Config) -> PriceService:
    return PriceService(
        jupiter_base_url=cfg.jupiter_base_url,
        jupiter_api_key=cfg.jupiter_api_key,
        jupiter_max_rps=cfg.jupiter_max_rps,
        sol_price_ttl_sec=cfg.sol_price_ttl_sec,
        token_price_ttl_sec=cfg.token_price_ttl_sec,
        timeout_sec=cfg.price_http_timeout_sec,
        fallback_sol_price_usd=cfg.fallback_sol_price_usd,
    )


def _make_rpc(cfg: Config) -> RpcClient:
    return RpcClient(
        cfg.rpc_http_url,
        timeout_sec=cfg.rpc_timeout_sec,
        max_retries=cfg.rpc_max_retries,
        max_concurrency=cfg.rpc_max_concurrency,
        min_interval_ms=cfg.rpc_min_interval_ms,
        commitment=cfg.commitment,
    )


# --------------------------------------------------------------------------- #
# команды
# --------------------------------------------------------------------------- #

async def cmd_run(cfg: Config) -> int:
    store = StateStore(cfg.state_file)
    state = store.load(cfg.wallet_addresses, cfg.wallet_names)
    events = EventLogger("copytrade.events", cfg.events_file)

    for warning in cfg.live_warnings():
        log.warning("%s", warning)

    async with _make_rpc(cfg) as rpc, _make_price_service(cfg) as prices:
        prices.seed_symbols(state.symbols)
        session: aiohttp.ClientSession | None = None
        if not cfg.demo_mode:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=cfg.price_http_timeout_sec * 2)
            )
        try:
            executor = build_executor(cfg, rpc, session)
            engine = Engine(cfg, state, rpc, prices, executor, store, events)
            _install_signal_handlers(engine)

            try:
                await engine.run()
            except (KeyboardInterrupt, asyncio.CancelledError):
                log.info("Останов по сигналу")
            finally:
                engine.stop()
                await engine.shutdown()
                await executor.close()
        finally:
            if session is not None:
                await session.close()
    return 0


async def cmd_stats(cfg: Config) -> int:
    state = StateStore(cfg.state_file).load(cfg.wallet_addresses, cfg.wallet_names)
    async with _make_price_service(cfg) as prices:
        prices.seed_symbols(state.symbols)
        mints = list(state.open_positions)
        marks = await prices.token_prices_sol(mints) if mints else {}
        sol_usd = await prices.sol_usd() or ZERO
    summary = build_summary(state, marks, sol_usd)
    print(render_summary(summary, state, color=cfg.color))
    return 0


async def cmd_export(cfg: Config) -> int:
    state = StateStore(cfg.state_file).load(cfg.wallet_addresses, cfg.wallet_names)
    async with _make_price_service(cfg) as prices:
        prices.seed_symbols(state.symbols)
        mints = list(state.open_positions)
        marks = await prices.token_prices_sol(mints) if mints else {}
        sol_usd = await prices.sol_usd() or ZERO
    summary = build_summary(state, marks, sol_usd)
    positions = export_positions_csv(summary, cfg.positions_csv)
    trades = export_trades_csv(state, cfg.trades_csv)
    wallets = export_wallets_csv(summary, state, cfg.wallets_csv)
    print(f"Позиции : {positions}")
    print(f"Сделки  : {trades}")
    print(f"Кошельки: {wallets}")
    return 0


async def cmd_check(cfg: Config) -> int:
    ok = True
    print("--- Конфиг ---")
    # Вывод check первым делом кидают в чат, поэтому ключ здесь не показываем.
    print(f"  RPC HTTP        : {mask_url(cfg.rpc_http_url)}")
    print(f"  RPC WS          : {mask_url(cfg.rpc_ws_url)}")
    print(f"  MIN_BUY_USD     : {cfg.min_buy_usd}")
    print(f"  POSITION_SIZE   : {cfg.position_size_sol} SOL")
    print(f"  DEMO_MODE       : {cfg.demo_mode}")
    print(f"  TRANSFER=SELL   : {cfg.treat_transfer_as_sell}")
    print(f"  Jupiter API key : {'задан' if cfg.jupiter_api_key else 'нет (keyless, 0.5 rps)'}")
    print(f"  Состояние       : {cfg.state_file}")
    print(f"  Логи            : {cfg.log_dir}/ (новый файл на каждый запуск)")
    print(f"  Сводка (текст)  : {cfg.summary_file or '(отключена)'}"
          + (f" — раз в {cfg.stats_interval_sec:g} c" if cfg.stats_interval_sec > 0 else ""))

    print("\n--- Комиссии ---")
    print(f"  SLIPPAGE        : {cfg.slippage_bps} bps ({cfg.slippage_bps / 100:g}%)")
    print(f"  PRIORITY_FEE    : {cfg.priority_fee_sol} SOL ({cfg.priority_fee_lamports} lamports)")
    print(f"  BRIBE           : {cfg.bribe_sol} SOL ({cfg.bribe_lamports} lamports)")
    print(f"  FEE_MODE        : {cfg.fee_mode}"
          + (f" -> {cfg.jito_endpoint}" if cfg.fee_mode == "jito" else ""))
    print(f"  В демо списываем: {cfg.fixed_fee_sol} SOL/сделка, "
          f"проскальзывание {'да' if cfg.paper_apply_slippage else 'нет'}")

    print(f"\n--- Кошельки ({len(cfg.wallets)}) ---")
    for spec in cfg.wallets:
        print(f"  №{spec.index} {spec.name:<16} {spec.address}")

    if not cfg.demo_mode:
        print("\n--- Живая торговля ---")
        try:
            from .live import load_keypair
            keypair = load_keypair(cfg)
            print(f"  Ключ OK, торгуем с адреса: {keypair.pubkey()}")
        except Exception as exc:  # noqa: BLE001 - текст уже без ключа
            ok = False
            print(f"  ОШИБКА ключа: {exc}")
        print(f"  MAX_OPEN_POSITIONS : {cfg.max_open_positions}")
        print(f"  MAX_DAILY_LOSS_SOL : {cfg.max_daily_loss_sol}")
        print(f"  MIN_WALLET_BALANCE : {cfg.min_wallet_balance_sol} SOL")
        print(f"  Покупки из прошлого: "
              f"{'пропускаются (SKIP_BUYS_BEFORE_START)' if cfg.skip_buys_before_start else 'КОПИРУЮТСЯ'}")
        print(f"  Возраст сигнала    : "
              f"{f'не старше {cfg.max_signal_age_sec:g} c' if cfg.max_signal_age_sec > 0 else 'НЕ ПРОВЕРЯЕТСЯ'}"
              " (только покупки; продажи зеркалятся всегда)")

    for warning in cfg.live_warnings():
        print(f"  ВНИМАНИЕ: {warning}")

    print("\n--- RPC ---")
    async with _make_rpc(cfg) as rpc:
        try:
            health = await rpc.get_health()
            slot = await rpc.get_slot()
            print(f"  OK: getHealth={health}, слот {slot}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  ОШИБКА: {exc}")

        for spec in cfg.wallets:
            try:
                rows = await rpc.get_signatures_for_address(spec.address, limit=3)
                if rows:
                    newest = rows[0]
                    print(f"  №{spec.index} {spec.name}: последняя tx "
                          f"{newest['signature'][:24]}… (слот {newest.get('slot')})")
                else:
                    print(f"  №{spec.index} {spec.name}: ВНИМАНИЕ, нет транзакций")
            except Exception as exc:  # noqa: BLE001
                ok = False
                print(f"  №{spec.index} {spec.name}: ОШИБКА getSignaturesForAddress: {exc}")

    print("\n--- WebSocket ---")
    if not cfg.ws_enabled or not cfg.rpc_ws_url:
        print("  Пропущено: RPC_WS_URL не задан — бот будет работать на polling")
    else:
        try:
            async with websockets.connect(cfg.rpc_ws_url, open_timeout=15, close_timeout=5) as ws:
                # Проверяем подписку сразу на все кошельки: у части провайдеров
                # есть лимит на число подписок в одном соединении.
                for spec in cfg.wallets:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": spec.index, "method": "logsSubscribe",
                        "params": [{"mentions": [spec.address]},
                                   {"commitment": cfg.commitment}],
                    }))
                confirmed = 0
                for _ in cfg.wallets:
                    raw = await asyncio.wait_for(ws.recv(), timeout=20)
                    response = json.loads(raw)
                    if response.get("method"):
                        continue
                    if "error" in response:
                        ok = False
                        print(f"  ОШИБКА logsSubscribe: {response['error']}")
                    else:
                        confirmed += 1
                if confirmed:
                    print(f"  OK: подтверждено подписок {confirmed}/{len(cfg.wallets)}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  ОШИБКА: {exc}")

    print("\n--- Цены ---")
    async with _make_price_service(cfg) as prices:
        sol_usd = await prices.sol_usd(force=True)
        if sol_usd:
            print(f"  SOL/USD = ${sol_usd}")
        else:
            ok = False
            print("  ОШИБКА: не удалось получить курс SOL ни из одного источника")

    print("\nИТОГ:", "всё готово к запуску" if ok else "есть проблемы, см. выше")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# точка входа
# --------------------------------------------------------------------------- #

def _install_signal_handlers(engine: Engine) -> None:
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, engine.stop)
        except (NotImplementedError, RuntimeError):
            # Windows: add_signal_handler не поддержан, ловим KeyboardInterrupt выше
            pass


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    command = args.command or "run"

    try:
        cfg = load_config(config_path=args.config, env_path=args.env)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    if args.log_level:
        cfg.log_level = args.log_level
    if args.no_color:
        cfg.color = False

    log_file = None
    if command == "run":
        # Имя файла = время старта: каждый запуск пишет в свой лог, старые
        # не перетираются и их легко сопоставить с сессией.
        cfg.start_run()
        cfg.prune_old_logs()
        log_file = cfg.log_file

    setup_logging(cfg.log_level, log_file, cfg.color)
    if log_file:
        log.info("Лог этого запуска: %s", log_file)
        log.info("События (JSONL)  : %s", cfg.events_file)
        if cfg.summary_file:
            log.info("Сводка (текст)   : %s", cfg.summary_file)

    handlers = {
        "run": cmd_run,
        "stats": cmd_stats,
        "export": cmd_export,
        "check": cmd_check,
    }
    try:
        return asyncio.run(handlers[command](cfg))
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        return 130
    except Exception as exc:  # noqa: BLE001
        log.error("Фатальная ошибка: %s", exc, exc_info=True)
        return 1
