"""Серверный раннер проверок и реестр прогонов.

Агент не может подсунуть свой вывод: команду запускает раннер в копии рабочей
директории, сам пишет код возврата, хвост вывода и sha кода. Скрытые проверки
из ``hidden/`` подмешиваются в копию, а наружу отдаётся только pass/fail.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import NotFoundError, ValidationError
from . import secrets as secret_scanner

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    task_id TEXT,
    area TEXT,
    command TEXT NOT NULL,
    seeds INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    exit_code INTEGER,
    pass_rate REAL NOT NULL DEFAULT 0,
    output_tail TEXT,
    code_sha TEXT,
    hidden_passed INTEGER NOT NULL DEFAULT 0,
    hidden_total INTEGER NOT NULL DEFAULT 0,
    hidden_pass_rate REAL NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    system INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_runs_actor ON runs(actor);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
"""

_IGNORE = shutil.ignore_patterns(
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".venv",
    "node_modules", "*.pyc",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class AreaConfig:
    """Настройки проверки для области: команда, таймаут, seed и hidden."""

    command: str
    timeout: float = 120.0
    seeds: int = 1
    workdir: str | None = None
    hidden_command: str | None = None
    max_output: int = 4000

    def as_dict(self) -> dict[str, Any]:
        """Представить настройки словарём."""
        return {
            "command": self.command,
            "timeout": self.timeout,
            "seeds": self.seeds,
            "workdir": self.workdir,
            "hidden_command": self.hidden_command,
            "max_output": self.max_output,
        }


@dataclass(frozen=True)
class RunRecord:
    """Запись прогона, созданная системой."""

    id: int
    actor: str
    task_id: str | None
    area: str | None
    command: str
    seeds: int
    status: str
    exit_code: int | None
    pass_rate: float
    output_tail: str
    code_sha: str | None
    hidden_passed: int
    hidden_total: int
    hidden_pass_rate: float
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    system: bool = True

    @property
    def passed(self) -> bool:
        """Все seed и скрытые проверки прошли."""
        return self.status == "pass"

    def as_dict(self) -> dict[str, Any]:
        """Представить прогон словарём."""
        return {
            "id": self.id,
            "actor": self.actor,
            "task_id": self.task_id,
            "area": self.area,
            "command": self.command,
            "seeds": self.seeds,
            "status": self.status,
            "exit_code": self.exit_code,
            "pass_rate": self.pass_rate,
            "output_tail": self.output_tail,
            "code_sha": self.code_sha,
            "hidden_passed": self.hidden_passed,
            "hidden_total": self.hidden_total,
            "hidden_pass_rate": self.hidden_pass_rate,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "system": self.system,
        }


class Runner:
    """Реестр прогонов и серверный запуск проверок в копии рабочей директории."""

    def __init__(
        self,
        store: Any,
        *,
        workdir: str | Path | None = None,
        hidden_dir: str | Path | None = None,
        area_config: Mapping[str, AreaConfig | Mapping[str, Any]] | None = None,
        clock: Any = None,
    ) -> None:
        self.store = store
        self.workdir = Path(workdir) if workdir is not None else None
        self.hidden_dir = (
            Path(hidden_dir)
            if hidden_dir is not None
            else Path(store.root) / "hidden"
        )
        self._clock = clock or _utc_now
        self._lock = threading.RLock()
        self._areas: dict[str, AreaConfig] = {}
        for area, config in (area_config or {}).items():
            self.configure(area, config)
        with self.store._lock:
            self.store._ensure_open()
            with self.store._conn:
                self.store._conn.executescript(_SCHEMA)

    def configure(
        self, area: str, config: AreaConfig | Mapping[str, Any]
    ) -> AreaConfig:
        """Задать команду проверки для области пути."""
        if not isinstance(area, str) or not area.strip():
            raise ValidationError(
                "area обязательна.",
                hint="Передайте префикс области, например src/artek_brain.",
            )
        if isinstance(config, AreaConfig):
            prepared = config
        elif isinstance(config, Mapping):
            prepared = AreaConfig(
                command=str(config.get("command") or ""),
                timeout=float(config.get("timeout", 120.0)),
                seeds=int(config.get("seeds", 1)),
                workdir=config.get("workdir"),
                hidden_command=config.get("hidden_command"),
                max_output=int(config.get("max_output", 4000)),
            )
        elif isinstance(config, str):
            prepared = AreaConfig(command=config)
        else:
            raise ValidationError(
                "Некорректная конфигурация области.",
                hint="Передайте строку команды или словарь с command/timeout/seeds.",
            )
        if not prepared.command.strip():
            raise ValidationError(
                f"Для области «{area}» не задана команда.",
                hint="Например: {'command': 'uv run pytest -q'}.",
            )
        if prepared.seeds < 1 or prepared.seeds > 100:
            raise ValidationError(
                "seeds должен быть от 1 до 100.",
                hint="Передайте seeds=1 или другое небольшое число.",
            )
        self._areas[area.strip().strip("/")] = prepared
        return prepared

    def areas(self) -> dict[str, AreaConfig]:
        """Настроенные области проверки."""
        return dict(self._areas)

    def resolve(self, area: str) -> tuple[str, AreaConfig]:
        """Найти настройки по самому длинному подходящему префиксу."""
        cleaned = (area or "").strip().strip("/")
        matches = [
            (prefix, config)
            for prefix, config in self._areas.items()
            if not prefix
            or cleaned == prefix
            or cleaned.startswith(prefix + "/")
        ]
        if not matches:
            raise ValidationError(
                f"Для области «{area}» нет команды проверки.",
                hint="Настройте Runner.configure(area, {'command': ...}).",
            )
        prefix, config = max(matches, key=lambda item: len(item[0]))
        return prefix, config

    # ------------------------------------------------------------------
    # Запуск
    # ------------------------------------------------------------------
    def run(
        self,
        actor: str,
        *,
        task_id: str | None = None,
        area: str | None = None,
        command: str | None = None,
        seeds: int | None = None,
        timeout: float | None = None,
        workdir: str | Path | None = None,
        hidden_command: str | None = None,
        max_output: int | None = None,
    ) -> RunRecord:
        """Выполнить проверку в копии рабочей директории и записать прогон."""
        if not isinstance(actor, str) or not actor.strip():
            raise ValidationError(
                "actor обязателен.",
                hint="Передайте имя агента из токена.",
            )
        config = None
        if area:
            try:
                _, config = self.resolve(area)
            except ValidationError:
                if not command:
                    raise
        if config is not None:
            cmd = command or config.command
            run_timeout = float(timeout if timeout is not None else config.timeout)
            run_seeds = int(seeds if seeds is not None else config.seeds)
            source = workdir or config.workdir or self.workdir
            hidden_cmd = (
                hidden_command
                if hidden_command is not None
                else config.hidden_command
            )
            limit = int(max_output if max_output is not None else config.max_output)
        else:
            if not command:
                raise ValidationError(
                    "Не задана команда проверки.",
                    hint="Передайте area с настройкой или command.",
                )
            cmd = command
            run_timeout = float(timeout if timeout is not None else 120.0)
            run_seeds = int(seeds if seeds is not None else 1)
            source = workdir or self.workdir
            hidden_cmd = hidden_command
            limit = int(max_output if max_output is not None else 4000)
        if run_seeds < 1 or run_seeds > 100:
            raise ValidationError(
                "seeds должен быть от 1 до 100.",
                hint="Передайте seeds=1 или другое небольшое число.",
            )
        if run_timeout <= 0:
            raise ValidationError(
                "timeout должен быть больше 0.",
                hint="Передайте timeout=120 (секунды).",
            )
        source_dir = Path(source) if source is not None else Path.cwd()
        if not source_dir.is_dir():
            raise ValidationError(
                f"Рабочая директория «{source_dir}» не найдена.",
                hint="Передайте существующий workdir.",
            )

        started = self._clock()
        temp_root = Path(tempfile.mkdtemp(prefix="brain-run-"))
        copy_dir = temp_root / "work"
        try:
            shutil.copytree(source_dir, copy_dir, ignore=_IGNORE)
            hidden_files = self._copy_hidden(copy_dir)
            public_tail = ""
            exit_code: int | None = None
            passed_seeds = 0
            for index in range(run_seeds):
                env = self._env(index)
                code, output = self._execute(
                    cmd, copy_dir, env, run_timeout, limit
                )
                exit_code = code
                public_tail = output
                if code == 0:
                    passed_seeds += 1
            pass_rate = passed_seeds / run_seeds

            hidden_passed = 0
            hidden_total = len(hidden_files)
            hidden_pass_rate = 1.0 if hidden_total == 0 else 0.0
            if hidden_total:
                hidden_cmd = hidden_cmd or self._default_hidden_command()
                hidden_ok = 0
                for index in range(run_seeds):
                    env = self._env(index)
                    code, _ = self._execute(
                        hidden_cmd, copy_dir, env, run_timeout, 0
                    )
                    if code == 0:
                        hidden_ok += 1
                hidden_pass_rate = hidden_ok / run_seeds
                hidden_passed = (
                    hidden_total if hidden_pass_rate == 1.0 else 0
                )

            code_sha = self._code_sha(copy_dir)
            finished = self._clock()
            duration_ms = int((finished - started).total_seconds() * 1000)
            if pass_rate == 1.0 and hidden_pass_rate == 1.0:
                status = "pass"
            elif pass_rate == 0.0 and hidden_pass_rate == 0.0:
                status = "fail"
            else:
                status = "partial"
            record = self._insert(
                actor=actor.strip(),
                task_id=task_id,
                area=area,
                command=cmd,
                seeds=run_seeds,
                status=status,
                exit_code=exit_code,
                pass_rate=pass_rate,
                output_tail=secret_scanner.redact(public_tail),
                code_sha=code_sha,
                hidden_passed=hidden_passed,
                hidden_total=hidden_total,
                hidden_pass_rate=hidden_pass_rate,
                started_at=_format_ts(started),
                finished_at=_format_ts(finished),
                duration_ms=duration_ms,
            )
            self.store._audit_ok(
                actor.strip(),
                "run",
                task_id,
                {
                    "run_id": record.id,
                    "area": area,
                    "status": status,
                    "pass_rate": pass_rate,
                    "hidden_pass_rate": hidden_pass_rate,
                    "code_sha": code_sha,
                },
            )
            return record
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def _copy_hidden(self, copy_dir: Path) -> list[Path]:
        hidden = self.hidden_dir
        if not hidden.exists() or not hidden.is_dir():
            return []
        files = [
            item
            for item in hidden.rglob("*")
            if item.is_file() and "__pycache__" not in item.parts
        ]
        target = copy_dir / "_hidden"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(hidden, target, ignore=_IGNORE)
        return files

    @staticmethod
    def _default_hidden_command() -> str:
        return f"{sys.executable} -m pytest -q _hidden"

    @staticmethod
    def _env(seed: int) -> dict[str, str]:
        env = dict(os.environ)
        env["BRAIN_SEED"] = str(seed)
        env["PYTHONHASHSEED"] = str(seed)
        return env

    @staticmethod
    def _execute(
        command: str,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_output: int,
    ) -> tuple[int, str]:
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return -1, f"timeout после {timeout:g} с"
        except OSError as exc:
            return -1, f"не удалось запустить команду: {exc}"
        output = (completed.stdout or "") + (completed.stderr or "")
        if max_output <= 0:
            output = ""
        else:
            output = output[-max_output:]
        return int(completed.returncode), output

    @staticmethod
    def _code_sha(copy_dir: Path) -> str:
        digest = hashlib.sha256()
        for item in sorted(copy_dir.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(copy_dir)
            if any(part in _IGNORE_PATHS for part in relative.parts):
                continue
            if relative.parts and relative.parts[0] == "_hidden":
                continue
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(item.read_bytes())
            except OSError:
                continue
            digest.update(b"\0")
        return digest.hexdigest()

    # ------------------------------------------------------------------
    # Реестр
    # ------------------------------------------------------------------
    def _insert(self, **values: Any) -> RunRecord:
        with self._lock:
            self.store._ensure_open()
            with self.store._conn:
                cursor = self.store._conn.execute(
                    """
                    INSERT INTO runs
                        (actor, task_id, area, command, seeds, status, exit_code,
                         pass_rate, output_tail, code_sha, hidden_passed,
                         hidden_total, hidden_pass_rate, started_at, finished_at,
                         duration_ms, system)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        values["actor"],
                        values["task_id"],
                        values["area"],
                        values["command"],
                        values["seeds"],
                        values["status"],
                        values["exit_code"],
                        values["pass_rate"],
                        values["output_tail"],
                        values["code_sha"],
                        values["hidden_passed"],
                        values["hidden_total"],
                        values["hidden_pass_rate"],
                        values["started_at"],
                        values["finished_at"],
                        values["duration_ms"],
                    ),
                )
                run_id = int(cursor.lastrowid)
        record = self.get(run_id)
        if record is None:  # pragma: no cover - защита от гонки
            raise NotFoundError(f"Прогон #{run_id} не сохранился.")
        return record

    def get(self, run_id: int) -> RunRecord | None:
        """Найти прогон по id."""
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            raise ValidationError(
                "run_id должен быть целым числом.",
                hint="Возьмите id из brain run.",
            )
        with self.store._lock:
            self.store._ensure_open()
            row = self.store._conn.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        return self._row_to_run(row) if row is not None else None

    def list(
        self,
        *,
        actor: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        """Последние прогоны с фильтрами."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValidationError(
                "limit должен быть от 1 до 1000.",
                hint="Передайте limit=50.",
            )
        clauses: list[str] = []
        params: list[Any] = []
        if actor is not None:
            clauses.append("actor=?")
            params.append(actor)
        if task_id is not None:
            clauses.append("task_id=?")
            params.append(task_id)
        sql = "SELECT * FROM runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.store._lock:
            self.store._ensure_open()
            rows = self.store._conn.execute(sql, params).fetchall()
        return [self._row_to_run(row) for row in rows]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=int(row["id"]),
            actor=row["actor"],
            task_id=row["task_id"],
            area=row["area"],
            command=row["command"],
            seeds=int(row["seeds"]),
            status=row["status"],
            exit_code=row["exit_code"],
            pass_rate=float(row["pass_rate"]),
            output_tail=row["output_tail"] or "",
            code_sha=row["code_sha"],
            hidden_passed=int(row["hidden_passed"]),
            hidden_total=int(row["hidden_total"]),
            hidden_pass_rate=float(row["hidden_pass_rate"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            duration_ms=row["duration_ms"],
            system=bool(row["system"]),
        )


_IGNORE_PATHS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".venv",
    "node_modules",
}
