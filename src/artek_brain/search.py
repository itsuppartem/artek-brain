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

from . import frontmatter
from .errors import DeletedError, NotFoundError, ValidationError

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_TAG_SPLIT_RE = re.compile(r"[\s,]+")

_SCHEMA = """
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


def _fts_query(query: str) -> str:
    """Безопасно превратить запрос в FTS5-выражение «все слова»."""
    terms = [term for term in query.split() if term.strip()]
    if not terms:
        return ""
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
        self._conn = sqlite3.connect(
            str(db_path), timeout=30.0, isolation_level="DEFERRED", check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        with self._conn:
            self._conn.executescript(_SCHEMA)
        self._signature_value: tuple[int, int] | None = None
        if sync:
            self.sync()

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

    def _signature(self) -> tuple[int, int]:
        """Отпечаток хранилища: последний id версии и число документов."""
        with self.store._lock:
            self.store._ensure_open()
            row = self.store._conn.execute(
                "SELECT (SELECT COALESCE(MAX(id), 0) FROM versions) AS v,"
                " (SELECT COUNT(*) FROM documents) AS d"
            ).fetchone()
        return int(row["v"]), int(row["d"])

    def _ensure_fresh(self) -> None:
        """Догнать индекс, если Store писали в обход фасада."""
        if self._signature_value != self._signature():
            self.sync()

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

    def update(self, path: str) -> None:
        """Обновить индексы одного документа после изменения."""
        exists, deleted, info, text = self._doc_state(path)
        with self._lock:
            self._ensure_open()
            with self._conn:
                if not exists:
                    self._remove_current_locked(path)
                elif deleted:
                    self._remove_current_locked(path)
                    self._write_history_locked(path, info, text)
                else:
                    self._write_current_locked(path, info, text)
                    self._write_history_locked(path, info, text)
        self._signature_value = self._signature()

    def remove(self, path: str) -> None:
        """Убрать документ из текущего индекса (история остаётся)."""
        with self._lock:
            self._ensure_open()
            with self._conn:
                self._remove_current_locked(path)
        self._signature_value = self._signature()

    def sync(self) -> None:
        """Перестроить оба индекса из хранилища целиком."""
        with self.store._lock:
            self.store._ensure_open()
            docs = self.store._conn.execute(
                "SELECT path, current_version, deleted FROM documents ORDER BY path"
            ).fetchall()
            versions = self.store._conn.execute(
                "SELECT path, n FROM versions ORDER BY path, n"
            ).fetchall()
            snapshot: list[tuple[str, int, bool]] = []
            for row in docs:
                snapshot.append((row["path"], int(row["current_version"]), bool(row["deleted"])))
            version_pairs = [(row["path"], int(row["n"])) for row in versions]
        with self._lock:
            self._ensure_open()
            with self._conn:
                self._conn.execute("DELETE FROM current_fts")
                self._conn.execute("DELETE FROM current_map")
                self._conn.execute("DELETE FROM history_fts")
                self._conn.execute("DELETE FROM history_map")
                for path, current_version, deleted in snapshot:
                    if deleted:
                        continue
                    info, text = self._doc_version(path, current_version)
                    self._write_current_locked(path, info, text)
                for path, version in version_pairs:
                    try:
                        info, text = self._doc_version(path, version)
                    except (NotFoundError, DeletedError):
                        continue
                    self._write_history_locked(path, info, text)
        self._signature_value = self._signature()

    rebuild = sync

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
        with self._lock:
            self._ensure_open()
            if match:
                sql = (
                    f"SELECT {columns}, bm25(current_fts) AS rank, "
                    "snippet(current_fts, 2, '[', ']', '…', 16) AS snip "
                    "FROM current_fts WHERE current_fts MATCH ?"
                )
                args: list[Any] = [match, *params]
                if clauses:
                    sql += " AND " + " AND ".join(clauses)
                sql += " ORDER BY rank ASC, path ASC"
            else:
                sql = f"SELECT {columns}, 0.0 AS rank, substr(body, 1, 240) AS snip FROM current_fts"
                args = list(params)
                if clauses:
                    sql += " WHERE " + " AND ".join(clauses)
                sql += " ORDER BY path ASC"
            rows = self._conn.execute(sql, args).fetchall()
        items = [self._row_to_item(row) for row in rows]
        if can_read is not None:
            items = [item for item in items if can_read(item.path)]
        total = len(items)
        page = items[offset : offset + limit]
        return SearchPage(
            items=page, total=total, limit=limit, offset=offset, query=query
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
