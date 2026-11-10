"""Ядро хранилища Artek Brain: SQLite для метаданных, блобы по sha256.

В модуле нет сети и вызовов LLM. Все операции детерминированы.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import threading
import unicodedata
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from . import secrets as secret_scanner
from .errors import (
    ArtekBrainError,
    BaseVersionRequiredError,
    ConflictError,
    DeletedError,
    InvalidVersionError,
    NotDeletedError,
    NotFoundError,
    StoredDataError,
    ValidationError,
)
from .quota import Quota

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_DAY = timedelta(days=1)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    path TEXT PRIMARY KEY,
    current_version INTEGER,
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    n INTEGER NOT NULL,
    parent_n INTEGER,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    author TEXT NOT NULL,
    reason TEXT,
    ts TEXT NOT NULL,
    batch_id TEXT,
    op TEXT NOT NULL,
    UNIQUE(path, n),
    FOREIGN KEY (path) REFERENCES documents(path) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_versions_path_n ON versions(path, n);
CREATE INDEX IF NOT EXISTS idx_versions_author_ts ON versions(author, ts);
CREATE INDEX IF NOT EXISTS idx_versions_batch ON versions(batch_id);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    path TEXT,
    detail_json TEXT,
    session TEXT,
    model TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit(actor);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit(action);
"""


@dataclass(frozen=True)
class VersionInfo:
    """Метаданные версии документа."""

    path: str
    n: int
    parent_n: int | None
    sha256: str
    size: int
    author: str
    reason: str | None
    ts: str
    batch_id: str | None
    op: str

    def as_dict(self) -> dict[str, Any]:
        """Представить версию словарём."""
        return {
            "path": self.path,
            "n": self.n,
            "parent_n": self.parent_n,
            "sha256": self.sha256,
            "size": self.size,
            "author": self.author,
            "reason": self.reason,
            "ts": self.ts,
            "batch_id": self.batch_id,
            "op": self.op,
        }


@dataclass(frozen=True)
class WriteResult:
    """Результат записи/удаления/отката."""

    path: str
    version: int
    parent_version: int | None
    sha256: str
    size: int
    op: str
    created: bool = False
    merged: bool = False
    changed: bool = True
    batch_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Представить результат словарём."""
        return {
            "path": self.path,
            "version": self.version,
            "parent_version": self.parent_version,
            "sha256": self.sha256,
            "size": self.size,
            "op": self.op,
            "created": self.created,
            "merged": self.merged,
            "changed": self.changed,
            "batch_id": self.batch_id,
        }


@dataclass(frozen=True)
class ReadResult:
    """Результат чтения с признаком усечения."""

    path: str
    version: int
    content: str
    truncated: bool
    total_chars: int
    total_lines: int
    section: str | None = None
    line_range: tuple[int, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Представить результат словарём."""
        return {
            "path": self.path,
            "version": self.version,
            "content": self.content,
            "truncated": self.truncated,
            "total_chars": self.total_chars,
            "total_lines": self.total_lines,
            "section": self.section,
            "line_range": self.line_range,
        }


@dataclass(frozen=True)
class SectionBlame:
    """Кто последним менял раздел Markdown."""

    title: str
    level: int
    version: int
    author: str
    ts: str

    def as_dict(self) -> dict[str, Any]:
        """Представить запись словарём."""
        return {
            "title": self.title,
            "level": self.level,
            "version": self.version,
            "author": self.author,
            "ts": self.ts,
        }


@dataclass(frozen=True)
class RollbackSkipped:
    """Документ, который откат не тронул, и причина."""

    path: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        """Представить запись словарём."""
        return {"path": self.path, "reason": self.reason}


