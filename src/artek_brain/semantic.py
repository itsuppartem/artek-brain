"""Опциональный семантический индекс поверх FTS (только stdlib).

Индекс хранит чанки текущих версий документов в `semantic.sqlite3`, векторы —
BLOB float32. Косинус считается вручную; при большом индексе (больше 50k
чанков) поиск сначала ограничивается кандидатами из FTS. Семантика выключена
по умолчанию и включается настройкой хранилища `settings.json`.
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from . import frontmatter
from .embeddings import (
    DEFAULT_HASH_DIM,
    DEFAULT_OLLAMA_URL,
    Embedder,
    EmbeddingError,
    build_embedder,
)
from .errors import ArtekBrainError, ValidationError
from .search import SearchItem, SearchPage, _first_heading, _parse_date, _tags_key
from .store import _split_sections

SETTINGS_FILE = "settings.json"
INDEX_FILE = "semantic.sqlite3"
CHUNK_CHARS = 1500
PREFILTER_THRESHOLD = 50_000
DEFAULT_RRF_K = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    version INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    section TEXT,
    title TEXT,
    text TEXT NOT NULL,
    vector BLOB NOT NULL,
    dim INTEGER NOT NULL,
    author TEXT,
    ts TEXT,
    doc_type TEXT,
    status TEXT,
    tags TEXT,
    UNIQUE(path, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
CREATE INDEX IF NOT EXISTS idx_chunks_model ON chunks(dim);
"""


class SemanticError(ArtekBrainError):
    """Базовая ошибка семантического слоя."""


class SemanticDisabledError(SemanticError):
    """Семантика выключена в настройках хранилища."""


class SemanticNotConfiguredError(SemanticError):
    """Семантика включена, но эмбеддер не настроен или сломан."""


class SemanticIndexStaleError(SemanticError):
    """Индекс построен другой моделью или повреждён; нужен reindex."""


class SemanticIndexError(SemanticError):
    """Данные семантического индекса повреждены."""


@dataclass(frozen=True)
class SemanticSettings:
    """Настройки семантического слоя из settings.json."""

    enabled: bool = False
    embedder: str | None = None
    model: str | None = None
    url: str = DEFAULT_OLLAMA_URL
    dim: int = DEFAULT_HASH_DIM
    timeout: float = 10.0

    def as_dict(self) -> dict[str, Any]:
        """Представить настройки словарём."""
        return {
            "enabled": self.enabled,
            "embedder": self.embedder,
            "model": self.model,
            "url": self.url,
            "dim": self.dim,
            "timeout": self.timeout,
        }


def settings_path(store: Any) -> Path:
    """Путь к файлу настроек рядом с хранилищем."""
    return Path(store.root) / SETTINGS_FILE


def load_settings(store: Any) -> SemanticSettings:
    """Прочитать настройки семантики; отсутствие файла = выключено."""
    target = settings_path(store)
    if not target.exists():
        return SemanticSettings()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            f"Не удалось прочитать настройки {target}: {exc}.",
            hint="Исправьте JSON в settings.json или удалите файл.",
        ) from exc
    if not isinstance(raw, dict):
        raise ValidationError(
            f"Настройки {target} должны быть JSON-объектом.",
            hint="Ожидается {\"semantic\": {...}}.",
        )
    block = raw.get("semantic", raw)
    if not isinstance(block, dict):
        raise ValidationError(
            "Блок semantic в settings.json должен быть объектом.",
            hint="Ожидается {\"semantic\": {\"enabled\": true, ...}}.",
        )
    enabled = bool(block.get("enabled", False))
    embedder = block.get("embedder")
    model = block.get("model")
    url = block.get("url") or DEFAULT_OLLAMA_URL
    try:
        dim = int(block.get("dim", DEFAULT_HASH_DIM))
        timeout = float(block.get("timeout", 10.0))
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "dim и timeout в настройках семантики должны быть числами.",
            hint="Например: {\"dim\": 256, \"timeout\": 10}.",
        ) from exc
    return SemanticSettings(
        enabled=enabled,
        embedder=str(embedder).strip().lower() if embedder else None,
        model=str(model).strip() if model else None,
        url=str(url),
        dim=dim,
        timeout=timeout,
    )


def save_settings(store: Any, settings: SemanticSettings) -> Path:
    """Атомарно сохранить настройки семантики."""
    target = settings_path(store)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                raw = existing
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raw = {}
    raw["semantic"] = settings.as_dict()
    temporary = target.with_name(f"{target.name}.tmp")
    temporary.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(target)
    return target


