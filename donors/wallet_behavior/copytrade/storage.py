"""Персистентность состояния в JSON: атомарная запись, переживает рестарт."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from .constants import short_mint
from .models import BotState

log = logging.getLogger("copytrade.storage")


class StateStore:
    def __init__(self, path: str | Path, *, keep_backup: bool = True) -> None:
        self.path = Path(path)
        self.keep_backup = keep_backup
        self._last_save = 0.0

    def load(
        self,
        expected_wallets: Sequence[str] = (),
        names: Mapping[str, str] | None = None,
    ) -> BotState:
        expected = list(expected_wallets)
        if not self.path.is_file():
            log.info("Состояние не найдено (%s) — стартуем с нуля", self.path)
            return self._fresh(expected, names)

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            state = BotState.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            broken = self.path.with_suffix(f".broken.{int(time.time())}.json")
            log.error("Состояние повреждено (%s). Сохраняю как %s и стартую заново.", exc, broken)
            try:
                shutil.copy2(self.path, broken)
            except OSError:
                pass
            return self._fresh(expected, names)

        self._check_wallets(state, expected)
        if expected:
            state.target_wallets = expected
        state.wallet_names.update(names or {})
        log.info(
            "Состояние загружено: кошельков %s, открытых позиций %s, закрытых %s",
            len(state.target_wallets), len(state.open_positions), len(state.closed_positions),
        )
        for wallet in state.target_wallets:
            cursor = state.cursors.get(wallet)
            if cursor and cursor.last_signature:
                log.debug("  %s: курсор %s… (слот %s)",
                          state.wallet_label(wallet), cursor.last_signature[:16], cursor.last_slot)
        return state

    def _fresh(self, wallets: Sequence[str], names: Mapping[str, str] | None) -> BotState:
        return BotState(target_wallets=list(wallets), wallet_names=dict(names or {}))

    def _check_wallets(self, state: BotState, expected: Sequence[str]) -> None:
        """Защита от чужого состояния.

        Список кошельков можно менять на ходу — добавленный просто стартует
        с чистым курсором, убранный оставляет свои позиции досчитываться.
        А вот полностью другой набор почти наверняка означает перепутанный
        STATE_FILE: продолжать на нём — значит смешать две разные статистики.
        """
        if not expected or not state.target_wallets:
            return
        known = set(state.target_wallets)
        wanted = set(expected)
        if known & wanted:
            for wallet in wanted - known:
                log.info("Новый кошелёк в конфиге: %s — стартует с чистым курсором",
                         short_mint(wallet))
            for wallet in known - wanted:
                log.warning("Кошелёк %s убран из конфига, но остаётся в статистике "
                            "и в открытых позициях", short_mint(wallet))
            return
        raise RuntimeError(
            f"В {self.path} состояние по кошелькам "
            f"{', '.join(short_mint(w) for w in state.target_wallets)}, "
            f"а в конфиге {', '.join(short_mint(w) for w in expected)} — "
            f"пересечений нет. Укажи другой STATE_FILE или удали старый."
        )

    def save(self, state: BotState, *, max_signatures: int = 5000) -> None:
        # Храним только хвост подписей: он нужен лишь для дедупликации,
        # а полная история tx и так есть в блокчейне.
        if len(state.processed_signatures) > max_signatures:
            state.processed_signatures = state.processed_signatures[-max_signatures:]

        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Пишем во временный файл в том же каталоге и подменяем: os.replace
        # атомарен, поэтому убитый по Ctrl+C бот не оставит обрезанный JSON.
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            if self.keep_backup and self.path.is_file():
                try:
                    shutil.copy2(self.path, self.path.with_suffix(".bak.json"))
                except OSError:
                    pass
            os.replace(tmp_name, self.path)
            self._last_save = time.time()
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def save_throttled(self, state: BotState, interval: float, *, max_signatures: int = 5000) -> bool:
        if time.time() - self._last_save < interval:
            return False
        self.save(state, max_signatures=max_signatures)
        return True