@dataclass(frozen=True)
class RollbackReport:
    """Итог массового отката."""

    rolled_back: list[WriteResult] = field(default_factory=list)
    skipped: list[RollbackSkipped] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Представить отчёт словарём."""
        return {
            "rolled_back": [item.as_dict() for item in self.rolled_back],
            "skipped": [item.as_dict() for item in self.skipped],
        }


@dataclass(frozen=True)
class AuditEntry:
    """Строка журнала аудита."""

    id: int
    ts: str
    actor: str | None
    action: str
    path: str | None
    detail: dict[str, Any]
    session: str | None
    model: str | None

    def as_dict(self) -> dict[str, Any]:
        """Представить запись словарём."""
        return {
            "id": self.id,
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "path": self.path,
            "detail": self.detail,
            "session": self.session,
            "model": self.model,
        }


@dataclass(frozen=True)
class GcResult:
    """Итог сборки мусора блобов."""

    scanned: int
    removed: int
    freed_bytes: int

    def as_dict(self) -> dict[str, Any]:
        """Представить результат словарём."""
        return {
            "scanned": self.scanned,
            "removed": self.removed,
            "freed_bytes": self.freed_bytes,
        }


@dataclass(frozen=True)
class _Section:
    key: str
    title: str
    level: int
    text: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_ts(value: str | datetime | None, *, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        raw = str(value).strip()
        if not raw:
            raise ValidationError(
                f"Пустое значение {name}.",
                hint=f"Передайте {name} в ISO-формате, например 2026-01-31T10:00:00Z.",
            )
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(
                f"Не удалось разобрать дату {name}={raw!r}.",
                hint="Передайте дату в ISO-формате, например 2026-01-31T10:00:00Z.",
            ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return _format_ts(moment)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _split_sections(text: str) -> list[_Section]:
    """Разбить Markdown на разделы по заголовкам, не трогая блоки кода."""
    sections: list[_Section] = []
    lines = text.splitlines(keepends=True)
    if not lines:
        return sections
    current_title: str | None = None
    current_level = 0
    current_lines: list[str] = []
    occurrences: dict[tuple[int, str], int] = {}
    fence: str | None = None

    def flush() -> None:
        nonlocal current_lines, current_title
        if not current_lines:
            return
        if current_title is None:
            sections.append(
                _Section("__preamble__", "", 0, "".join(current_lines))
            )
        else:
            index = occurrences[(current_level, current_title)]
            key = f"{current_level}:{current_title}:{index}"
            sections.append(
                _Section(key, current_title, current_level, "".join(current_lines))
            )
        current_lines = []

    for line in lines:
        stripped = line.strip()
        marker = stripped[:3]
        if marker in ("```", "~~~"):
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
        match = None if fence else _HEADING_RE.match(line.rstrip("\r\n"))
        if match:
            flush()
            current_level = len(match.group(1))
            current_title = match.group(2).strip()
            occurrences[(current_level, current_title)] = (
                occurrences.get((current_level, current_title), -1) + 1
            )
            current_lines = [line]
        else:
            current_lines.append(line)
    flush()
    return sections


def _diff_text(
    old: str, new: str, path: str, old_label: int | str, new_label: int | str
) -> str:
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"{path}@{old_label}",
            tofile=f"{path}@{new_label}",
            lineterm="",
        )
    )


def _section_label(section: _Section) -> str:
    return section.title or "preamble"


def _changed_section_labels(old: str, new: str) -> list[str]:
    old_map = {section.key: section.text for section in _split_sections(old)}
    seen: list[str] = []
    for section in _split_sections(new):
        if old_map.get(section.key) != section.text:
            label = _section_label(section)
            if label not in seen:
                seen.append(label)
    for section in _split_sections(old):
        if section.key not in {
            item.key for item in _split_sections(new)
        }:
            label = _section_label(section)
            if label not in seen:
                seen.append(label)
    return seen


def _merge_three_way(base: str, current: str, new: str) -> tuple[str, list[str]]:
    """Склеить разные разделы; вернуть (текст, список конфликтных разделов)."""
    base_map = {section.key: section for section in _split_sections(base)}
    current_map = {section.key: section for section in _split_sections(current)}
    new_map = {section.key: section for section in _split_sections(new)}

    order: list[str] = []
    for source in (new, current, base):
        for section in _split_sections(source):
            if section.key not in order:
                order.append(section.key)

    chosen: dict[str, str | None] = {}
    conflicts: list[str] = []
    for key in order:
        base_text = base_map[key].text if key in base_map else None
        current_text = current_map[key].text if key in current_map else None
        new_text = new_map[key].text if key in new_map else None
        current_changed = current_text != base_text
        new_changed = new_text != base_text
        if current_changed and new_changed:
            if current_text == new_text:
                chosen[key] = current_text
            else:
                chosen[key] = current_text
                section = current_map.get(key) or new_map.get(key) or base_map[key]
                label = _section_label(section)
                if label not in conflicts:
                    conflicts.append(label)
        elif new_changed:
            chosen[key] = new_text
        elif current_changed:
            chosen[key] = current_text
        else:
            chosen[key] = (
                current_text
                if current_text is not None
                else (base_text if base_text is not None else new_text)
            )

    if conflicts:
        return "", conflicts
    return "".join(chosen[key] for key in order if chosen[key] is not None), []


def _select_section(text: str, section: str) -> str:
    sections = _split_sections(text)
    target = section.strip()
    if target.lower() in ("preamble", "__preamble__"):
        target = ""
    else:
        target = target.lstrip("#").strip()
    match_index: int | None = None
    for index, item in enumerate(sections):
        if item.title == target and (item.title or target == ""):
            match_index = index
            break
    if match_index is None:
        for index, item in enumerate(sections):
            if item.title and item.title.lower() == target.lower():
                match_index = index
                break
    if match_index is None:
        available = [item.title for item in sections if item.title]
        listing = ", ".join(f"«{title}»" for title in available) or "нет заголовков"
        raise NotFoundError(
            f"Раздел «{section}» не найден. Доступные разделы: {listing}.",
            hint="Передайте section с точным заголовком без символов #.",
        )
    level = sections[match_index].level
    parts: list[str] = []
    for index in range(match_index, len(sections)):
        item = sections[index]
        if index > match_index and item.title and item.level <= level:
            break
        parts.append(item.text)
    return "".join(parts)


class Store:
    """Хранилище документов: версии, блобы, аудит, откаты и квоты."""

    def __init__(self, path: str | os.PathLike[str], *, quota: Quota | None = None) -> None:
        self.root = Path(path)
        if self.root.exists() and not self.root.is_dir():
            raise ValidationError(
                f"Путь «{self.root}» существует и не является каталогом.",
                hint="Передайте путь к каталогу хранилища или новый путь.",
            )
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs_dir = self.root / "blobs"
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "brain.sqlite3"
        self.quota = quota or Quota()
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level="DEFERRED",
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------
    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def close(self) -> None:
        """Закрыть соединение с базой (повторный вызов безопасен)."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValidationError(
                "Хранилище закрыто.",
                hint="Создайте новый Store(path) или не вызывайте close() до конца работы.",
            )

    def _normalize_path(self, path: str) -> str:
        if not isinstance(path, str):
            raise ValidationError(
                "path должен быть строкой.",
                hint="Передайте логический путь, например docs/README.md.",
            )
        value = path.strip()
        if not value:
            raise ValidationError(
                "Пустой path.",
                hint="Передайте логический путь, например docs/README.md.",
            )
        value = unicodedata.normalize("NFKC", value)
        if value.startswith("/") or "\\" in value or "\x00" in value:
            raise ValidationError(
                f"Недопустимый path {path!r}.",
                hint="Путь должен быть относительным и через /, без .. и обратных слэшей.",
            )
        if any(ord(char) < 32 for char in value):
            raise ValidationError(
                f"Недопустимый path {path!r}.",
                hint="Уберите управляющие символы из пути.",
            )
        parts = value.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValidationError(
                f"Недопустимый path {path!r}.",
                hint="Уберите пустые сегменты, . и .. из пути.",
            )
        if any(part.endswith((" ", ".")) for part in parts):
            raise ValidationError(
                f"Недопустимый path {path!r}.",
                hint="Сегменты пути не должны заканчиваться пробелом или точкой.",
            )
        if len(value) > 1024:
            raise ValidationError(
                "Слишком длинный path.",
                hint="Сократите путь до 1024 символов.",
            )
        return "/".join(parts)

    def _require_text(self, content: str) -> None:
        if not isinstance(content, str):
            raise ValidationError(
                "content должен быть строкой UTF-8.",
                hint="Передайте текст (str), а не bytes или другой объект.",
            )

    def _validate_author(self, author: str) -> None:
        if not isinstance(author, str) or not author.strip():
            raise ValidationError(
                "author обязателен.",
                hint="Передайте непустую строку автора, например agent-1.",
            )
        if len(author) > 200:
            raise ValidationError(
                "Слишком длинный author.",
                hint="Сократите имя автора до 200 символов.",
            )

    def _validate_reason(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError(
                "reason обязателен.",
                hint="Передайте причину правки, например «уточнил раздел про квоты».",
            )
        if len(reason) > 2000:
            raise ValidationError(
                "Слишком длинный reason.",
                hint="Сократите причину до 2000 символов.",
            )

    def _validate_batch(self, batch_id: str | None) -> None:
        if batch_id is None:
            return
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ValidationError(
                "batch_id должен быть непустой строкой или None.",
                hint="Передайте метку пачки, например import-2026-01-31.",
            )
        if len(batch_id) > 200:
            raise ValidationError(
                "Слишком длинный batch_id.",
                hint="Сократите batch_id до 200 символов.",
            )

    def _get_doc_locked(self, path: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM documents WHERE path=?", (path,)
        ).fetchone()

    def _version_row_locked(self, path: str, n: int) -> sqlite3.Row:
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise InvalidVersionError(
                f"Некорректная версия {n!r}.",
                hint="Версия — целое число от 1 до текущей версии документа.",
            )
        row = self._conn.execute(
            "SELECT * FROM versions WHERE path=? AND n=?", (path, n)
        ).fetchone()
        if row is None:
            doc = self._get_doc_locked(path)
            current = doc["current_version"] if doc else "?"
            raise InvalidVersionError(
                f"Версия {n} документа «{path}» не найдена (текущая: {current}).",
                hint=f"Передайте version от 1 до {current}.",
            )
        return row

    @staticmethod
    def _version_info(row: sqlite3.Row) -> VersionInfo:
        return VersionInfo(
            path=row["path"],
            n=row["n"],
            parent_n=row["parent_n"],
            sha256=row["sha256"],
            size=row["size"],
            author=row["author"],
            reason=row["reason"],
            ts=row["ts"],
            batch_id=row["batch_id"],
            op=row["op"],
        )

    def _blob_path(self, sha: str) -> Path:
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise StoredDataError(
                f"Некорректный sha256 {sha!r} в метаданных.",
                hint="Запустите gc() и проверьте целостность базы.",
            )
        return self.blobs_dir / sha[:2] / sha[2:]

    def _put_blob(self, data: bytes) -> tuple[str, int]:
        sha = _sha256(data)
        target = self._blob_path(sha)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f"{target.name}.tmp-{uuid4().hex}")
            temp.write_bytes(zlib.compress(data, 6))
            os.replace(temp, target)
        return sha, len(data)

    def _read_blob(self, sha: str) -> str:
        target = self._blob_path(sha)
        try:
            compressed = target.read_bytes()
        except FileNotFoundError as exc:
            raise StoredDataError(
                f"Блоб {sha[:12]}… не найден на диске.",
                hint="Запустите gc() или восстановите каталог blobs из резервной копии.",
            ) from exc
        try:
            data = zlib.decompress(compressed)
        except zlib.error as exc:
            raise StoredDataError(
                f"Блоб {sha[:12]}… повреждён (zlib).",
                hint="Восстановите каталог blobs из резервной копии.",
            ) from exc
        if _sha256(data) != sha:
            raise StoredDataError(
                f"Блоб {sha[:12]}… не совпал по sha256.",
                hint="Данные повреждены; восстановите каталог blobs из резервной копии.",
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StoredDataError(
                f"Блоб {sha[:12]}… не является UTF-8 текстом.",
                hint="Проверьте, что документ был записан как текст.",
            ) from exc

    def _check_versions_quota_locked(self, path: str) -> None:
        used = self._conn.execute(
            "SELECT COUNT(*) FROM versions WHERE path=?", (path,)
        ).fetchone()[0]
        self.quota.check_versions(int(used), path)

    def _check_daily_quota_locked(self, author: str) -> None:
        start = _utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + _DAY
        used = self._conn.execute(
            "SELECT COUNT(*) FROM versions WHERE author=? AND ts>=? AND ts<?",
            (author, _format_ts(start), _format_ts(end)),
        ).fetchone()[0]
        self.quota.check_daily_writes(int(used), author)

    def _insert_version(
        self,
        path: str,
        n: int,
        parent_n: int | None,
        sha256: str,
        size: int,
        author: str,
        reason: str,
        ts: str,
        batch_id: str | None,
        op: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO versions
                (path, n, parent_n, sha256, size, author, reason, ts, batch_id, op)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                path,
                n,
                parent_n,
                sha256,
                size,
                author,
                secret_scanner.redact(reason),
                ts,
                batch_id,
                op,
            ),
        )

    def _audit_row(
        self,
        actor: str | None,
        action: str,
        path: str | None,
        detail: dict[str, Any] | None,
        session: str | None = None,
        model: str | None = None,
        ts: str | None = None,
    ) -> None:
        payload = secret_scanner.sanitize_json(detail or {})
        self._conn.execute(
            """
            INSERT INTO audit (ts, actor, action, path, detail_json, session, model)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts or _format_ts(_utc_now()), actor or "anonymous", action, path, payload, session, model),
        )

    def _log_failure(
        self,
        action: str,
        path: str | None,
        actor: str | None,
        exc: ArtekBrainError,
        session: str | None,
        model: str | None,
    ) -> None:
        detail = {
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
        }
        try:
            with self._lock:
                if self._closed:
                    return
                with self._conn:
                    self._audit_row(actor, action, path, detail, session, model)
        except (sqlite3.Error, ValueError):
            pass

    def _audit_ok(
        self,
        actor: str | None,
        action: str,
        path: str | None,
        detail: dict[str, Any] | None,
        session: str | None = None,
        model: str | None = None,
    ) -> None:
        self._audit_row(actor, action, path, {"ok": True, **(detail or {})}, session, model)

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------
    def write(
        self,
        path: str,
        content: str,
        author: str,
        reason: str,
        base_version: int | None = None,
        expected_sha256: str | None = None,
        batch_id: str | None = None,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> WriteResult:
        """Атомарно записать новую версию документа.

        Для нового документа base_version должен быть None. Для существующего
        base_version обязателен. Если base_version старше текущей версии,
        выполняется трёхсторонняя склейка по разделам Markdown, а при правках
        в одном разделе поднимается ConflictError.
        """
        return self._run_write(
            path,
            content,
            author,
            reason,
            base_version,
            expected_sha256,
            batch_id,
            action="write",
            session=session,
            model=model,
        )

    def _run_write(
        self,
        path: str,
        content: str,
        author: str,
        reason: str,
        base_version: int | None,
        expected_sha256: str | None,
        batch_id: str | None,
        *,
        action: str,
        session: str | None,
        model: str | None,
    ) -> WriteResult:
        safe_path = path if isinstance(path, str) else None
        safe_author = author if isinstance(author, str) else None
        try:
            path = self._normalize_path(path)
            safe_path = path
            self._require_text(content)
            self._validate_author(author)
            self._validate_reason(reason)
            self._validate_batch(batch_id)
            if expected_sha256 is not None and not isinstance(expected_sha256, str):
                raise ValidationError(
                    "expected_sha256 должен быть строкой.",
                    hint="Передайте sha256 текущей версии или None.",
                )
            secret_scanner.assert_clean(content, path)
            data = content.encode("utf-8")
            self.quota.check_document_size(len(data), path)
            with self._lock:
                self._ensure_open()
                with self._conn:
                    result = self._write_locked(
                        path,
                        content,
                        author,
                        reason,
                        base_version,
                        expected_sha256,
                        batch_id,
                    )
                    self._audit_ok(
                        author,
                        action,
                        path,
                        {
                            "version": result.version,
                            "parent_version": result.parent_version,
                            "op": result.op,
                            "sha256": result.sha256,
                            "size": result.size,
                            "merged": result.merged,
                            "changed": result.changed,
                            "batch_id": batch_id,
                            "reason": reason,
                        },
                        session,
                        model,
                    )
                    return result
        except ArtekBrainError as exc:
            self._log_failure(action, safe_path, safe_author, exc, session, model)
            raise

    def _write_locked(
        self,
        path: str,
        content: str,
        author: str,
        reason: str,
        base_version: int | None,
        expected_sha256: str | None,
        batch_id: str | None,
    ) -> WriteResult:
        doc = self._get_doc_locked(path)
        ts = _format_ts(_utc_now())
        merged = False
        if doc is None:
            if base_version is not None:
                raise InvalidVersionError(
                    f"Документ «{path}» не существует, base_version={base_version} передавать нельзя.",
                    hint="Для нового документа передайте base_version=None.",
                )
            if expected_sha256 is not None:
                raise ValidationError(
                    f"Документ «{path}» не существует, expected_sha256 передавать нельзя.",
                    hint="Для нового документа передайте expected_sha256=None.",
                )
            current_n = 0
            parent_n: int | None = None
            op = "create"
            final_text = content
        else:
            if doc["deleted"]:
                raise DeletedError(
                    f"Документ «{path}» удалён, запись запрещена.",
                    hint=f"Сначала вызовите restore(path={path!r}, author=..., reason=...).",
                )
            current_n = int(doc["current_version"])
            if base_version is None:
                raise BaseVersionRequiredError(
                    f"Для существующего документа «{path}» (версия {current_n}) обязателен base_version.",
                    hint=f"Прочитайте read(path={path!r}) и передайте base_version={current_n}.",
                )
            if not isinstance(base_version, int) or isinstance(base_version, bool):
                raise InvalidVersionError(
                    f"base_version должен быть целым числом, получено {base_version!r}.",
                    hint="Передайте номер версии из read(path).",
                )
            if base_version < 1 or base_version > current_n:
                raise InvalidVersionError(
                    f"base_version={base_version} вне диапазона 1..{current_n} для «{path}».",
                    hint=f"Передайте base_version от 1 до {current_n}.",
                )
            current = self._version_info(self._version_row_locked(path, current_n))
            current_text = self._read_blob(current.sha256)
            if expected_sha256 is not None and expected_sha256 != current.sha256:
                raise ConflictError(
                    path,
                    current_n,
                    diff=_diff_text(current_text, content, path, current_n, "new"),
                    conflicting_sections=_changed_section_labels(current_text, content),
                )
            if current.sha256 == _sha256(content.encode("utf-8")):
                return WriteResult(
                    path=path,
                    version=current_n,
                    parent_version=current.parent_n,
                    sha256=current.sha256,
                    size=current.size,
                    op="noop",
                    created=False,
                    merged=False,
                    changed=False,
                    batch_id=batch_id,
                )
            if base_version == current_n:
                final_text = content
                parent_n = current_n
                op = "write"
            else:
                base = self._version_info(self._version_row_locked(path, base_version))
                if base.sha256 == current.sha256:
                    final_text = content
                    parent_n = current_n
                    op = "write"
                else:
                    base_text = self._read_blob(base.sha256)
                    final_text, conflicts = _merge_three_way(base_text, current_text, content)
                    if conflicts:
                        raise ConflictError(
                            path,
                            current_n,
                            diff=_diff_text(current_text, content, path, current_n, "new"),
                            conflicting_sections=conflicts,
                        )
                    parent_n = current_n
                    op = "merge"
                    merged = True

        self._check_versions_quota_locked(path)
        self._check_daily_quota_locked(author)
        data = final_text.encode("utf-8")
        self.quota.check_document_size(len(data), path)
        secret_scanner.assert_clean(final_text, path)
        sha, size = self._put_blob(data)
        new_n = 1 if doc is None else current_n + 1
        if doc is None:
            self._conn.execute(
                "INSERT INTO documents (path, current_version, deleted, created_at) VALUES (?, NULL, 0, ?)",
                (path, ts),
            )
        self._insert_version(
            path, new_n, parent_n, sha, size, author, reason, ts, batch_id, op
        )
        self._conn.execute(
            "UPDATE documents SET current_version=?, deleted=0 WHERE path=?",
            (new_n, path),
        )
        return WriteResult(
            path=path,
            version=new_n,
            parent_version=parent_n,
            sha256=sha,
            size=size,
            op=op,
            created=doc is None,
            merged=merged,
            changed=True,
            batch_id=batch_id,
        )

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------
    def read(
        self,
        path: str,
        version: int | None = None,
        section: str | None = None,
        line_range: Sequence[int] | None = None,
        max_chars: int | None = None,
        *,
        actor: str | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> ReadResult:
        """Прочитать документ (или версию/раздел/диапазон строк) с усечением."""
        safe_path = path if isinstance(path, str) else None
        try:
            path = self._normalize_path(path)
            safe_path = path
            if max_chars is not None:
                if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
                    raise ValidationError(
                        "max_chars должен быть положительным целым числом.",
                        hint="Передайте max_chars=None или число больше 0.",
                    )
            normalized_range: tuple[int, int] | None = None
            if line_range is not None:
                if (
                    not isinstance(line_range, (tuple, list))
                    or len(line_range) != 2
                    or not all(
                        isinstance(item, int) and not isinstance(item, bool)
                        for item in line_range
                    )
                ):
                    raise ValidationError(
                        "line_range должен быть парой целых чисел (start, end).",
                        hint="Передайте line_range=(1, 50), нумерация с 1.",
                    )
                start, end = int(line_range[0]), int(line_range[1])
                if start < 1 or end < start:
                    raise ValidationError(
                        f"Некорректный line_range=({start}, {end}).",
                        hint="Передайте start >= 1 и end >= start.",
                    )
                normalized_range = (start, end)
            with self._lock:
                self._ensure_open()
                with self._conn:
                    doc = self._get_doc_locked(path)
                    if doc is None:
                        raise NotFoundError(
                            f"Документ «{path}» не найден.",
                            hint="Проверьте путь или создайте документ через write().",
                        )
                    if doc["deleted"]:
                        raise DeletedError(
                            f"Документ «{path}» удалён.",
                            hint=f"Вызовите restore(path={path!r}, author=..., reason=...), чтобы вернуть его.",
                        )
                    target_n = (
                        int(doc["current_version"]) if version is None else version
                    )
                    info = self._version_info(
                        self._version_row_locked(path, int(target_n))
                    )
                    full_text = self._read_blob(info.sha256)
                    selected = (
                        _select_section(full_text, section)
                        if section is not None
                        else full_text
                    )
                    if normalized_range is not None:
                        lines = selected.splitlines(keepends=True)
                        selected = "".join(lines[normalized_range[0] - 1 : normalized_range[1]])
                    total_chars = len(selected)
                    total_lines = len(selected.splitlines())
                    truncated = False
                    if max_chars is not None and total_chars > max_chars:
                        selected = selected[:max_chars]
                        truncated = True
                    self._audit_ok(
                        actor,
                        "read",
                        path,
                        {
                            "version": info.n,
                            "section": section,
                            "line_range": normalized_range,
                            "max_chars": max_chars,
                            "truncated": truncated,
                            "chars": len(selected),
                        },
                        session,
                        model,
                    )
                    return ReadResult(
                        path=path,
                        version=info.n,
                        content=selected,
                        truncated=truncated,
                        total_chars=total_chars,
                        total_lines=total_lines,
                        section=section,
                        line_range=normalized_range,
                    )
        except ArtekBrainError as exc:
            self._log_failure("read", safe_path, actor, exc, session, model)
            raise

    def history(
        self,
        path: str,
        limit: int = 50,
        offset: int = 0,
        *,
        actor: str | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> list[VersionInfo]:
        """История версий, новые сверху."""
        safe_path = path if isinstance(path, str) else None
        try:
            path = self._normalize_path(path)
            safe_path = path
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000:
                raise ValidationError(
                    "limit должен быть целым числом от 1 до 5000.",
                    hint="Передайте limit=50 (или другое число в диапазоне).",
                )
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise ValidationError(
                    "offset должен быть целым числом >= 0.",
                    hint="Передайте offset=0 для первой страницы.",
                )
            with self._lock:
                self._ensure_open()
                with self._conn:
                    if self._get_doc_locked(path) is None:
                        raise NotFoundError(
                            f"Документ «{path}» не найден.",
                            hint="Проверьте путь.",
                        )
                    rows = self._conn.execute(
                        "SELECT * FROM versions WHERE path=? ORDER BY n DESC LIMIT ? OFFSET ?",
                        (path, limit, offset),
                    ).fetchall()
                    result = [self._version_info(row) for row in rows]
                    self._audit_ok(
                        actor,
                        "history",
                        path,
                        {"limit": limit, "offset": offset, "count": len(result)},
                        session,
                        model,
                    )
                    return result
        except ArtekBrainError as exc:
            self._log_failure("history", safe_path, actor, exc, session, model)
            raise

    def diff(
        self,
        path: str,
        a: int | None,
        b: int | None,
        *,
        actor: str | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> str:
        """Unified diff между версиями (None означает текущую)."""
        safe_path = path if isinstance(path, str) else None
        try:
            path = self._normalize_path(path)
            safe_path = path
            with self._lock:
                self._ensure_open()
                with self._conn:
                    doc = self._get_doc_locked(path)
                    if doc is None:
                        raise NotFoundError(
                            f"Документ «{path}» не найден.",
                            hint="Проверьте путь.",
                        )
                    left_n = int(doc["current_version"]) if a is None else a
                    right_n = int(doc["current_version"]) if b is None else b
                    left = self._version_info(self._version_row_locked(path, int(left_n)))
                    right = self._version_info(self._version_row_locked(path, int(right_n)))
                    text = _diff_text(
                        self._read_blob(left.sha256),
                        self._read_blob(right.sha256),
                        path,
                        left.n,
                        right.n,
                    )
                    self._audit_ok(
                        actor,
                        "diff",
                        path,
                        {"a": left.n, "b": right.n, "lines": len(text.splitlines())},
                        session,
                        model,
                    )
                    return text
        except ArtekBrainError as exc:
            self._log_failure("diff", safe_path, actor, exc, session, model)
            raise

    def blame(
        self,
        path: str,
        *,
        actor: str | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> list[SectionBlame]:
        """Кто последним менял каждый раздел текущей версии."""
        safe_path = path if isinstance(path, str) else None
        try:
            path = self._normalize_path(path)
            safe_path = path
            with self._lock:
                self._ensure_open()
                with self._conn:
                    doc = self._get_doc_locked(path)
                    if doc is None:
                        raise NotFoundError(
                            f"Документ «{path}» не найден.",
                            hint="Проверьте путь.",
                        )
                    rows = self._conn.execute(
                        "SELECT * FROM versions WHERE path=? ORDER BY n ASC", (path,)
                    ).fetchall()
                    blamed: dict[str, SectionBlame] = {}
                    previous: dict[str, _Section] = {}
                    for row in rows:
                        info = self._version_info(row)
                        current = {
                            section.key: section
                            for section in _split_sections(self._read_blob(info.sha256))
                        }
                        for key, section in current.items():
                            old = previous.get(key)
                            if (
                                old is None
                                or old.text != section.text
                                or old.title != section.title
                                or old.level != section.level
                            ):
                                blamed[key] = SectionBlame(
                                    title=section.title or "preamble",
                                    level=section.level,
                                    version=info.n,
                                    author=info.author,
                                    ts=info.ts,
                                )
                        previous = current
                    current_text = self._read_blob(
                        self._version_info(
                            self._version_row_locked(path, int(doc["current_version"]))
                        ).sha256
                    )
                    result = [
                        blamed[section.key]
                        for section in _split_sections(current_text)
                        if section.key in blamed
                    ]
                    self._audit_ok(
                        actor,
                        "blame",
                        path,
                        {"sections": len(result)},
                        session,
                        model,
                    )
                    return result
        except ArtekBrainError as exc:
            self._log_failure("blame", safe_path, actor, exc, session, model)
            raise

    # ------------------------------------------------------------------
    # Удаление и откаты
    # ------------------------------------------------------------------
    def delete(
        self,
        path: str,
        author: str,
        reason: str,
        base_version: int | None = None,
        batch_id: str | None = None,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> WriteResult:
        """Мягко удалить документ, сохранив историю."""
        safe_path = path if isinstance(path, str) else None
        safe_author = author if isinstance(author, str) else None
        try:
            path = self._normalize_path(path)
            safe_path = path
            self._validate_author(author)
            self._validate_reason(reason)
            self._validate_batch(batch_id)
            with self._lock:
                self._ensure_open()
                with self._conn:
                    doc = self._get_doc_locked(path)
                    if doc is None:
                        raise NotFoundError(
                            f"Документ «{path}» не найден.",
                            hint="Проверьте путь.",
                        )
                    if doc["deleted"]:
                        raise DeletedError(
                            f"Документ «{path}» уже удалён.",
                            hint=f"Вызовите restore(path={path!r}, author=..., reason=...).",
                        )
                    current_n = int(doc["current_version"])
                    current = self._version_info(
                        self._version_row_locked(path, current_n)
                    )
                    if base_version is not None:
                        if not isinstance(base_version, int) or isinstance(base_version, bool):
                            raise InvalidVersionError(
                                "base_version должен быть целым числом.",
                                hint="Передайте base_version из read(path) или None.",
                            )
                        if base_version != current_n:
                            raise ConflictError(
                                path,
                                current_n,
                                diff="",
                                conflicting_sections=[],
                                hint=f"Перечитайте документ и передайте base_version={current_n}.",
                            )
                    self._check_versions_quota_locked(path)
                    self._check_daily_quota_locked(author)
                    ts = _format_ts(_utc_now())
                    new_n = current_n + 1
                    self._insert_version(
                        path,
                        new_n,
                        current_n,
                        current.sha256,
                        current.size,
                        author,
                        reason,
                        ts,
                        batch_id,
                        "delete",
                    )
                    self._conn.execute(
                        "UPDATE documents SET current_version=?, deleted=1 WHERE path=?",
                        (new_n, path),
                    )
                    result = WriteResult(
                        path=path,
                        version=new_n,
                        parent_version=current_n,
                        sha256=current.sha256,
                        size=current.size,
                        op="delete",
                        batch_id=batch_id,
                    )
                    self._audit_ok(
                        author,
                        "delete",
                        path,
                        {"version": new_n, "base_version": base_version, "reason": reason},
                        session,
                        model,
                    )
                    return result
        except ArtekBrainError as exc:
            self._log_failure("delete", safe_path, safe_author, exc, session, model)
            raise

    def restore(
        self,
        path: str,
        author: str,
        reason: str,
        base_version: int | None = None,
        batch_id: str | None = None,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> WriteResult:
        """Восстановить мягко удалённый документ."""
        safe_path = path if isinstance(path, str) else None
        safe_author = author if isinstance(author, str) else None
        try:
            path = self._normalize_path(path)
            safe_path = path
            self._validate_author(author)
            self._validate_reason(reason)
            self._validate_batch(batch_id)
            with self._lock:
                self._ensure_open()
                with self._conn:
                    doc = self._get_doc_locked(path)
                    if doc is None:
                        raise NotFoundError(
                            f"Документ «{path}» не найден.",
                            hint="Проверьте путь.",
                        )
                    if not doc["deleted"]:
                        raise NotDeletedError(
                            f"Документ «{path}» не удалён, восстанавливать нечего.",
                            hint="Передайте путь к удалённому документу.",
                        )
                    current_n = int(doc["current_version"])
                    current = self._version_info(
                        self._version_row_locked(path, current_n)
                    )
                    if base_version is not None and base_version != current_n:
                        raise ConflictError(
                            path,
                            current_n,
                            diff="",
                            conflicting_sections=[],
                            hint=f"Передайте base_version={current_n} или None.",
                        )
                    self._check_versions_quota_locked(path)
                    self._check_daily_quota_locked(author)
                    ts = _format_ts(_utc_now())
                    new_n = current_n + 1
                    self._insert_version(
                        path,
                        new_n,
                        current_n,
                        current.sha256,
                        current.size,
                        author,
                        reason,
                        ts,
                        batch_id,
                        "restore",
                    )
                    self._conn.execute(
                        "UPDATE documents SET current_version=?, deleted=0 WHERE path=?",
                        (new_n, path),
                    )
                    result = WriteResult(
                        path=path,
                        version=new_n,
                        parent_version=current_n,
                        sha256=current.sha256,
                        size=current.size,
                        op="restore",
                        batch_id=batch_id,
                    )
                    self._audit_ok(
                        author,
                        "restore",
                        path,
                        {"version": new_n, "reason": reason},
                        session,
                        model,
                    )
                    return result
        except ArtekBrainError as exc:
            self._log_failure("restore", safe_path, safe_author, exc, session, model)
            raise

    def rollback_version(
        self,
        path: str,
        to_version: int,
        author: str,
        reason: str,
        *,
        session: str | None = None,
        model: str | None = None,
        batch_id: str | None = None,
    ) -> WriteResult:
        """Создать новую версию со старым содержимым (история не стирается)."""
        safe_path = path if isinstance(path, str) else None
        safe_author = author if isinstance(author, str) else None
        try:
            path = self._normalize_path(path)
            safe_path = path
            self._validate_author(author)
            self._validate_reason(reason)
            self._validate_batch(batch_id)
            with self._lock:
                self._ensure_open()
                with self._conn:
                    doc = self._get_doc_locked(path)
                    if doc is None:
                        raise NotFoundError(
                            f"Документ «{path}» не найден.",
                            hint="Проверьте путь.",
                        )
                    if doc["deleted"]:
                        raise DeletedError(
                            f"Документ «{path}» удалён.",
                            hint="Сначала восстановите документ через restore().",
                        )
                    current_n = int(doc["current_version"])
                    current = self._version_info(
                        self._version_row_locked(path, current_n)
                    )
                    target = self._version_info(
                        self._version_row_locked(path, int(to_version))
                    )
                    if target.sha256 == current.sha256:
                        result = WriteResult(
                            path=path,
                            version=current_n,
                            parent_version=current.parent_n,
                            sha256=current.sha256,
                            size=current.size,
                            op="noop",
                            changed=False,
                            batch_id=batch_id,
                        )
                        self._audit_ok(
                            author,
                            "rollback_version",
                            path,
                            {
                                "version": current_n,
                                "to_version": target.n,
                                "changed": False,
                                "reason": reason,
                            },
                            session,
                            model,
                        )
                        return result
                    self._check_versions_quota_locked(path)
                    self._check_daily_quota_locked(author)
                    content = self._read_blob(target.sha256)
                    secret_scanner.assert_clean(content, path)
                    ts = _format_ts(_utc_now())
                    new_n = current_n + 1
                    self._insert_version(
                        path,
                        new_n,
                        current_n,
                        target.sha256,
                        target.size,
                        author,
                        reason,
                        ts,
                        batch_id,
                        "rollback",
                    )
                    self._conn.execute(
                        "UPDATE documents SET current_version=? WHERE path=?",
                        (new_n, path),
                    )
                    result = WriteResult(
                        path=path,
                        version=new_n,
                        parent_version=current_n,
                        sha256=target.sha256,
                        size=target.size,
                        op="rollback",
                        batch_id=batch_id,
                    )
                    self._audit_ok(
                        author,
                        "rollback_version",
                        path,
                        {"version": new_n, "to_version": target.n, "reason": reason},
                        session,
                        model,
                    )
                    return result
        except ArtekBrainError as exc:
            self._log_failure(
                "rollback_version", safe_path, safe_author, exc, session, model
            )
            raise

    def rollback_author(
        self,
        author: str,
        since: str | datetime | None,
        until: str | datetime | None,
        actor: str,
        reason: str,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> RollbackReport:
        """Откатить правки автора за период там, где после них не писали другие."""
        safe_author = author if isinstance(author, str) else None
        safe_actor = actor if isinstance(actor, str) else None
        try:
            self._validate_author(author)
            self._validate_author(actor)
            self._validate_reason(reason)
            since_iso = _parse_ts(since, name="since") or ""
            until_iso = _parse_ts(until, name="until") or "\uffff"
            if since_iso and until_iso != "\uffff" and since_iso > until_iso:
                raise ValidationError(
                    "since позже until.",
                    hint="Передайте since <= until.",
                )
            rolled_back: list[WriteResult] = []
            skipped: list[RollbackSkipped] = []
            with self._lock:
                self._ensure_open()
                with self._conn:
                    rows = self._conn.execute(
                        """
                        SELECT * FROM versions
                        WHERE author=? AND ts>=? AND ts<=?
                        ORDER BY path ASC, n ASC
                        """,
                        (author, since_iso, until_iso),
                    ).fetchall()
                    grouped: dict[str, list[sqlite3.Row]] = {}
                    for row in rows:
                        grouped.setdefault(row["path"], []).append(row)
                    for path, items in grouped.items():
                        doc = self._get_doc_locked(path)
                        if doc is None or doc["deleted"]:
                            skipped.append(
                                RollbackSkipped(path, "документ удалён или отсутствует")
                            )
                            continue
                        first = items[0]
                        last = items[-1]
                        current_n = int(doc["current_version"])
                        other = self._conn.execute(
                            """
                            SELECT 1 FROM versions
                            WHERE path=? AND n>=? AND n<=? AND author<>?
                            LIMIT 1
                            """,
                            (path, first["n"], current_n, author),
                        ).fetchone()
                        if other is not None:
                            skipped.append(
                                RollbackSkipped(
                                    path,
                                    "после начала правок автора писал другой автор",
                                )
                            )
                            continue
                        if last["n"] != current_n and first["n"] <= current_n:
                            skipped.append(
                                RollbackSkipped(
                                    path,
                                    "текущая версия новее правок автора",
                                )
                            )
                            continue
                        if first["parent_n"] is None:
                            skipped.append(
                                RollbackSkipped(path, "нет версии до правок автора")
                            )
                            continue
                        target = self._version_info(
                            self._version_row_locked(path, int(first["parent_n"]))
                        )
                        current = self._version_info(
                            self._version_row_locked(path, current_n)
                        )
                        if target.sha256 == current.sha256:
                            skipped.append(
                                RollbackSkipped(path, "содержимое уже совпадает")
                            )
                            continue
                        self._check_versions_quota_locked(path)
                        self._check_daily_quota_locked(actor)
                        content = self._read_blob(target.sha256)
                        secret_scanner.assert_clean(content, path)
                        ts = _format_ts(_utc_now())
                        new_n = current_n + 1
                        self._insert_version(
                            path,
                            new_n,
                            current_n,
                            target.sha256,
                            target.size,
                            actor,
                            reason,
                            ts,
                            None,
                            "rollback_author",
                        )
                        self._conn.execute(
                            "UPDATE documents SET current_version=? WHERE path=?",
                            (new_n, path),
                        )
                        rolled_back.append(
                            WriteResult(
                                path=path,
                                version=new_n,
                                parent_version=current_n,
                                sha256=target.sha256,
                                size=target.size,
                                op="rollback_author",
                            )
                        )
                    self._audit_ok(
                        actor,
                        "rollback_author",
                        None,
                        {
                            "author": author,
                            "since": since_iso,
                            "until": until_iso,
                            "rolled_back": [
                                {"path": item.path, "version": item.version}
                                for item in rolled_back
                            ],
                            "skipped": [item.as_dict() for item in skipped],
                            "reason": reason,
                        },
                        session,
                        model,
                    )
                    return RollbackReport(rolled_back=rolled_back, skipped=skipped)
        except ArtekBrainError as exc:
            self._log_failure(
                "rollback_author", None, safe_actor or safe_author, exc, session, model
            )
            raise

    def rollback_batch(
        self,
        batch_id: str,
        author: str | None = None,
        reason: str | None = None,
        *,
        actor: str | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> RollbackReport:
        """Откатить пачку правок по batch_id, если после неё не писали другие.

        Исполнитель задаётся author или actor (actor имеет приоритет).
        """
        performer = actor if actor is not None else author
        safe_author = performer if isinstance(performer, str) else None
        try:
            self._validate_batch(batch_id)
            if batch_id is None:
                raise ValidationError(
                    "batch_id обязателен для rollback_batch.",
                    hint="Передайте batch_id из write(..., batch_id=...).",
                )
            if reason is None:
                raise ValidationError(
                    "reason обязателен.",
                    hint="Передайте причину отката пачки.",
                )
            self._validate_author(performer)
            self._validate_reason(reason)
            rolled_back: list[WriteResult] = []
            skipped: list[RollbackSkipped] = []
            with self._lock:
                self._ensure_open()
                with self._conn:
                    rows = self._conn.execute(
                        "SELECT * FROM versions WHERE batch_id=? ORDER BY path ASC, n ASC",
                        (batch_id,),
                    ).fetchall()
                    if not rows:
                        raise NotFoundError(
                            f"Пачка «{batch_id}» не найдена.",
                            hint="Проверьте batch_id или список пачек в истории версий.",
                        )
                    grouped: dict[str, list[sqlite3.Row]] = {}
                    for row in rows:
                        grouped.setdefault(row["path"], []).append(row)
                    for path, items in grouped.items():
                        doc = self._get_doc_locked(path)
                        if doc is None or doc["deleted"]:
                            skipped.append(
                                RollbackSkipped(path, "документ удалён или отсутствует")
                            )
                            continue
                        first = items[0]
                        last = items[-1]
                        current_n = int(doc["current_version"])
                        foreign = self._conn.execute(
                            """
                            SELECT 1 FROM versions
                            WHERE path=? AND n>=? AND n<=?
                              AND (batch_id IS NULL OR batch_id<>?)
                            LIMIT 1
                            """,
                            (path, first["n"], last["n"], batch_id),
                        ).fetchone()
                        if foreign is not None:
                            skipped.append(
                                RollbackSkipped(
                                    path, "внутри пачки есть записи других пачек/авторов"
                                )
                            )
                            continue
                        after = self._conn.execute(
                            "SELECT 1 FROM versions WHERE path=? AND n>? LIMIT 1",
                            (path, last["n"]),
                        ).fetchone()
                        if after is not None:
                            skipped.append(
                                RollbackSkipped(path, "после пачки были другие записи")
                            )
                            continue
                        if first["parent_n"] is None:
                            skipped.append(
                                RollbackSkipped(path, "нет версии до пачки")
                            )
                            continue
                        target = self._version_info(
                            self._version_row_locked(path, int(first["parent_n"]))
                        )
                        current = self._version_info(
                            self._version_row_locked(path, current_n)
                        )
                        if target.sha256 == current.sha256:
                            skipped.append(
                                RollbackSkipped(path, "содержимое уже совпадает")
                            )
                            continue
                        if last["n"] != current_n:
                            # страховка: текущая версия должна быть концом пачки
                            skipped.append(
                                RollbackSkipped(path, "текущая версия не совпадает с концом пачки")
                            )
                            continue
                        self._check_versions_quota_locked(path)
                        self._check_daily_quota_locked(performer)
                        content = self._read_blob(target.sha256)
                        secret_scanner.assert_clean(content, path)
                        ts = _format_ts(_utc_now())
                        new_n = current_n + 1
                        self._insert_version(
                            path,
                            new_n,
                            current_n,
                            target.sha256,
                            target.size,
                            performer,
                            reason,
                            ts,
                            None,
                            "rollback_batch",
                        )
                        self._conn.execute(
                            "UPDATE documents SET current_version=? WHERE path=?",
                            (new_n, path),
                        )
                        rolled_back.append(
                            WriteResult(
                                path=path,
                                version=new_n,
                                parent_version=current_n,
                                sha256=target.sha256,
                                size=target.size,
                                op="rollback_batch",
                            )
                        )
                    self._audit_ok(
                        performer,
                        "rollback_batch",
                        None,
                        {
                            "batch_id": batch_id,
                            "rolled_back": [
                                {"path": item.path, "version": item.version}
                                for item in rolled_back
                            ],
                            "skipped": [item.as_dict() for item in skipped],
                            "reason": reason,
                        },
                        session,
                        model,
                    )
                    return RollbackReport(rolled_back=rolled_back, skipped=skipped)
        except ArtekBrainError as exc:
            self._log_failure(
                "rollback_batch", None, safe_author, exc, session, model
            )
            raise

    # ------------------------------------------------------------------
    # Импорт/экспорт
    # ------------------------------------------------------------------
    def export_markdown(
        self,
        path: str,
        version: int | None = None,
        *,
        actor: str | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> str:
        """Экспортировать содержимое текущей (или указанной) версии в Markdown."""
        safe_path = path if isinstance(path, str) else None
        try:
            path = self._normalize_path(path)
            safe_path = path
            with self._lock:
                self._ensure_open()
                with self._conn:
                    doc = self._get_doc_locked(path)
                    if doc is None:
                        raise NotFoundError(
                            f"Документ «{path}» не найден.",
                            hint="Проверьте путь.",
                        )
                    target_n = int(doc["current_version"]) if version is None else version
                    info = self._version_info(
                        self._version_row_locked(path, int(target_n))
                    )
                    content = self._read_blob(info.sha256)
                    self._audit_ok(
                        actor,
                        "export",
                        path,
                        {"version": info.n, "chars": len(content)},
                        session,
                        model,
                    )
                    return content
        except ArtekBrainError as exc:
            self._log_failure("export", safe_path, actor, exc, session, model)
            raise

    def import_file(
        self,
        path: str,
        file: str | os.PathLike[str] | Any,
        author: str,
        reason: str,
        base_version: int | None = None,
        expected_sha256: str | None = None,
        batch_id: str | None = None,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> WriteResult:
        """Импортировать файл с диска или file-like объект как новую версию."""
        try:
            if hasattr(file, "read"):
                raw = file.read()
            elif isinstance(file, (str, os.PathLike)):
                try:
                    raw = Path(file).read_bytes()
                except OSError as exc:
                    raise ValidationError(
                        f"Не удалось прочитать файл {file!r}: {exc}.",
                        hint="Передайте существующий путь или file-like объект.",
                    ) from exc
            else:
                raise ValidationError(
                    "file должен быть путём, bytes или file-like объектом.",
                    hint="Передайте pathlib.Path, str-путь или открытый файл.",
                )
            if isinstance(raw, bytes):
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValidationError(
                        "Файл не является текстом UTF-8.",
                        hint="Сохраните документ в UTF-8 и повторите импорт.",
                    ) from exc
            elif isinstance(raw, str):
                content = raw
            else:
                raise ValidationError(
                    "file.read() вернул не строку и не bytes.",
                    hint="Передайте текстовый файл в UTF-8.",
                )
        except ArtekBrainError as exc:
            self._log_failure(
                "import",
                path if isinstance(path, str) else None,
                author if isinstance(author, str) else None,
                exc,
                session,
                model,
            )
            raise
        return self._run_write(
            path,
            content,
            author,
            reason,
            base_version,
            expected_sha256,
            batch_id,
            action="import",
            session=session,
            model=model,
        )

    # ------------------------------------------------------------------
    # Аудит, статистика, gc
    # ------------------------------------------------------------------
    def audit(
        self,
        limit: int = 100,
        actor: str | None = None,
        action: str | None = None,
        since: str | datetime | None = None,
    ) -> list[AuditEntry]:
        """Прочитать журнал аудита, новые сверху."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ValidationError(
                "limit должен быть целым числом от 1 до 10000.",
                hint="Передайте limit=100 (или другое число в диапазоне).",
            )
        since_iso = _parse_ts(since, name="since")
        clauses: list[str] = []
        params: list[Any] = []
        if actor is not None:
            clauses.append("actor=?")
            params.append(actor)
        if action is not None:
            clauses.append("action=?")
            params.append(action)
        if since_iso is not None:
            clauses.append("ts>=?")
            params.append(since_iso)
        sql = "SELECT * FROM audit"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(sql, params).fetchall()
        result: list[AuditEntry] = []
        for row in rows:
            try:
                detail = json.loads(row["detail_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                detail = {"raw": row["detail_json"]}
            if not isinstance(detail, dict):
                detail = {"value": detail}
            result.append(
                AuditEntry(
                    id=row["id"],
                    ts=row["ts"],
                    actor=row["actor"],
                    action=row["action"],
                    path=row["path"],
                    detail=detail,
                    session=row["session"],
                    model=row["model"],
                )
            )
        return result

    def stats(self) -> dict[str, Any]:
        """Метрики: документы, версии, байты на диске, топ авторов, ошибки."""
        with self._lock:
            self._ensure_open()
            documents = self._conn.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0]
            deleted = self._conn.execute(
                "SELECT COUNT(*) FROM documents WHERE deleted=1"
            ).fetchone()[0]
            versions = self._conn.execute(
                "SELECT COUNT(*) FROM versions"
            ).fetchone()[0]
            errors = self._conn.execute(
                "SELECT COUNT(*) FROM audit WHERE detail_json LIKE '%\"ok\": false%'"
            ).fetchone()[0]
            reads = self._conn.execute(
                "SELECT COUNT(*) FROM audit WHERE action='read'"
            ).fetchone()[0]
            top_rows = self._conn.execute(
                """
                SELECT author, COUNT(*) AS c FROM versions
                GROUP BY author ORDER BY c DESC, author ASC LIMIT 5
                """
            ).fetchall()
        blob_bytes = 0
        blob_files = 0
        if self.blobs_dir.exists():
            for item in self.blobs_dir.rglob("*"):
                if item.is_file():
                    blob_files += 1
                    blob_bytes += item.stat().st_size
        disk_bytes = 0
        if self.root.exists():
            for item in self.root.rglob("*"):
                if item.is_file():
                    disk_bytes += item.stat().st_size
        return {
            "documents": int(documents),
            "documents_deleted": int(deleted),
            "versions": int(versions),
            "blobs": blob_files,
            "blob_bytes": blob_bytes,
            "db_bytes": max(0, disk_bytes - blob_bytes),
            "disk_bytes": disk_bytes,
            "top_authors": [
                {"author": row["author"], "versions": int(row["c"])}
                for row in top_rows
            ],
            "errors": int(errors),
            "reads": int(reads),
        }

    def gc(self, *, actor: str | None = None) -> GcResult:
        """Удалить блобы, на которые не ссылается ни одна версия."""
        with self._lock:
            self._ensure_open()
            with self._conn:
                referenced = {
                    row["sha256"]
                    for row in self._conn.execute(
                        "SELECT DISTINCT sha256 FROM versions"
                    ).fetchall()
                }
                files = [
                    item for item in self.blobs_dir.rglob("*") if item.is_file()
                ]
                removed = 0
                freed = 0
                for item in files:
                    sha = None
                    if (
                        item.parent.parent == self.blobs_dir
                        and len(item.parent.name) == 2
                        and _SHA_RE.match(item.parent.name + item.name)
                    ):
                        sha = item.parent.name + item.name
                    if sha is not None and sha in referenced:
                        continue
                    try:
                        size = item.stat().st_size
                        item.unlink()
                    except OSError:
                        continue
                    removed += 1
                    freed += size
                for directory in sorted(
                    (item for item in self.blobs_dir.rglob("*") if item.is_dir()),
                    key=lambda item: len(item.parts),
                    reverse=True,
                ):
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                result = GcResult(
                    scanned=len(files), removed=removed, freed_bytes=freed
                )
                self._audit_ok(actor, "gc", None, result.as_dict())
                return result