def configure_semantic(
    store: Any,
    *,
    enabled: bool = True,
    embedder: str | None = "hash",
    model: str | None = None,
    url: str = DEFAULT_OLLAMA_URL,
    dim: int = DEFAULT_HASH_DIM,
    timeout: float = 10.0,
) -> SemanticSettings:
    """Включить/выключить семантику и сохранить настройки хранилища."""
    settings = SemanticSettings(
        enabled=bool(enabled),
        embedder=embedder,
        model=model,
        url=url,
        dim=int(dim),
        timeout=float(timeout),
    )
    save_settings(store, settings)
    return settings


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _chunk_windows(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Разбить текст раздела на окна не длиннее limit символов."""
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    windows: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                windows.append(current)
                current = ""
            windows.append(line[:limit])
            line = line[limit:]
        if current and len(current) + len(line) > limit:
            windows.append(current)
            current = ""
        current += line
    if current:
        windows.append(current)
    return windows


def _snippet(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _tags_tuple(value: str | None) -> tuple[str, ...]:
    return tuple(item for item in (value or "").strip("|").split("|") if item)


class SemanticIndex:
    """Индекс чанков текущих версий документов и косинусный поиск."""

    def __init__(
        self,
        store: Any,
        embedder: Embedder | None = None,
        *,
        path: str | Path | None = None,
        config_error: str | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self._config_error = config_error
        self._lock = threading.RLock()
        self._closed = False
        self._stale = False
        self._stale_reason: str | None = None
        self._candidate_provider: Callable[[str], Sequence[str]] | None = None
        db_path = Path(path) if path is not None else Path(store.root) / INDEX_FILE
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
        self._meta: dict[str, str] = {}
        self._refresh_state()

    @classmethod
    def from_store(cls, store: Any) -> "SemanticIndex | None":
        """Создать индекс по настройкам; None, если семантика выключена."""
        try:
            settings = load_settings(store)
        except ArtekBrainError as exc:
            return cls(store, None, config_error=str(exc))
        if not settings.enabled:
            return None
        config_error: str | None = None
        embedder: Embedder | None = None
        try:
            embedder = build_embedder(
                settings.embedder,
                dim=settings.dim,
                model=settings.model,
                url=settings.url,
                timeout=settings.timeout,
            )
        except ArtekBrainError as exc:
            config_error = str(exc)
        if embedder is None and config_error is None:
            config_error = (
                "Эмбеддер не настроен: укажите embedder=hash или embedder=ollama "
                "в settings.json."
            )
        return cls(store, embedder, config_error=config_error)

    def __enter__(self) -> "SemanticIndex":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Закрыть индекс (повторный вызов безопасен)."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValidationError(
                "Семантический индекс закрыт.",
                hint="Создайте новый SemanticIndex(store) до конца работы.",
            )

    # ------------------------------------------------------------------
    # Состояние и настройка
    # ------------------------------------------------------------------
    def set_candidate_provider(
        self, provider: Callable[[str], Sequence[str]] | None
    ) -> None:
        """Задать источник FTS-кандидатов для больших индексов."""
        self._candidate_provider = provider

    @property
    def stale(self) -> bool:
        """Построен ли индекс другой моделью (нужен reindex)."""
        return self._stale

    @property
    def stale_reason(self) -> str | None:
        """Почему индекс считается устаревшим."""
        return self._stale_reason

    def _read_meta_locked(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM meta").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def _set_meta_locked(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _refresh_state(self) -> None:
        with self._lock:
            self._ensure_open()
            self._meta = self._read_meta_locked()
            self._stale = False
            self._stale_reason = None
            if self.embedder is None:
                return
            stored_model = self._meta.get("model")
            stored_dim = self._meta.get("dim")
            if stored_model is not None and stored_model != self.embedder.name:
                self._stale = True
                self._stale_reason = (
                    f"индекс построен моделью «{stored_model}», "
                    f"а настроена «{self.embedder.name}»"
                )
            elif stored_model is None and self.chunk_count() > 0:
                self._stale = True
                self._stale_reason = "у индекса нет имени модели"
            elif stored_dim is not None and self.embedder.dim:
                try:
                    same_dim = int(stored_dim) == self.embedder.dim
                except (TypeError, ValueError):
                    same_dim = False
                if not same_dim:
                    self._stale = True
                    self._stale_reason = (
                        f"размерность индекса {stored_dim} не совпадает "
                        f"с эмбеддером {self.embedder.dim}"
                    )

    def _raise_if_unavailable(self) -> None:
        if self._config_error is not None:
            raise SemanticNotConfiguredError(
                f"Семантический поиск недоступен: {self._config_error}",
                hint=(
                    "Исправьте блок semantic в settings.json "
                    "(embedder=hash или embedder=ollama + model) и пересоздайте Brain."
                ),
            )
        if self.embedder is None:
            raise SemanticNotConfiguredError(
                "Эмбеддер не настроен.",
                hint=(
                    "Укажите embedder=hash или embedder=ollama и model "
                    "в settings.json, затем пересоздайте Brain."
                ),
            )
        if self._stale:
            raise SemanticIndexStaleError(
                f"Семантический индекс устарел: {self._stale_reason or 'сменилась модель'}.",
                hint=(
                    "Выполните reindex() у SemanticIndex (или пересоздайте индекс "
                    "после смены модели)."
                ),
            )

    def chunk_count(self) -> int:
        """Сколько чанков лежит в индексе."""
        with self._lock:
            self._ensure_open()
            return int(self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def stats(self) -> dict[str, Any]:
        """Краткое состояние индекса."""
        with self._lock:
            self._ensure_open()
            return {
                "chunks": self.chunk_count(),
                "model": self._meta.get("model"),
                "dim": self._meta.get("dim"),
                "stale": self._stale,
                "stale_reason": self._stale_reason,
            }

    # ------------------------------------------------------------------
    # Наполнение индекса
    # ------------------------------------------------------------------
    def _doc_state(self, path: str) -> tuple[bool, bool, Any | None, str | None]:
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

    def _prepare_chunks(
        self, path: str, info: Any, text: str
    ) -> list[dict[str, Any]]:
        parsed = frontmatter.parse(text)
        title = str(parsed.data.get("title") or "") or _first_heading(
            parsed.body, Path(path).stem
        )
        tags = _tags_key(parsed.data.get("tags", []))
        doc_type = str(parsed.data.get("type") or "")
        status = str(parsed.data.get("status") or "")
        chunks: list[dict[str, Any]] = []
        for section in _split_sections(text):
            for window in _chunk_windows(section.text):
                if not window.strip():
                    continue
                chunks.append(
                    {
                        "section": section.title or "preamble",
                        "text": window,
                        "title": title,
                        "doc_type": doc_type,
                        "status": status,
                        "tags": tags,
                    }
                )
        return chunks

    def _delete_path_locked(self, path: str) -> None:
        self._conn.execute("DELETE FROM chunks WHERE path=?", (path,))

    def _insert_chunks_locked(
        self,
        path: str,
        info: Any,
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
    ) -> None:
        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            dim = len(vector)
            blob = struct.pack(f"<{dim}f", *vector)
            self._conn.execute(
                """
                INSERT INTO chunks
                    (path, version, chunk_index, section, title, text, vector,
                     dim, author, ts, doc_type, status, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    path,
                    int(info.n),
                    index,
                    chunk["section"],
                    chunk["title"],
                    chunk["text"],
                    blob,
                    dim,
                    info.author,
                    info.ts,
                    chunk["doc_type"],
                    chunk["status"],
                    chunk["tags"],
                ),
            )

    def update(self, path: str) -> bool:
        """Обновить чанки одного документа после write/delete/restore/rollback."""
        if self._closed:
            return False
        if self.embedder is None or self._stale:
            return False
        try:
            exists, deleted, info, text = self._doc_state(path)
            if not exists or deleted or info is None or text is None:
                with self._lock:
                    self._ensure_open()
                    with self._conn:
                        self._delete_path_locked(path)
                return True
            chunks = self._prepare_chunks(path, info, text)
            vectors: list[list[float]] = []
            if chunks:
                vectors = self.embedder.embed([chunk["text"] for chunk in chunks])
                if len(vectors) != len(chunks):
                    raise EmbeddingError(
                        "Эмбеддер вернул неверное число векторов.",
                        hint="Проверьте настройки эмбеддера.",
                    )
                dims = {len(vector) for vector in vectors}
                if len(dims) != 1 or 0 in dims:
                    raise EmbeddingError(
                        "Эмбеддер вернул векторы разной или нулевой размерности.",
                        hint="Проверьте модель эмбеддингов.",
                    )
            with self._lock:
                self._ensure_open()
                with self._conn:
                    self._delete_path_locked(path)
                    self._insert_chunks_locked(path, info, chunks, vectors)
                    self._set_meta_locked("model", self.embedder.name)
                    self._set_meta_locked(
                        "dim", str(len(vectors[0]) if vectors else self.embedder.dim)
                    )
                    self._set_meta_locked("updated_at", _utc_now())
                self._meta = self._read_meta_locked()
            return True
        except ArtekBrainError as exc:
            with self._lock:
                self._stale = True
                self._stale_reason = str(exc)
            return False
        except sqlite3.Error as exc:
            with self._lock:
                self._stale = True
                self._stale_reason = f"ошибка индекса: {exc}"
            return False

    def remove(self, path: str) -> None:
        """Убрать документ из семантического индекса."""
        with self._lock:
            self._ensure_open()
            with self._conn:
                self._delete_path_locked(path)

    def reindex(self) -> int:
        """Перестроить индекс текущих версий под текущую модель."""
        self._ensure_open()
        if self._config_error is not None:
            raise SemanticNotConfiguredError(
                f"Семантический индекс недоступен: {self._config_error}",
                hint=(
                    "Исправьте блок semantic в settings.json "
                    "(embedder=hash или embedder=ollama + model)."
                ),
            )
        if self.embedder is None:
            raise SemanticNotConfiguredError(
                "Эмбеддер не настроен.",
                hint=(
                    "Укажите embedder=hash или embedder=ollama и model "
                    "в settings.json."
                ),
            )
        assert self.embedder is not None
        with self.store._lock:
            self.store._ensure_open()
            rows = self.store._conn.execute(
                "SELECT path FROM documents WHERE deleted=0 ORDER BY path"
            ).fetchall()
        paths = [row["path"] for row in rows]
        with self._lock:
            self._ensure_open()
            with self._conn:
                self._conn.execute("DELETE FROM chunks")
                self._conn.execute("DELETE FROM meta")
            self._stale = False
            self._stale_reason = None
            self._meta = {}
        for path in paths:
            self.update(path)
        if self._stale:
            raise SemanticIndexStaleError(
                f"Не удалось перестроить индекс: {self._stale_reason or 'ошибка эмбеддера'}.",
                hint="Проверьте настройки эмбеддера и повторите reindex().",
            )
        if not paths:
            with self._lock:
                with self._conn:
                    self._set_meta_locked("model", self.embedder.name)
                    self._set_meta_locked("dim", str(self.embedder.dim))
                    self._set_meta_locked("updated_at", _utc_now())
                self._meta = self._read_meta_locked()
        return self.chunk_count()

    sync = reindex

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

    def _matches(
        self,
        row: sqlite3.Row,
        *,
        doc_type: str | None,
        status: str | None,
        tags: Sequence[str] | str | None,
        author: str | None,
        since: str | None,
        until: str | None,
    ) -> bool:
        if doc_type and (row["doc_type"] or "") != str(doc_type):
            return False
        if status and (row["status"] or "") != str(status):
            return False
        if author and (row["author"] or "") != str(author):
            return False
        if since and (row["ts"] or "") < since:
            return False
        if until and (row["ts"] or "") > until:
            return False
        if tags:
            raw_tags = [tags] if isinstance(tags, str) else list(tags)
            stored = row["tags"] or ""
            for tag in raw_tags:
                cleaned = str(tag).strip().lower()
                if cleaned and f"|{cleaned}|" not in stored:
                    return False
        return True

    @staticmethod
    def _decode_vector(blob: bytes, dim: int) -> tuple[float, ...]:
        if dim <= 0 or len(blob) != dim * 4:
            raise SemanticIndexError(
                "Вектор в семантическом индексе повреждён.",
                hint="Перестройте индекс через reindex().",
            )
        return struct.unpack(f"<{dim}f", blob)

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
        candidate_paths: Sequence[str] | None = None,
    ) -> SearchPage:
        """Косинусный поиск по чанкам с фильтрами и правами."""
        doc_type = doc_type or type
        self._validate_page(limit, offset)
        self._ensure_open()
        self._raise_if_unavailable()
        if not isinstance(query, str) or not query.strip():
            raise ValidationError(
                "Для семантического поиска нужен непустой query.",
                hint="Передайте строку запроса, например «деплой сервера».",
            )
        assert self.embedder is not None
        query_vectors = self.embedder.embed([query])
        if len(query_vectors) != 1 or not query_vectors[0]:
            raise EmbeddingError(
                "Эмбеддер вернул пустой вектор запроса.",
                hint="Проверьте настройки эмбеддера.",
            )
        query_vector = query_vectors[0]
        stored_dim = self._meta.get("dim")
        if stored_dim is not None and int(stored_dim) != len(query_vector):
            self._stale = True
            self._stale_reason = (
                f"эмбеддер вернул размерность {len(query_vector)}, "
                f"а в индексе {stored_dim}"
            )
            raise SemanticIndexStaleError(
                f"Семантический индекс устарел: {self._stale_reason}.",
                hint="Выполните reindex() после смены модели.",
            )
        since_iso = _parse_date(since, name="since")
        until_iso = _parse_date(until, name="until")
        prefix = (path_prefix or "").strip().strip("/")
        if (
            candidate_paths is None
            and self._candidate_provider is not None
            and self.chunk_count() > PREFILTER_THRESHOLD
        ):
            try:
                candidate_paths = self._candidate_provider(query)
            except Exception:
                candidate_paths = None
        sql = (
            "SELECT path, version, section, title, text, vector, dim, author, ts, "
            "doc_type, status, tags FROM chunks"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if prefix:
            clauses.append("(path = ? OR path LIKE ? ESCAPE '\\')")
            params.extend([prefix, self._like_prefix(prefix)])
        if candidate_paths is not None:
            unique = list(dict.fromkeys(str(item) for item in candidate_paths))[:900]
            if not unique:
                return SearchPage(
                    items=[], total=0, limit=limit, offset=offset, query=query
                )
            clauses.append("path IN (" + ",".join("?" for _ in unique) + ")")
            params.extend(unique)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(sql, params).fetchall()
        query_norm = math.sqrt(sum(value * value for value in query_vector)) or 1.0
        best: dict[str, tuple[float, sqlite3.Row]] = {}
        for row in rows:
            path = row["path"]
            if can_read is not None and not can_read(path):
                continue
            if not self._matches(
                row,
                doc_type=doc_type,
                status=status,
                tags=tags,
                author=author,
                since=since_iso,
                until=until_iso,
            ):
                continue
            vector = self._decode_vector(row["vector"], int(row["dim"]))
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            dot = sum(a * b for a, b in zip(query_vector, vector))
            score = dot / (query_norm * norm)
            if score <= 1e-9:
                continue
            current = best.get(path)
            if current is None or score > current[0]:
                best[path] = (score, row)
        ordered = sorted(best.items(), key=lambda item: (-item[1][0], item[0]))
        total = len(ordered)
        page = ordered[offset : offset + limit]
        items = [
            SearchItem(
                path=path,
                version=int(row["version"]),
                title=row["title"] or "",
                body_snippet=_snippet(row["text"] or ""),
                doc_type=row["doc_type"] or "",
                status=row["status"] or "",
                tags=_tags_tuple(row["tags"]),
                author=row["author"] or "",
                ts=row["ts"] or "",
                score=float(score),
            )
            for path, (score, row) in page
        ]
        return SearchPage(
            items=items, total=total, limit=limit, offset=offset, query=query
        )


def hybrid_search(
    fts: Any,
    semantic: SemanticIndex,
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
    rrf_k: int = DEFAULT_RRF_K,
) -> SearchPage:
    """Объединить FTS (BM25) и семантику методом RRF с теми же правами."""
    doc_type = doc_type or type
    if not isinstance(query, str) or not query.strip():
        raise ValidationError(
            "Для гибридного поиска нужен непустой query.",
            hint="Передайте строку запроса, например «деплой сервера».",
        )
    depth = min(1000, max(limit + offset, 100))
    common = {
        "path_prefix": path_prefix,
        "doc_type": doc_type,
        "status": status,
        "tags": tags,
        "author": author,
        "since": since,
        "until": until,
        "limit": depth,
        "offset": 0,
        "can_read": can_read,
    }
    fts_page = fts.search(query, **common)
    candidates = None
    if semantic.chunk_count() > PREFILTER_THRESHOLD:
        candidates = [item.path for item in fts_page.items]
    semantic_page = semantic.search(
        query, **common, candidate_paths=candidates
    )
    merged: dict[str, dict[str, Any]] = {}
    for rank, item in enumerate(fts_page.items, start=1):
        merged[item.path] = {
            "item": item,
            "score": 1.0 / (rrf_k + rank),
            "from_fts": True,
        }
    for rank, item in enumerate(semantic_page.items, start=1):
        value = 1.0 / (rrf_k + rank)
        entry = merged.get(item.path)
        if entry is None:
            merged[item.path] = {
                "item": item,
                "score": value,
                "from_fts": False,
            }
        else:
            entry["score"] += value
    ordered = sorted(
        merged.values(), key=lambda entry: (-entry["score"], entry["item"].path)
    )
    total = len(ordered)
    page = ordered[offset : offset + limit]
    items = [replace(entry["item"], score=float(entry["score"])) for entry in page]
    return SearchPage(
        items=items, total=total, limit=limit, offset=offset, query=query
    )
