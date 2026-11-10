"""Песочница скриптов Artek Brain (ТЗ 3.7).

Скрипт (Python или shell) выполняется через bubblewrap с выключенной сетью,
минимальным окружением и лимитами CPU/RAM/времени/процессов/вывода. Данные
мозга доступны только как явно выданный read-only срез в /data; результат
возвращается из stdout/stderr и каталога /out. Без bwrap запуск отклоняется,
тихого запуска без изоляции нет.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from . import secrets as secret_scanner
from .access import Access, Principal
from .errors import ArtekBrainError, PermissionDenied, ValidationError
from .nav import Nav

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_LANGUAGES = {
    "python": ("py", "python"),
    "py": ("py", "python"),
    "python3": ("py", "python"),
    "shell": ("sh", "shell"),
    "sh": ("sh", "shell"),
    "bash": ("sh", "shell"),
}
_ROLE_RANK = {"agent": 1, "human": 2, "owner": 3}

# Небольшой автономный хелпер, который копируется в песочницу как brainlib.py.
_BRAINLIB_SOURCE = '''"""Чтение read-only среза Artek Brain из /data."""

from __future__ import annotations

import json
import os
from pathlib import Path

DATA_ROOT = Path("/data")
MANIFEST = DATA_ROOT / ".slice.json"


def manifest():
    """Метаданные выданного среза (пути, версии, усечение)."""
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"documents": [], "truncated": False}


def documents():
    """Список логических путей документов в срезе."""
    result = []
    if not DATA_ROOT.exists():
        return result
    for base, dirs, files in os.walk(DATA_ROOT):
        dirs[:] = [item for item in dirs if not item.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            full = Path(base) / name
            result.append(str(full.relative_to(DATA_ROOT)).replace(os.sep, "/"))
    return sorted(result)


def _resolve(path):
    rel = str(path).strip().replace("\\\\", "/").lstrip("/")
    parts = [item for item in rel.split("/") if item not in ("", ".")]
    if not rel or any(item == ".." for item in parts):
        raise ValueError("Путь должен быть относительным и без ..")
    target = (DATA_ROOT / "/".join(parts)).resolve()
    root = DATA_ROOT.resolve()
    if target != root and root not in target.parents:
        raise ValueError("Путь выходит за пределы среза")
    return target


def read(path):
    """Прочитать документ из среза как текст UTF-8."""
    return _resolve(path).read_text(encoding="utf-8")


def find(text, limit=50):
    """Найти подстроку в документах среза: список (path, line, text)."""
    needle = str(text).lower()
    found = []
    for name in documents():
        try:
            lines = read(name).splitlines()
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, 1):
            if needle in line.lower():
                found.append((name, number, line))
                if len(found) >= int(limit):
                    return found
    return found


def search(text, limit=50):
    """Псевдоним find для удобства."""
    return find(text, limit=limit)


__all__ = ["DATA_ROOT", "manifest", "documents", "read", "find", "search"]
'''


class SandboxError(ArtekBrainError):
    """Ошибка песочницы."""


class SandboxUnavailable(SandboxError):
    """Нет обязательного компонента изоляции (bwrap/prlimit)."""


class SandboxBusy(SandboxError):
    """Очередь запусков переполнена или запуск не завершился вовремя."""


class SandboxNotFound(SandboxError):
    """Запуск с таким id не найден."""


@dataclass(frozen=True)
class SandboxLimits:
    """Лимиты одного запуска."""

    cpu_seconds: int = 5
    wall_seconds: float = 10.0
    memory_bytes: int = 256 * 1024 * 1024
    max_processes: int = 32
    max_output_bytes: int = 64 * 1024
    max_file_bytes: int = 1024 * 1024
    max_out_bytes: int = 1024 * 1024
    max_files: int = 32
    max_slice_documents: int = 100
    max_slice_bytes: int = 1024 * 1024
    max_script_bytes: int = 256 * 1024

    def as_dict(self) -> dict[str, Any]:
        """Представить лимиты словарём."""
        return asdict(self)


def _agent_limits() -> SandboxLimits:
    return SandboxLimits(
        cpu_seconds=10,
        wall_seconds=20.0,
        memory_bytes=512 * 1024 * 1024,
        max_processes=32,
        max_output_bytes=128 * 1024,
        max_file_bytes=2 * 1024 * 1024,
        max_out_bytes=2 * 1024 * 1024,
        max_files=32,
        max_slice_documents=200,
        max_slice_bytes=5 * 1024 * 1024,
        max_script_bytes=512 * 1024,
    )


def _human_limits() -> SandboxLimits:
    agent = _agent_limits()
    return replace(
        agent,
        cpu_seconds=30,
        wall_seconds=60.0,
        memory_bytes=1024 * 1024 * 1024,
        max_processes=64,
        max_output_bytes=256 * 1024,
        max_file_bytes=8 * 1024 * 1024,
        max_out_bytes=8 * 1024 * 1024,
        max_files=64,
        max_slice_documents=500,
        max_slice_bytes=20 * 1024 * 1024,
        max_script_bytes=1024 * 1024,
    )


def _owner_limits() -> SandboxLimits:
    human = _human_limits()
    return replace(
        human,
        cpu_seconds=60,
        wall_seconds=120.0,
        memory_bytes=2 * 1024 * 1024 * 1024,
        max_processes=128,
        max_output_bytes=1024 * 1024,
        max_file_bytes=32 * 1024 * 1024,
        max_out_bytes=32 * 1024 * 1024,
        max_files=128,
        max_slice_documents=1000,
        max_slice_bytes=50 * 1024 * 1024,
        max_script_bytes=2 * 1024 * 1024,
    )


@dataclass(frozen=True)
class SandboxConfig:
    """Настройки песочницы: пути, очередь и пределы по ролям."""

    bwrap: str = "/usr/bin/bwrap"
    prlimit: str = "/usr/bin/prlimit"
    python: str = "/usr/bin/python3"
    shell: str = "/bin/sh"
    default_limits: SandboxLimits = field(default_factory=SandboxLimits)
    role_ceilings: Mapping[str, SandboxLimits] = field(
        default_factory=lambda: {
            "agent": _agent_limits(),
            "human": _human_limits(),
            "owner": _owner_limits(),
        }
    )
    max_concurrent: int = 2
    max_concurrent_per_token: int = 1
    queue_timeout_seconds: float = 30.0
    monitor_interval_seconds: float = 0.03

    @classmethod
    def default(cls) -> "SandboxConfig":
        """Конфигурация по умолчанию."""
        return cls()

    def ceiling(self, role: str) -> SandboxLimits:
        """Потолок лимитов для роли."""
        return self.role_ceilings.get(role, self.default_limits)

    def resolve_limits(
        self, role: str, requested: Mapping[str, Any] | None
    ) -> SandboxLimits:
        """Лимиты запуска: значения по умолчанию, но не выше потолка роли."""
        base = self.default_limits
        if requested:
            if not isinstance(requested, Mapping):
                raise ValidationError(
                    "limits должен быть объектом.",
                    hint="Например: {\"wall_seconds\": 5, \"memory_bytes\": 67108864}.",
                )
            values: dict[str, Any] = {}
            for key in base.as_dict():
                if key not in requested or requested[key] is None:
                    continue
                raw = requested[key]
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    raise ValidationError(
                        f"limit «{key}» должен быть числом.",
                        hint="Передайте положительное число.",
                    )
                if raw <= 0:
                    raise ValidationError(
                        f"limit «{key}» должен быть больше 0.",
                        hint="Передайте положительное число.",
                    )
                values[key] = raw
            base = replace(base, **values)
        cap = self.ceiling(role)
        merged = {}
        for key, value in base.as_dict().items():
            merged[key] = min(value, getattr(cap, key))
        # wall_seconds остаётся float, остальные целые.
        merged["wall_seconds"] = float(merged["wall_seconds"])
        for key in merged:
            if key != "wall_seconds":
                merged[key] = int(merged[key])
        return SandboxLimits(**merged)


@dataclass
class _Slice:
    """Подготовленный read-only срез."""

    directory: Path
    data_dir: Path
    lib_dir: Path
    entries: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    skipped: int = 0
    total_bytes: int = 0

    def fingerprint(self) -> str:
        """Отпечаток состава среза (путь+версия)."""
        parts = [f"{item['path']}@{item['version']}" for item in self.entries]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


@dataclass
class _Job:
    """Состояние запуска в очереди."""

    id: str
    token_id: int
    actor: str
    language: str
    script_sha256: str
    created_at: float
    status: str = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: BaseException | None = None
    cancel_requested: bool = False
    proc: subprocess.Popen[bytes] | None = None
    script_path: str | None = None
    script_redacted: bool = False
    slice_paths: list[str] = field(default_factory=list)
    event: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict[str, Any]:
        """Публичное состояние без токена и внутренних объектов."""
        data: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "language": self.language,
            "script_sha256": self.script_sha256,
            "script_path": self.script_path,
            "slice": list(self.slice_paths),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancel_requested": self.cancel_requested,
        }
        if self.result is not None:
            data["result"] = copy.deepcopy(self.result)
        if self.error is not None:
            data["error"] = {
                "type": type(self.error).__name__,
                "message": str(self.error),
            }
        return data


class Sandbox:
    """Песочница скриптов поверх Store и Access."""

    def __init__(
        self,
        config: SandboxConfig | None = None,
        store: Any | None = None,
        access: Access | None = None,
    ) -> None:
        self.config = config or SandboxConfig()
        self.store = store
        self.access = access
        self._jobs: dict[str, _Job] = {}
        self._jobs_lock = threading.RLock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_lock = threading.RLock()
        self._host_sem = threading.BoundedSemaphore(max(1, self.config.max_concurrent))
        self._token_sems: dict[int, threading.BoundedSemaphore] = {}
        self._token_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Проверки прав и окружения
    # ------------------------------------------------------------------
    def _verify(self, token: str) -> Principal:
        if self.access is None:
            raise ValidationError(
                "Песочница не подключена к Access.",
                hint="Создайте Sandbox(config, store, access).",
            )
        return self.access.verify(token or "")

    def _require_tool(self, principal: Principal) -> None:
        role = principal.role
        if role == "auditor" or _ROLE_RANK.get(role, 0) < _ROLE_RANK["agent"]:
            exc = PermissionDenied(
                "sandbox.run",
                None,
                "agent role",
                actor=principal.name,
                reason="песочница доступна ролям agent, human и owner; auditor — нет",
            )
            self._log_failure(principal.name, exc)
            raise exc

    def _check_runtime(self, language: str) -> None:
        if not (os.path.isfile(self.config.bwrap) and os.access(self.config.bwrap, os.X_OK)):
            raise SandboxUnavailable(
                f"Изоляция недоступна: не найден bwrap «{self.config.bwrap}».",
                hint=(
                    "Установите bubblewrap и проверьте путь. Запуск без изоляции "
                    "запрещён."
                ),
            )
        if not (
            os.path.isfile(self.config.prlimit)
            and os.access(self.config.prlimit, os.X_OK)
        ):
            raise SandboxUnavailable(
                f"Не найден prlimit «{self.config.prlimit}» для лимитов.",
                hint="Установите util-linux (prlimit) или укажите путь в SandboxConfig.",
            )
        interpreter = self._interpreter(language)
        if not (os.path.isfile(interpreter) and os.access(interpreter, os.X_OK)):
            raise SandboxUnavailable(
                f"Не найден интерпретатор «{interpreter}».",
                hint="Укажите python/shell в SandboxConfig.",
            )

    def _interpreter(self, language: str) -> str:
        if language == "python":
            return self.config.python
        return self.config.shell

    @staticmethod
    def _normalize_language(language: str) -> str:
        key = str(language or "").strip().lower()
        if key not in _LANGUAGES:
            raise ValidationError(
                f"Неизвестный язык «{language}».",
                hint="Поддерживаются python и shell (sh/bash).",
            )
        return _LANGUAGES[key][1]

    # ------------------------------------------------------------------
    # Аудит
    # ------------------------------------------------------------------
    def _audit_ok(
        self, actor: str | None, path: str | None, detail: dict[str, Any]
    ) -> None:
        if self.store is None:
            return
        try:
            with self.store._lock:
                self.store._ensure_open()
                with self.store._conn:
                    self.store._audit_ok(actor, "sandbox_run", path, detail)
        except Exception:
            pass

    def _log_failure(self, actor: str | None, exc: BaseException) -> None:
        if self.store is None or not isinstance(exc, ArtekBrainError):
            return
        try:
            with self.store._lock:
                if self.store._closed:
                    return
                with self.store._conn:
                    self.store._audit_row(
                        actor,
                        "sandbox_run",
                        None,
                        {
                            "ok": False,
                            "error": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Срез данных
    # ------------------------------------------------------------------
    def _prepare_slice(
        self,
        principal: Principal,
        inputs: Any,
        limits: SandboxLimits,
    ) -> _Slice:
        if self.store is None:
            raise ValidationError(
                "Песочница не подключена к Store.",
                hint="Создайте Sandbox(config, store, access).",
            )
        if inputs is None:
            inputs = {}
        elif isinstance(inputs, (str, list, tuple)):
            inputs = {"paths": inputs}
        if not isinstance(inputs, Mapping):
            raise ValidationError(
                "inputs должен быть объектом.",
                hint=(
                    "Например: {\"prefix\": \"docs\"} или "
                    "{\"paths\": [\"docs/a.md\"]}."
                ),
            )

        prefixes: list[str] = []
        explicit: list[str] = []
        raw_paths = inputs.get("paths")
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        if isinstance(raw_paths, (list, tuple)):
            for item in raw_paths:
                if not isinstance(item, str) or not item.strip():
                    raise ValidationError(
                        "Каждый путь во inputs.paths должен быть непустой строкой.",
                        hint="Например: paths=[\"docs/a.md\"].",
                    )
                value = item.strip()
                if value.endswith("/"):
                    prefixes.append(value.rstrip("/"))
                else:
                    explicit.append(value)
        elif raw_paths is not None:
            raise ValidationError(
                "inputs.paths должен быть строкой или списком строк.",
                hint="Например: paths=[\"docs/a.md\"].",
            )
        raw_prefix = inputs.get("prefix")
        if raw_prefix is not None:
            if not isinstance(raw_prefix, str):
                raise ValidationError(
                    "inputs.prefix должен быть строкой.",
                    hint="Например: prefix=\"docs\".",
                )
            prefixes.append(raw_prefix.rstrip("/"))

        max_docs = self._input_int(
            inputs, "max_documents", limits.max_slice_documents,
            limits.max_slice_documents,
        )
        max_bytes = self._input_int(
            inputs, "max_bytes", limits.max_slice_bytes, limits.max_slice_bytes,
        )

        normalized_prefixes: list[str] = []
        for prefix in prefixes:
            value = "" if not prefix else self.store._normalize_path(prefix)
            if value and principal.role != "owner" and not principal.can_read(value):
                exc = PermissionDenied(
                    "sandbox.slice",
                    value,
                    "read",
                    actor=principal.name,
                    reason="нет права read на запрошенный префикс среза",
                )
                self._log_failure(principal.name, exc)
                raise exc
            normalized_prefixes.append(value)

        explicit_paths: list[str] = []
        for path in explicit:
            value = self.store._normalize_path(path)
            try:
                if self.access is not None:
                    self.access.require_read(
                        principal, value, action="sandbox.slice"
                    )
            except PermissionDenied as exc:
                self._log_failure(principal.name, exc)
                raise
            explicit_paths.append(value)

        documents = Nav(self.store)._documents()
        by_path = {item["path"]: item for item in documents}
        selected: dict[str, dict[str, Any]] = {}

        for path in explicit_paths:
            doc = by_path.get(path)
            if doc is not None:
                selected[path] = doc

        for prefix in normalized_prefixes:
            for doc in documents:
                path = doc["path"]
                if prefix and not (path == prefix or path.startswith(prefix + "/")):
                    continue
                if principal.role != "owner" and not principal.can_read(path):
                    continue
                selected[path] = doc

        directory = Path(tempfile.mkdtemp(prefix="artek-sandbox-"))
        data_dir = directory / "data"
        lib_dir = directory / "lib"
        data_dir.mkdir(mode=0o755)
        lib_dir.mkdir(mode=0o755)
        try:
            (lib_dir / "brainlib.py").write_text(
                _BRAINLIB_SOURCE, encoding="utf-8"
            )
            result = _Slice(
                directory=directory,
                data_dir=data_dir,
                lib_dir=lib_dir,
            )
            for path in sorted(selected):
                if len(result.entries) >= max_docs:
                    result.truncated = True
                    break
                try:
                    read = self.store.read(path, actor=principal.name)
                except ArtekBrainError:
                    result.skipped += 1
                    continue
                content = read.content
                size = len(content.encode("utf-8"))
                if result.total_bytes + size > max_bytes:
                    result.truncated = True
                    continue
                target = data_dir / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                result.entries.append(
                    {
                        "path": path,
                        "version": read.version,
                        "size": size,
                    }
                )
                result.total_bytes += size
            manifest = {
                "documents": result.entries,
                "truncated": result.truncated,
                "total_bytes": result.total_bytes,
                "skipped": result.skipped,
            }
            (data_dir / ".slice.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return result
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    @staticmethod
    def _input_int(
        inputs: Mapping[str, Any], key: str, default: int, ceiling: int
    ) -> int:
        if key not in inputs or inputs[key] is None:
            return default
        value = inputs[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValidationError(
                f"inputs.{key} должен быть целым числом больше 0.",
                hint=f"Например: {key}=20.",
            )
        return min(value, ceiling)

    # ------------------------------------------------------------------
    # Публичный запуск
    # ------------------------------------------------------------------
    def run(
        self,
        script: str,
        language: str = "python",
        token: str = "",
        inputs: Any = None,
        *,
        limits: Mapping[str, Any] | None = None,
        cache: bool = False,
    ) -> dict[str, Any]:
        """Выполнить скрипт и дождаться результата."""
        job_id = self.submit(
            script,
            language,
            token,
            inputs=inputs,
            limits=limits,
            cache=cache,
        )
        return self.wait(job_id)

    def submit(
        self,
        script: str,
        language: str = "python",
        token: str = "",
        inputs: Any = None,
        *,
        limits: Mapping[str, Any] | None = None,
        cache: bool = False,
    ) -> str:
        """Поставить запуск в очередь и вернуть id (статус — status(id))."""
        if not isinstance(script, str) or not script.strip():
            raise ValidationError(
                "script обязателен.",
                hint="Передайте непустой текст скрипта.",
            )
        normalized = self._normalize_language(language)
        principal = self._verify(token)
        self._require_tool(principal)
        self._check_runtime(normalized)
        effective = self.config.resolve_limits(principal.role, limits)
        raw = script.encode("utf-8")
        if len(raw) > effective.max_script_bytes:
            raise ValidationError(
                f"Скрипт больше лимита {effective.max_script_bytes} байт.",
                hint="Сократите скрипт или попросите роль с большим потолком.",
            )
        script_sha = hashlib.sha256(raw).hexdigest()
        try:
            slice_ = self._prepare_slice(principal, inputs, effective)
        except ArtekBrainError:
            raise
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "script": script_sha,
                    "language": normalized,
                    "slice": slice_.fingerprint(),
                    "limits": effective.as_dict(),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        job = _Job(
            id=uuid4().hex[:16],
            token_id=principal.id,
            actor=principal.name,
            language=normalized,
            script_sha256=script_sha,
            created_at=time.time(),
            slice_paths=[item["path"] for item in slice_.entries],
        )
        job.created_at = time.time()
        with self._jobs_lock:
            self._jobs[job.id] = job

        if cache:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
            if cached is not None:
                result = copy.deepcopy(cached)
                result["id"] = job.id
                result["cached"] = True
                job.result = result
                job.status = "done"
                job.finished_at = time.time()
                job.event.set()
                shutil.rmtree(slice_.directory, ignore_errors=True)
                self._audit_ok(
                    principal.name,
                    job.script_path,
                    self._audit_detail(
                        job, slice_, result, limits=effective, cached=True
                    ),
                )
                return job.id

        thread = threading.Thread(
            target=self._run_job,
            args=(job, script, normalized, effective, slice_, cache_key, bool(cache)),
            name=f"sandbox-{job.id}",
            daemon=True,
        )
        thread.start()
        return job.id

    def wait(self, job_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Дождаться запуска и вернуть результат (или ошибку)."""
        job = self._get_job(job_id)
        if not job.event.wait(timeout):
            raise SandboxBusy(
                f"Запуск {job_id} ещё не завершился.",
                hint="Увеличьте таймаут или проверьте status.",
            )
        if job.error is not None:
            raise job.error
        if job.result is None:
            raise SandboxError(
                f"Запуск {job_id} завершился без результата.",
                hint="Проверьте status и журнал аудита.",
            )
        return copy.deepcopy(job.result)

    def status(self, token: str, job_id: str) -> dict[str, Any]:
        """Состояние запуска по id (свой запуск или owner)."""
        principal = self._verify(token)
        job = self._get_job(job_id)
        if principal.role != "owner" and principal.id != job.token_id:
            raise PermissionDenied(
                "sandbox.status",
                job_id,
                "owner",
                actor=principal.name,
                reason="чужой запуск песочницы не виден",
            )
        return job.public()

    def cancel(self, token: str, job_id: str) -> dict[str, Any]:
        """Отменить запуск по id и убить всю группу процессов."""
        principal = self._verify(token)
        job = self._get_job(job_id)
        if principal.role != "owner" and principal.id != job.token_id:
            raise PermissionDenied(
                "sandbox.cancel",
                job_id,
                "owner",
                actor=principal.name,
                reason="чужой запуск песочницы нельзя отменить",
            )
        with self._jobs_lock:
            if job.status in ("done", "error", "cancelled", "timeout"):
                return {
                    "id": job.id,
                    "cancelled": False,
                    "status": job.status,
                }
            job.cancel_requested = True
            proc = job.proc
        if proc is not None:
            _killpg(proc.pid, signal.SIGKILL)
        return {
            "id": job.id,
            "cancelled": True,
            "status": job.status,
        }

    def shutdown(self) -> None:
        """Отменить все незавершённые запуски при остановке сервера."""
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.status in ("queued", "running"):
                with self._jobs_lock:
                    job.cancel_requested = True
                    proc = job.proc
                if proc is not None:
                    _killpg(proc.pid, signal.SIGKILL)

    def _get_job(self, job_id: str) -> _Job:
        with self._jobs_lock:
            job = self._jobs.get(str(job_id or ""))
        if job is None:
            raise SandboxNotFound(
                f"Запуск «{job_id}» не найден.",
                hint="Возьмите id из результата sandbox run.",
            )
        return job

    # ------------------------------------------------------------------
    # Выполнение
    # ------------------------------------------------------------------
    def _token_semaphore(self, token_id: int) -> threading.BoundedSemaphore:
        with self._token_lock:
            sem = self._token_sems.get(token_id)
            if sem is None:
                sem = threading.BoundedSemaphore(
                    max(1, self.config.max_concurrent_per_token)
                )
                self._token_sems[token_id] = sem
            return sem

    def _run_job(
        self,
        job: _Job,
        script: str,
        language: str,
        limits: SandboxLimits,
        slice_: _Slice,
        cache_key: str,
        cache: bool,
    ) -> None:
        token_sem = self._token_semaphore(job.token_id)
        host_acquired = False
        token_acquired = False
        started = time.monotonic()
        try:
            token_acquired = token_sem.acquire(
                timeout=self.config.queue_timeout_seconds
            )
            if not token_acquired:
                raise SandboxBusy(
                    "Очередь запусков этого токена переполнена.",
                    hint="Дождитесь завершения текущего запуска.",
                )
            host_acquired = self._host_sem.acquire(
                timeout=self.config.queue_timeout_seconds
            )
            if not host_acquired:
                raise SandboxBusy(
                    "Очередь песочницы на хосте переполнена.",
                    hint="Повторите позже или уменьшите параллельность.",
                )
            with self._jobs_lock:
                if job.cancel_requested:
                    result = self._empty_result(job, language, slice_, limits)
                    result.update({"cancelled": True, "status": "cancelled"})
                    job.result = result
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    self._audit_ok(
                        job.actor,
                        job.script_path,
                        self._audit_detail(
                            job, slice_, result, limits=limits, cached=False
                        ),
                    )
                    return
                job.status = "running"
                job.started_at = time.time()

            script_path = self._store_script(job, script)
            job.script_path = script_path
            result = self._execute(job, script, language, limits, slice_)
            job.result = result
            job.status = result["status"]
            with self._cache_lock:
                if cache and not result.get("cancelled"):
                    self._cache[cache_key] = copy.deepcopy(result)
            self._audit_ok(
                job.actor,
                script_path,
                self._audit_detail(
                    job, slice_, result, limits=limits, cached=False
                ),
            )
        except BaseException as exc:  # сохранить ошибку для wait/status
            job.error = exc
            job.status = "error"
            self._log_failure(job.actor, exc)
        finally:
            job.finished_at = job.finished_at or time.time()
            if host_acquired:
                self._host_sem.release()
            if token_acquired:
                token_sem.release()
            shutil.rmtree(slice_.directory, ignore_errors=True)
            job.event.set()

    def _store_script(self, job: _Job, script: str) -> str | None:
        if self.store is None:
            return None
        ext = "py" if job.language == "python" else "sh"
        path = f"runs/sandbox/{job.id}/script.{ext}"
        redacted = bool(secret_scanner.scan(script))
        job.script_redacted = redacted
        content = secret_scanner.redact(script) if redacted else script
        try:
            self.store.write(
                path,
                content,
                job.actor,
                f"скрипт песочницы {job.id}",
            )
        except ArtekBrainError as exc:
            self._log_failure(job.actor, exc)
            raise
        return path

    def _execute(
        self,
        job: _Job,
        script: str,
        language: str,
        limits: SandboxLimits,
        slice_: _Slice,
    ) -> dict[str, Any]:
        started = time.monotonic()
        out_dir = Path(tempfile.mkdtemp(prefix="artek-out-"))
        tmp_size = min(
            max(16 * 1024 * 1024, limits.max_file_bytes * 2),
            128 * 1024 * 1024,
        )
        command = self._command(language, limits, slice_, out_dir, tmp_size)
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONPATH": "/opt/brainlib",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=env,
                close_fds=True,
            )
        except OSError as exc:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise SandboxUnavailable(
                f"Не удалось запустить bwrap: {exc}.",
                hint="Проверьте bwrap и права пользователя.",
            ) from exc
        with self._jobs_lock:
            job.proc = proc

        out_buf: list[bytes] = []
        err_buf: list[bytes] = []
        out_state = {"total": 0, "truncated": False}
        err_state = {"total": 0, "truncated": False}
        readers = [
            threading.Thread(
                target=_read_stream,
                args=(
                    proc.stdout,
                    limits.max_output_bytes,
                    out_buf,
                    out_state,
                ),
                daemon=True,
            ),
            threading.Thread(
                target=_read_stream,
                args=(
                    proc.stderr,
                    limits.max_output_bytes,
                    err_buf,
                    err_state,
                ),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        try:
            proc.stdin.write(script.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        deadline = started + float(limits.wall_seconds)
        peak_memory = 0
        reason: str | None = None
        while True:
            code = proc.poll()
            if code is not None:
                break
            now = time.monotonic()
            with self._jobs_lock:
                cancelled = job.cancel_requested
            if cancelled:
                reason = "cancelled"
                _killpg(proc.pid, signal.SIGKILL)
                break
            if now >= deadline:
                reason = "timeout"
                _killpg(proc.pid, signal.SIGKILL)
                break
            count, rss = _process_group_stats(proc.pid)
            peak_memory = max(peak_memory, rss)
            if count > limits.max_processes:
                reason = "processes"
                _killpg(proc.pid, signal.SIGKILL)
                break
            out_size, out_files = _directory_usage(out_dir)
            if out_size > limits.max_out_bytes or out_files > limits.max_files:
                reason = "output"
                _killpg(proc.pid, signal.SIGKILL)
                break
            time.sleep(max(0.005, self.config.monitor_interval_seconds))

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _killpg(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for reader in readers:
            reader.join(timeout=5)
        with self._jobs_lock:
            job.proc = None

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = b"".join(out_buf).decode("utf-8", errors="replace")
        stderr = b"".join(err_buf).decode("utf-8", errors="replace")
        files, files_truncated = _collect_out_files(
            out_dir, limits.max_out_bytes, limits.max_files
        )
        shutil.rmtree(out_dir, ignore_errors=True)
        exit_code = proc.returncode
        cancelled = reason == "cancelled"
        timed_out = reason == "timeout"
        if cancelled:
            status = "cancelled"
        elif timed_out or reason in ("processes", "output"):
            status = "timeout" if timed_out else "error"
        elif exit_code == 0:
            status = "done"
        else:
            status = "error"
        return {
            "id": job.id,
            "status": status,
            "language": job.language,
            "script_sha256": job.script_sha256,
            "script_path": job.script_path,
            "script_redacted": job.script_redacted,
            "exit_code": exit_code,
            "signal": -exit_code if exit_code is not None and exit_code < 0 else None,
            "cancelled": cancelled,
            "timed_out": timed_out,
            "limit_reason": reason,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_bytes": out_state["total"],
            "stderr_bytes": err_state["total"],
            "stdout_truncated": out_state["truncated"],
            "stderr_truncated": err_state["truncated"],
            "out": files,
            "out_truncated": files_truncated,
            "duration_ms": duration_ms,
            "peak_memory_bytes": peak_memory,
            "limits": limits.as_dict(),
            "slice": {
                "paths": [item["path"] for item in slice_.entries],
                "versions": {
                    item["path"]: item["version"] for item in slice_.entries
                },
                "bytes": slice_.total_bytes,
                "truncated": slice_.truncated,
                "skipped": slice_.skipped,
            },
            "cached": False,
        }

    def _empty_result(
        self,
        job: _Job,
        language: str,
        slice_: _Slice,
        limits: SandboxLimits,
    ) -> dict[str, Any]:
        return {
            "id": job.id,
            "status": "cancelled",
            "language": language,
            "script_sha256": job.script_sha256,
            "script_path": job.script_path,
            "script_redacted": job.script_redacted,
            "exit_code": None,
            "signal": None,
            "stdout": "",
            "stderr": "",
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "out": [],
            "out_truncated": False,
            "duration_ms": 0,
            "peak_memory_bytes": 0,
            "limits": limits.as_dict(),
            "slice": {
                "paths": [item["path"] for item in slice_.entries],
                "versions": {
                    item["path"]: item["version"] for item in slice_.entries
                },
                "bytes": slice_.total_bytes,
                "truncated": slice_.truncated,
                "skipped": slice_.skipped,
            },
        }

    def _command(
        self,
        language: str,
        limits: SandboxLimits,
        slice_: _Slice,
        out_dir: Path,
        tmp_size: int,
    ) -> list[str]:
        interpreter = self._interpreter(language)
        bwrap: list[str] = [
            self.config.bwrap,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/usr",
            "/usr",
        ]
        for path in ("/bin", "/sbin", "/lib", "/lib64"):
            if os.path.exists(path):
                bwrap += ["--ro-bind", path, path]
        for path in ("/etc/alternatives", "/etc/ld.so.cache", "/etc/localtime"):
            if os.path.exists(path):
                bwrap += ["--ro-bind", path, path]
        bwrap += [
            "--ro-bind",
            str(slice_.data_dir),
            "/data",
            "--ro-bind",
            str(slice_.lib_dir),
            "/opt/brainlib",
            "--bind",
            str(out_dir),
            "/out",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--size",
            str(int(tmp_size)),
            "--tmpfs",
            "/tmp",
            "--remount-ro",
            "/",
            "--chdir",
            "/tmp",
            "--",
            interpreter,
        ]
        if language == "python":
            bwrap.append("-")
        else:
            bwrap.append("-s")
        prefix = [
            self.config.prlimit,
            f"--cpu={int(limits.cpu_seconds)}",
            f"--as={int(limits.memory_bytes)}",
            f"--nproc={int(limits.max_processes) + 8}",
            f"--fsize={int(limits.max_file_bytes)}",
            "--core=0",
            "--",
        ]
        return prefix + bwrap

    def _audit_detail(
        self,
        job: _Job,
        slice_: _Slice,
        result: Mapping[str, Any],
        *,
        limits: SandboxLimits,
        cached: bool,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "language": job.language,
            "script_sha256": job.script_sha256,
            "script_path": job.script_path,
            "script_redacted": job.script_redacted,
            "slice": {
                "paths": [item["path"] for item in slice_.entries],
                "versions": {
                    item["path"]: item["version"] for item in slice_.entries
                },
                "truncated": slice_.truncated,
                "skipped": slice_.skipped,
                "bytes": slice_.total_bytes,
            },
            "exit_code": result.get("exit_code"),
            "timed_out": result.get("timed_out", False),
            "cancelled": result.get("cancelled", False),
            "limit_reason": result.get("limit_reason"),
            "duration_ms": result.get("duration_ms"),
            "stdout_bytes": result.get("stdout_bytes"),
            "stderr_bytes": result.get("stderr_bytes"),
            "out_files": len(result.get("out") or []),
            "peak_memory_bytes": result.get("peak_memory_bytes"),
            "cached": cached,
            "limits": limits.as_dict(),
        }


# ----------------------------------------------------------------------
# Низкоуровневые помощники
# ----------------------------------------------------------------------
def _read_stream(
    stream: Any,
    limit: int,
    sink: list[bytes],
    state: dict[str, Any],
) -> None:
    if stream is None:
        return
    kept = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            state["total"] += len(chunk)
            if kept < limit:
                take = chunk[: limit - kept]
                sink.append(take)
                kept += len(take)
                if len(take) < len(chunk):
                    state["truncated"] = True
            else:
                state["truncated"] = True
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _process_group_stats(pgid: int) -> tuple[int, int]:
    """Число процессов в группе и суммарный RSS (байты)."""
    count = 0
    rss = 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 0, 0
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        try:
            tail = raw[raw.rindex(b")") + 2 :].split()
            if int(tail[2]) != pgid:
                continue
        except (ValueError, IndexError):
            continue
        count += 1
        try:
            with open(f"/proc/{entry}/status", "rb") as handle:
                for line in handle:
                    if line.startswith(b"VmRSS:"):
                        rss += int(line.split()[1]) * 1024
                        break
        except (OSError, ValueError, IndexError):
            pass
    return count, rss


def _directory_usage(path: Path) -> tuple[int, int]:
    size = 0
    count = 0
    try:
        for item in path.rglob("*"):
            try:
                info = item.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                size += info.st_size
                count += 1
    except OSError:
        pass
    return size, count


def _collect_out_files(
    directory: Path, max_bytes: int, max_files: int
) -> tuple[list[dict[str, Any]], bool]:
    """Прочитать обычные файлы /out, не следуя симлинкам."""
    result: list[dict[str, Any]] = []
    remaining = int(max_bytes)
    truncated = False
    try:
        entries = sorted(directory.rglob("*"), key=lambda item: str(item))
    except OSError:
        return result, False
    for entry in entries:
        try:
            info = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if len(result) >= max_files or remaining <= 0:
            truncated = True
            break
        name = str(entry.relative_to(directory)).replace(os.sep, "/")
        take = min(info.st_size, remaining)
        try:
            fd = os.open(entry, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            continue
        try:
            data = os.read(fd, take)
        finally:
            os.close(fd)
        item: dict[str, Any] = {"name": name, "size": info.st_size}
        if take < info.st_size:
            item["truncated"] = True
            truncated = True
        try:
            item["content"] = data.decode("utf-8")
            item["encoding"] = "utf-8"
        except UnicodeDecodeError:
            import base64

            item["content"] = base64.b64encode(data).decode("ascii")
            item["encoding"] = "base64"
        result.append(item)
        remaining -= len(data)
    return result, truncated
