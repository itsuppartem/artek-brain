"""Полнотекстовый поиск Artek Brain на FTS5/BM25 (только stdlib).

Индекс живёт в отдельной базе search.sqlite3 рядом с хранилищем. Он хранит
текущие версии документов и все версии для поиска по истории. Обновляется
фасадом Brain после write/delete/restore/rollback; метод sync() перестраивает
индекс из хранилища.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .perms import restrict_db
from . import frontmatter
from .errors import DeletedError, NotFoundError, ValidationError

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_TAG_SPLIT_RE = re.compile(r"[\s,]+")

_INDEX_SCHEMA_VERSION = "2"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS indexed_docs (
    path TEXT PRIMARY KEY,
    current_version INTEGER,
    deleted INTEGER NOT NULL DEFAULT 0,
    history_max INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS current_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE
);
CREATE VIRTUAL TABLE IF NOT EXISTS current_fts USING fts5(
    path UNINDEXED,
    title,
    body,
    doc_type UNINDEXED,
    status UNINDEXED,
    tags UNINDEXED,
    author UNINDEXED,
    ts UNINDEXED,
    version UNINDEXED,
    tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS history_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    version INTEGER NOT NULL,
    UNIQUE(path, version)
);
CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
    path UNINDEXED,
    version UNINDEXED,
    title,
    body,
    doc_type UNINDEXED,
    status UNINDEXED,
    tags UNINDEXED,
    author UNINDEXED,
    ts UNINDEXED,
    tokenize='unicode61'
);
"""

_LEVELS = ("read", "write", "admin")


def _parse_date(value: str | datetime | None, *, name: str) -> str | None:
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
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _stem(term: str) -> str:
    """Грубая основа слова для префиксного поиска: «выкатка» → «выкат»."""
    if len(term) <= 5:
        return term
    return term[: max(4, len(term) - 2)]


def _fts_query(query: str, match: str = "all") -> str:
    """Безопасно превратить запрос в FTS5-выражение.

    match="all" — все слова точно (по умолчанию);
    match="any" — любое слово по основе с префиксом (запасной режим,
    когда строгий запрос ничего не нашёл).
    """
    terms = [term for term in query.split() if term.strip()]
    if not terms:
        return ""
    if match == "any":
        parts = ['"' + _stem(term).replace('"', '""') + '"*' for term in terms]
        return " OR ".join(parts)
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _tags_key(value: Any) -> str:
    if isinstance(value, str):
        items = _TAG_SPLIT_RE.split(value)
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        items = []
    cleaned = [item.strip().lower() for item in items if item and item.strip()]
    return "|" + "|".join(cleaned) + "|" if cleaned else ""


def _first_heading(text: str, fallback: str) -> str:
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            return match.group(1).strip()
    return fallback


@dataclass(frozen=True)
class SearchItem:
    """Один результат поиска."""

    path: str
    version: int
    title: str
    body_snippet: str
    doc_type: str
    status: str
    tags: tuple[str, ...]
    author: str
    ts: str
    score: float = 0.0

    @property
    def type(self) -> str:
        """Псевдоним поля type из frontmatter."""
        return self.doc_type

    @property
    def snippet(self) -> str:
        """Короткий фрагмент с подсветкой найденных слов."""
        return self.body_snippet

    def as_dict(self) -> dict[str, Any]:
        """Представить результат словарём."""
        return {
            "path": self.path,
            "version": self.version,
            "title": self.title,
            "snippet": self.body_snippet,
            "type": self.doc_type,
            "status": self.status,
            "tags": list(self.tags),
            "author": self.author,
            "ts": self.ts,
            "score": self.score,
        }


@dataclass(frozen=True)
class SearchPage:
    """Страница результатов с общим числом совпадений."""

    items: list[SearchItem] = field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0
    query: str = ""
    relaxed: bool = False

    @property
    def has_more(self) -> bool:
        """Есть ли следующая страница."""
        return self.offset + len(self.items) < self.total

    def as_dict(self) -> dict[str, Any]:
        """Представить страницу словарём."""
        return {
            "items": [item.as_dict() for item in self.items],
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "has_more": self.has_more,
            "query": self.query,
            "match": "any" if self.relaxed else "all",
            **(
                {
                    "note": (
                        "Точного совпадения по всем словам нет; показаны "
                        "документы с любым из слов (по основе)."
                    )
                }
                if self.relaxed
                else {}
            ),
        }


class Search:
    """FTS5-индекс текущих версий и истории документов."""

    def __init__(
        self,
        store: Any,
        *,
        path: str | Path | None = None,
        sync: bool = True,
    ) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._closed = False
        db_path = Path(path) if path is not None else Path(store.root) / "search.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._conn = self._open_connection()
        self._signature_value: str | None = None
        if sync:
            self.sync()

    @staticmethod
    def _is_corruption(exc: BaseException) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "malformed",
                "not a database",
                "file is not a database",
                "database disk image is malformed",
                "corrupt",
            )
        )

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level="DEFERRED",
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        restrict_db(self.db_path)
        return conn

    def _open_connection(self) -> sqlite3.Connection:
        try:
            conn = self._new_connection()
            try:
                with conn:
                    conn.executescript(_SCHEMA)
            except sqlite3.DatabaseError:
                conn.close()
                raise
            return conn
        except sqlite3.DatabaseError as exc:
            if not self._is_corruption(exc):
                raise
            for suffix in ("", "-wal", "-shm"):
                try:
                    self.db_path.with_name(self.db_path.name + suffix).unlink()
                except FileNotFoundError:
                    pass
            conn = self._new_connection()
            with conn:
                conn.executescript(_SCHEMA)
            return conn

    def _recreate_index(self) -> None:
        """Пересоздать файл индекса после повреждения."""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                self.db_path.with_name(self.db_path.name + suffix).unlink()
            except FileNotFoundError:
                pass
        self._conn = self._open_connection()

    def __enter__(self) -> "Search":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Закрыть индексную базу."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValidationError(
                "Индекс поиска закрыт.",
                hint="Создайте новый Search(store) до конца работы.",
            )

    def _signature(self) -> str:
        """Отпечаток хранилища: версии и состояние документов.

        Учитывает удаление и восстановление (флаг deleted и current_version),
        поэтому не пропускает изменения, которые не меняют MAX(id)/COUNT.
        """
        with self.store._lock:
            self.store._ensure_open()
            row = self.store._conn.execute(
                "SELECT"
                " (SELECT COUNT(*) FROM versions) AS vc,"
                " (SELECT COALESCE(MAX(id), 0) FROM versions) AS vm,"
                " (SELECT COALESCE(SUM(n), 0) FROM versions) AS vn,"
                " (SELECT COUNT(*) FROM documents) AS dc,"
                " (SELECT COALESCE(SUM(COALESCE(current_version, 0)), 0)"
                "  FROM documents) AS dv,"
                " (SELECT COALESCE(SUM(deleted), 0) FROM documents) AS dd,"
                " (SELECT COALESCE(SUM(LENGTH(path)), 0) FROM documents) AS dp"
            ).fetchone()
        parts = (
            row["vc"],
            row["vm"],
            row["vn"],
            row["dc"],
            row["dv"],
            row["dd"],
            row["dp"],
        )
        return ":".join(str(int(value)) for value in parts)

    def _ensure_fresh(self) -> None:
        """Догнать индекс, если Store писали в обход фасада."""
        if self._signature_value != self._signature():
            self.sync()

    def _meta_locked(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM meta").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def _set_meta_locked(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def _delete_meta_locked(self, key: str) -> None:
        self._conn.execute("DELETE FROM meta WHERE key=?", (key,))

    def _indexed_docs_locked(self) -> dict[str, tuple[int, bool, int]]:
        rows = self._conn.execute(
            "SELECT path, current_version, deleted, history_max FROM indexed_docs"
        ).fetchall()
        return {
            str(row["path"]): (
                int(row["current_version"] or 0),
                bool(row["deleted"]),
                int(row["history_max"] or 0),
            )
            for row in rows
        }

    def _store_docs(self) -> dict[str, tuple[int, bool]]:
        with self.store._lock:
            self.store._ensure_open()
            rows = self.store._conn.execute(
                "SELECT path, current_version, deleted FROM documents ORDER BY path"
            ).fetchall()
        return {
            str(row["path"]): (int(row["current_version"] or 0), bool(row["deleted"]))
            for row in rows
        }

    def _path_snapshot(
        self, path: str, from_history: int
    ) -> tuple[
        bool,
        bool,
        Any | None,
        str | None,
        list[tuple[Any, str]],
        int,
    ]:
        """Снимок документа и его новых версий истории за один захват блокировки."""
        with self.store._lock:
            self.store._ensure_open()
            doc = self.store._get_doc_locked(path)
            if doc is None:
                return False, False, None, None, [], 0
            current = int(doc["current_version"])
            current_info = self.store._version_info(
                self.store._version_row_locked(path, current)
            )
            current_text = self.store._read_blob(current_info.sha256)
            rows = self.store._conn.execute(
                "SELECT n FROM versions WHERE path=? AND n>? ORDER BY n",
                (path, from_history),
            ).fetchall()
            history: list[tuple[Any, str]] = []
            for row in rows:
                info = self.store._version_info(
                    self.store._version_row_locked(path, int(row["n"]))
                )
                history.append((info, self.store._read_blob(info.sha256)))
            max_row = self.store._conn.execute(
                "SELECT COALESCE(MAX(n), 0) AS m FROM versions WHERE path=?", (path,)
            ).fetchone()
            max_n = int(max_row["m"])
            return True, bool(doc["deleted"]), current_info, current_text, history, max_n

    def _upsert_indexed_locked(
        self, path: str, current_version: int, deleted: bool, history_max: int
    ) -> None:
        self._conn.execute(
            "INSERT INTO indexed_docs (path, current_version, deleted, history_max)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET current_version=excluded.current_version,"
            " deleted=excluded.deleted, history_max=excluded.history_max",
            (path, int(current_version), int(bool(deleted)), int(history_max)),
        )

    def _delete_indexed_locked(self, path: str) -> None:
        self._conn.execute("DELETE FROM indexed_docs WHERE path=?", (path,))

    # ------------------------------------------------------------------
    # Наполнение индекса
    # ------------------------------------------------------------------
    def _doc_state(self, path: str) -> tuple[bool, bool, Any | None, str | None]:
        """Вернуть (существует, удалён, VersionInfo текущей, текст)."""
        with self.store._lock:
            self.store._ensure_open()
            doc = self.store._get_doc_locked(path)
            if doc is None:
                return False, False, None, None
            info = self.store._version_info(
                self.store._version_row_locked(path, int(doc["current_version"]))
            )
            text = self.store._read_blob(info.sha256)
            return True, bool(doc["deleted"]), info, text

    def _doc_version(self, path: str, version: int) -> tuple[Any, str]:
        with self.store._lock:
            self.store._ensure_open()
            if self.store._get_doc_locked(path) is None:
                raise NotFoundError(
                    f"Документ «{path}» не найден.",
                    hint="Проверьте путь.",
                )
            info = self.store._version_info(
                self.store._version_row_locked(path, int(version))
            )
            text = self.store._read_blob(info.sha256)
            return info, text

    @staticmethod
    def _split_text(text: str, fallback_title: str) -> dict[str, Any]:
        parsed = frontmatter.parse(text)
        body = parsed.body
        title = str(parsed.data.get("title") or "") or _first_heading(
            body, fallback_title
        )
        raw_tags = parsed.data.get("tags", [])
        tags = tuple(
            item for item in _tags_key(raw_tags).strip("|").split("|") if item
        )
        return {
            "body": body,
            "title": title,
            "doc_type": str(parsed.data.get("type") or ""),
            "status": str(parsed.data.get("status") or ""),
            "tags_key": _tags_key(raw_tags),
            "tags": tags,
        }

    def _write_current_locked(self, path: str, info: Any, text: str) -> None:
        parsed = self._split_text(text, Path(path).stem)
        row = self._conn.execute(
            "SELECT id FROM current_map WHERE path=?", (path,)
        ).fetchone()
        if row is not None:
            self._conn.execute(
                "DELETE FROM current_fts WHERE rowid=?", (row["id"],)
            )
            self._conn.execute("DELETE FROM current_map WHERE id=?", (row["id"],))
        cursor = self._conn.execute(
            "INSERT INTO current_map (path) VALUES (?)", (path,)
        )
        rid = int(cursor.lastrowid)
        self._conn.execute(
            """
            INSERT INTO current_fts
                (rowid, path, title, body, doc_type, status, tags, author, ts, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rid,
                path,
                parsed["title"],
                parsed["body"],
                parsed["doc_type"],
                parsed["status"],
                parsed["tags_key"],
                info.author,
                info.ts,
                str(info.n),
            ),
        )

    def _remove_current_locked(self, path: str) -> None:
        row = self._conn.execute(
            "SELECT id FROM current_map WHERE path=?", (path,)
        ).fetchone()
        if row is None:
            return
        self._conn.execute("DELETE FROM current_fts WHERE rowid=?", (row["id"],))
        self._conn.execute("DELETE FROM current_map WHERE id=?", (row["id"],))

    def _write_history_locked(self, path: str, info: Any, text: str) -> None:
        parsed = self._split_text(text, Path(path).stem)
        row = self._conn.execute(
            "SELECT id FROM history_map WHERE path=? AND version=?",
            (path, int(info.n)),
        ).fetchone()
        if row is not None:
            self._conn.execute(
                "DELETE FROM history_fts WHERE rowid=?", (row["id"],)
            )
            self._conn.execute("DELETE FROM history_map WHERE id=?", (row["id"],))
        cursor = self._conn.execute(
            "INSERT INTO history_map (path, version) VALUES (?, ?)",
            (path, int(info.n)),
        )
        rid = int(cursor.lastrowid)
        self._conn.execute(
            """
            INSERT INTO history_fts
                (rowid, path, version, title, body, doc_type, status, tags, author, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rid,
                path,
                str(info.n),
                parsed["title"],
                parsed["body"],
                parsed["doc_type"],
                parsed["status"],
                parsed["tags_key"],
                info.author,
                info.ts,
            ),
        )

    def _remove_history_locked(self, path: str) -> None:
        rows = self._conn.execute(
            "SELECT id FROM history_map WHERE path=?", (path,)
        ).fetchall()
        for row in rows:
            self._conn.execute(
                "DELETE FROM history_fts WHERE rowid=?", (row["id"],)
            )
        self._conn.execute("DELETE FROM history_map WHERE path=?", (path,))

    def update(self, path: str) -> None:
        """Обновить индексы одного документа после изменения."""
        with self._lock:
            self._ensure_open()
            old = self._indexed_docs_locked().get(path)
            from_history = old[2] if old is not None else 0
        snapshot = self._path_snapshot(path, from_history)
        exists, deleted, info, text, history, max_n = snapshot
        with self._lock:
            self._ensure_open()
            with self._conn:
                if not exists:
                    self._remove_current_locked(path)
                    self._delete_indexed_locked(path)
                    self._remove_history_locked(path)
                else:
                    if deleted:
                        self._remove_current_locked(path)
                    else:
                        self._write_current_locked(path, info, text)
                    for history_info, history_text in history:
                        self._write_history_locked(path, history_info, history_text)
                    self._upsert_indexed_locked(path, int(info.n), deleted, max_n)
            # Отпечаток не сохраняем: другой процесс мог записать документ,
            # которого ещё нет в индексе. Следующий sync догонит его.
            self._signature_value = None

    def remove(self, path: str) -> None:
        """Убрать документ из текущего индекса (история остаётся)."""
        with self._lock:
            self._ensure_open()
            with self._conn:
                self._remove_current_locked(path)
        self._signature_value = self._signature()

    def _incremental_sync_locked(self, fingerprint: str) -> None:
        docs = self._store_docs()
        indexed = self._indexed_docs_locked()
        changed: list[str] = []
        for path, state in docs.items():
            old = indexed.get(path)
            if old is None or (old[0], old[1]) != state:
                changed.append(path)
        for path in indexed:
            if path not in docs:
                changed.append(path)
        changed.sort()
        snapshots = []
        for path in changed:
            old = indexed.get(path)
            from_history = old[2] if old is not None else 0
            snapshots.append((path, self._path_snapshot(path, from_history)))
        with self._lock:
            self._ensure_open()
            with self._conn:
                for path, snapshot in snapshots:
                    exists, deleted, info, text, history, max_n = snapshot
                    if not exists:
                        self._remove_current_locked(path)
                        self._delete_indexed_locked(path)
                        self._remove_history_locked(path)
                        continue
                    if deleted:
                        self._remove_current_locked(path)
                        for history_info, history_text in history:
                            self._write_history_locked(
                                path, history_info, history_text
                            )
                        self._upsert_indexed_locked(
                            path, int(info.n), True, max_n
                        )
                    else:
                        self._write_current_locked(path, info, text)
                        for history_info, history_text in history:
                            self._write_history_locked(
                                path, history_info, history_text
                            )
                        self._upsert_indexed_locked(
                            path, int(info.n), False, max_n
                        )
                self._set_meta_locked({"schema": _INDEX_SCHEMA_VERSION})
                if self._signature() == fingerprint:
                    self._set_meta_locked({"fingerprint": fingerprint})
                    stable = True
                else:
                    self._delete_meta_locked("fingerprint")
                    stable = False
        self._signature_value = fingerprint if stable else None

    def sync(self) -> None:
        """Догнать индекс по отпечатку; полная перестройка — только при сбое."""
        with self._lock:
            self._ensure_open()
            try:
                meta = self._meta_locked()
            except sqlite3.DatabaseError as exc:
                if self._is_corruption(exc):
                    self._recreate_index()
                    self.rebuild()
                    return
                raise
        if meta.get("schema") != _INDEX_SCHEMA_VERSION:
            self.rebuild()
            return
        fingerprint = self._signature()
        if meta.get("fingerprint") == fingerprint:
            self._signature_value = fingerprint
            return
        self._incremental_sync_locked(fingerprint)

    def rebuild(self) -> None:
        """Полностью перестроить оба индекса из хранилища."""
        with self._lock:
            self._ensure_open()
            fingerprint = self._signature()
            with self.store._lock:
                self.store._ensure_open()
                docs = self.store._conn.execute(
                    "SELECT path, current_version, deleted FROM documents ORDER BY path"
                ).fetchall()
                versions = self.store._conn.execute(
                    "SELECT path, n FROM versions ORDER BY path, n"
                ).fetchall()
                snapshot: list[tuple[str, int, bool]] = [
                    (
                        str(row["path"]),
                        int(row["current_version"]),
                        bool(row["deleted"]),
                    )
                    for row in docs
                ]
                version_pairs = [(str(row["path"]), int(row["n"])) for row in versions]
            history_max: dict[str, int] = {}
            for path, version in version_pairs:
                history_max[path] = max(history_max.get(path, 0), version)
            with self._conn:
                self._conn.execute("DELETE FROM current_fts")
                self._conn.execute("DELETE FROM current_map")
                self._conn.execute("DELETE FROM history_fts")
                self._conn.execute("DELETE FROM history_map")
                self._conn.execute("DELETE FROM indexed_docs")
                for path, current_version, deleted in snapshot:
                    if not deleted:
                        info, text = self._doc_version(path, current_version)
                        self._write_current_locked(path, info, text)
                for path, version in version_pairs:
                    info, text = self._doc_version(path, version)
                    self._write_history_locked(path, info, text)
                for path, current_version, deleted in snapshot:
                    self._upsert_indexed_locked(
                        path,
                        current_version,
                        deleted,
                        history_max.get(path, 0),
                    )
                self._set_meta_locked({"schema": _INDEX_SCHEMA_VERSION})
                if self._signature() == fingerprint:
                    self._set_meta_locked({"fingerprint": fingerprint})
                    stable = True
                else:
                    self._delete_meta_locked("fingerprint")
                    stable = False
        self._signature_value = fingerprint if stable else None

    # ------------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------------
    def _validate_page(self, limit: int, offset: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValidationError(
                "limit должен быть целым числом от 1 до 1000.",
                hint="Передайте limit=20 (или другое число в диапазоне).",
            )
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValidationError(
                "offset должен быть целым числом >= 0.",
                hint="Передайте offset=0 для первой страницы.",
            )

    @staticmethod
    def _like_prefix(prefix: str) -> str:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return escaped + "/%"

    def _filter_clauses(
        self,
        *,
        path_prefix: str | None,
        doc_type: str | None,
        status: str | None,
        tags: Sequence[str] | str | None,
        author: str | None,
        since: str | None,
        until: str | None,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if path_prefix:
            prefix = path_prefix.strip().strip("/")
            if prefix:
                clauses.append("(path = ? OR path LIKE ? ESCAPE '\\')")
                params.extend([prefix, self._like_prefix(prefix)])
        if doc_type:
            clauses.append("doc_type = ?")
            params.append(str(doc_type))
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        if tags:
            raw_tags = [tags] if isinstance(tags, str) else list(tags)
            for tag in raw_tags:
                cleaned = str(tag).strip().lower()
                if cleaned:
                    clauses.append("tags LIKE ?")
                    params.append(f"%|{cleaned}|%")
        if author:
            clauses.append("author = ?")
            params.append(str(author))
        if since:
            clauses.append("ts >= ?")
            params.append(since)
        if until:
            clauses.append("ts <= ?")
            params.append(until)
        return clauses, params

    def search(
        self,
        query: str,
        *,
        path_prefix: str | None = None,
        type: str | None = None,
        doc_type: str | None = None,
        status: str | None = None,
        tags: Sequence[str] | str | None = None,
        author: str | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        limit: int = 20,
        offset: int = 0,
        can_read: Callable[[str], bool] | None = None,
    ) -> SearchPage:
        """Найти документы по текущим версиям с фильтрами и сниппетами."""
        doc_type = doc_type or type
        self._validate_page(limit, offset)
        if not isinstance(query, str):
            raise ValidationError(
                "query должен быть строкой.",
                hint="Передайте поисковую строку или пустую строку для обзора.",
            )
        self._ensure_fresh()
        since_iso = _parse_date(since, name="since")
        until_iso = _parse_date(until, name="until")
        clauses, params = self._filter_clauses(
            path_prefix=path_prefix,
            doc_type=doc_type,
            status=status,
            tags=tags,
            author=author,
            since=since_iso,
            until=until_iso,
        )
        match = _fts_query(query.strip())
        columns = (
            "rowid, path, title, body, doc_type, status, tags, author, ts, version"
        )
        def run(expr: str) -> list[sqlite3.Row]:
            with self._lock:
                self._ensure_open()
                if expr:
                    sql = (
                        f"SELECT {columns}, bm25(current_fts) AS rank, "
                        "snippet(current_fts, 2, '[', ']', '…', 16) AS snip "
                        "FROM current_fts WHERE current_fts MATCH ?"
                    )
                    args: list[Any] = [expr, *params]
                    if clauses:
                        sql += " AND " + " AND ".join(clauses)
                    sql += " ORDER BY rank ASC, path ASC"
                else:
                    sql = f"SELECT {columns}, 0.0 AS rank, substr(body, 1, 240) AS snip FROM current_fts"
                    args = list(params)
                    if clauses:
                        sql += " WHERE " + " AND ".join(clauses)
                    sql += " ORDER BY path ASC"
                return self._conn.execute(sql, args).fetchall()

        def readable(rows: list[sqlite3.Row]) -> list[SearchItem]:
            found = [self._row_to_item(row) for row in rows]
            if can_read is not None:
                found = [item for item in found if can_read(item.path)]
            return found

        items = readable(run(match))
        relaxed = False
        if match and not items:
            loose = _fts_query(query.strip(), "any")
            if loose and loose != match:
                items = readable(run(loose))
                relaxed = bool(items)
        total = len(items)
        page = items[offset : offset + limit]
        return SearchPage(
            items=page,
            total=total,
            limit=limit,
            offset=offset,
            query=query,
            relaxed=relaxed,
        )

    def search_history(
        self,
        query: str,
        *,
        path_prefix: str | None = None,
        type: str | None = None,
        doc_type: str | None = None,
        status: str | None = None,
        tags: Sequence[str] | str | None = None,
        author: str | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        version: int | None = None,
        limit: int = 20,
        offset: int = 0,
        can_read: Callable[[str], bool] | None = None,
    ) -> SearchPage:
        """Найти текст в любой версии документа, включая старые версии."""
        doc_type = doc_type or type
        self._validate_page(limit, offset)
        if not isinstance(query, str):
            raise ValidationError(
                "query должен быть строкой.",
                hint="Передайте поисковую строку или пустую строку для обзора.",
            )
        self._ensure_fresh()
        since_iso = _parse_date(since, name="since")
        until_iso = _parse_date(until, name="until")
        clauses, params = self._filter_clauses(
            path_prefix=path_prefix,
            doc_type=doc_type,
            status=status,
            tags=tags,
            author=author,
            since=since_iso,
            until=until_iso,
        )
        if version is not None:
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise ValidationError(
                    "version должен быть целым числом >= 1.",
                    hint="Передайте номер версии или None.",
                )
            clauses.append("version = ?")
            params.append(str(version))
        match = _fts_query(query.strip())
        columns = (
            "rowid, path, version, title, body, doc_type, status, tags, author, ts"
        )
        with self._lock:
            self._ensure_open()
            if match:
                sql = (
                    f"SELECT {columns}, bm25(history_fts) AS rank, "
                    "snippet(history_fts, 3, '[', ']', '…', 16) AS snip "
                    "FROM history_fts WHERE history_fts MATCH ?"
                )
                args: list[Any] = [match, *params]
                if clauses:
                    sql += " AND " + " AND ".join(clauses)
                sql += " ORDER BY rank ASC, path ASC, version DESC"
            else:
                sql = f"SELECT {columns}, 0.0 AS rank, substr(body, 1, 240) AS snip FROM history_fts"
                args = list(params)
                if clauses:
                    sql += " WHERE " + " AND ".join(clauses)
                sql += " ORDER BY path ASC, version DESC"
            rows = self._conn.execute(sql, args).fetchall()
        items = [self._row_to_item(row) for row in rows]
        if can_read is not None:
            items = [item for item in items if can_read(item.path)]
        total = len(items)
        page = items[offset : offset + limit]
        return SearchPage(
            items=page, total=total, limit=limit, offset=offset, query=query
        )

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> SearchItem:
        tags_key = row["tags"] or ""
        tags = tuple(item for item in tags_key.strip("|").split("|") if item)
        return SearchItem(
            path=row["path"],
            version=int(row["version"]),
            title=row["title"] or "",
            body_snippet=row["snip"] or "",
            doc_type=row["doc_type"] or "",
            status=row["status"] or "",
            tags=tags,
            author=row["author"] or "",
            ts=row["ts"] or "",
            score=float(row["rank"] or 0.0),
        )


# Удобные псевдонимы.
Search.index = Search.update
Search.index_document = Search.update
Search.search_versions = Search.search_history
