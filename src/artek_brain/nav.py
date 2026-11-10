"""Навигация по памяти Artek Brain: ls/tree, оглавление, ссылки, лента.

Только стандартная библиотека. Модуль читает хранилище через его публичные
методы и не меняет данные.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .errors import ValidationError
from .store import AuditEntry, _split_sections

_WIKI_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
_MD_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_CHANGE_ACTIONS = (
    "write",
    "create",
    "delete",
    "restore",
    "rollback_version",
    "rollback_author",
    "rollback_batch",
    "import",
    "gc",
)


def _parse_detail(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Node:
    """Узел дерева: файл или каталог с размером и числом версий."""

    path: str
    kind: str
    size: int = 0
    versions: int = 0
    version: int | None = None
    author: str | None = None
    ts: str | None = None
    depth: int = 0

    @property
    def is_dir(self) -> bool:
        """Каталог ли это."""
        return self.kind == "dir"

    def as_dict(self) -> dict[str, Any]:
        """Представить узел словарём."""
        return {
            "path": self.path,
            "kind": self.kind,
            "size": self.size,
            "versions": self.versions,
            "version": self.version,
            "author": self.author,
            "ts": self.ts,
            "depth": self.depth,
        }


@dataclass(frozen=True)
class TocItem:
    """Раздел документа с размером в символах."""

    title: str
    level: int
    chars: int
    lines: int

    @property
    def size(self) -> int:
        """Псевдоним размера в символах."""
        return self.chars

    def as_dict(self) -> dict[str, Any]:
        """Представить раздел словарём."""
        return {
            "title": self.title,
            "level": self.level,
            "chars": self.chars,
            "lines": self.lines,
        }


@dataclass(frozen=True)
class Link:
    """Внутренняя или внешняя ссылка из документа."""

    source: str
    target: str
    raw: str = ""
    text: str = ""
    kind: str = "markdown"
    exists: bool = False
    external: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Представить ссылку словарём."""
        return {
            "source": self.source,
            "target": self.target,
            "raw": self.raw,
            "text": self.text,
            "kind": self.kind,
            "exists": self.exists,
            "external": self.external,
        }


@dataclass(frozen=True)
class ChangesPage:
    """Порция ленты изменений с курсором аудита."""

    items: list[AuditEntry] = field(default_factory=list)
    cursor: int = 0
    next_cursor: int = 0
    limit: int = 100

    @property
    def has_more(self) -> bool:
        """Была ли порция заполнена целиком (возможно, есть ещё)."""
        return bool(self.items) and len(self.items) >= self.limit

    def as_dict(self) -> dict[str, Any]:
        """Представить порцию словарём."""
        return {
            "items": [item.as_dict() for item in self.items],
            "cursor": self.cursor,
            "next_cursor": self.next_cursor,
            "limit": self.limit,
            "has_more": self.has_more,
        }


@dataclass(frozen=True)
class ReadmeMap:
    """Карта памяти: README, его разделы/ссылки и верхний уровень."""

    readme: str | None
    title: str | None
    sections: list[TocItem] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    top_level: list[Node] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Представить карту словарём."""
        return {
            "readme": self.readme,
            "title": self.title,
            "sections": [item.as_dict() for item in self.sections],
            "links": [item.as_dict() for item in self.links],
            "top_level": [item.as_dict() for item in self.top_level],
        }


class Nav:
    """Навигация: дерево, оглавление, ссылки и изменения."""

    def __init__(self, store: Any) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # Документы и дерево
    # ------------------------------------------------------------------
    def _documents(self) -> list[dict[str, Any]]:
        with self.store._lock:
            self.store._ensure_open()
            rows = self.store._conn.execute(
                """
                SELECT d.path AS path, d.current_version AS current_version,
                       v.size AS size, v.author AS author, v.ts AS ts,
                       (SELECT COUNT(*) FROM versions x WHERE x.path=d.path) AS versions
                FROM documents d
                LEFT JOIN versions v ON v.path=d.path AND v.n=d.current_version
                WHERE d.deleted=0
                ORDER BY d.path
                """
            ).fetchall()
        return [
            {
                "path": row["path"],
                "size": int(row["size"] or 0),
                "versions": int(row["versions"] or 0),
                "version": (
                    int(row["current_version"])
                    if row["current_version"] is not None
                    else None
                ),
                "author": row["author"],
                "ts": row["ts"],
            }
            for row in rows
        ]

    @staticmethod
    def _in_prefix(path: str, prefix: str) -> bool:
        if not prefix:
            return True
        return path == prefix or path.startswith(prefix + "/")

    def _relative(self, path: str, prefix: str) -> str:
        if not prefix:
            return path
        if path == prefix:
            return ""
        return path[len(prefix) + 1 :]

    def ls(self, prefix: str = "") -> list[Node]:
        """Содержимое каталога: файлы и подкаталоги с размерами."""
        prefix = (prefix or "").strip().strip("/")
        docs = [doc for doc in self._documents() if self._in_prefix(doc["path"], prefix)]
        files: dict[str, dict[str, Any]] = {}
        dirs: dict[str, list[int]] = {}
        for doc in docs:
            rel = self._relative(doc["path"], prefix)
            if rel == "":
                files[doc["path"]] = doc
                continue
            parts = rel.split("/")
            if len(parts) == 1:
                files[doc["path"]] = doc
                continue
            dir_path = prefix + "/" + parts[0] if prefix else parts[0]
            stats = dirs.setdefault(dir_path, [0, 0])
            stats[0] += doc["size"]
            stats[1] += doc["versions"]
        nodes: list[Node] = [
            Node(path=path, kind="dir", size=stats[0], versions=stats[1])
            for path, stats in dirs.items()
        ]
        nodes.extend(
            Node(
                path=doc["path"],
                kind="file",
                size=doc["size"],
                versions=doc["versions"],
                version=doc["version"],
                author=doc["author"],
                ts=doc["ts"],
            )
            for doc in files.values()
        )
        nodes.sort(key=lambda node: (node.kind != "dir", node.path))
        return nodes

    def tree(self, prefix: str = "", max_depth: int | None = None) -> list[Node]:
        """Дерево от префикса в глубину, с агрегатами по каталогам."""
        prefix = (prefix or "").strip().strip("/")
        if max_depth is not None and (
            not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0
        ):
            raise ValidationError(
                "max_depth должен быть целым числом >= 0 или None.",
                hint="Передайте max_depth=2 или None для всего дерева.",
            )
        docs = [doc for doc in self._documents() if self._in_prefix(doc["path"], prefix)]
        dir_stats: dict[str, list[int]] = {}
        children: dict[str, set[str]] = {}
        file_by_path: dict[str, dict[str, Any]] = {}
        root_key = prefix
        children.setdefault(root_key, set())
        for doc in docs:
            rel = self._relative(doc["path"], prefix)
            if rel == "":
                file_by_path[doc["path"]] = doc
                continue
            parts = rel.split("/")
            parent = prefix
            for part in parts[:-1]:
                dir_path = f"{parent}/{part}" if parent else part
                children.setdefault(parent, set()).add(dir_path)
                children.setdefault(dir_path, set())
                stats = dir_stats.setdefault(dir_path, [0, 0])
                stats[0] += doc["size"]
                stats[1] += doc["versions"]
                parent = dir_path
            children.setdefault(parent, set()).add(doc["path"])
            file_by_path[doc["path"]] = doc

        def walk(parent: str, depth: int) -> Iterable[Node]:
            for child in sorted(children.get(parent, set())):
                if child in file_by_path:
                    doc = file_by_path[child]
                    yield Node(
                        path=doc["path"],
                        kind="file",
                        size=doc["size"],
                        versions=doc["versions"],
                        version=doc["version"],
                        author=doc["author"],
                        ts=doc["ts"],
                        depth=depth,
                    )
                else:
                    stats = dir_stats.get(child, [0, 0])
                    yield Node(
                        path=child,
                        kind="dir",
                        size=stats[0],
                        versions=stats[1],
                        depth=depth,
                    )
                    if max_depth is None or depth + 1 <= max_depth:
                        yield from walk(child, depth + 1)

        return list(walk(root_key, 0))

    # ------------------------------------------------------------------
    # Оглавление
    # ------------------------------------------------------------------
    def toc(
        self,
        path: str,
        version: int | None = None,
        *,
        actor: str | None = None,
    ) -> list[TocItem]:
        """Оглавление документа: заголовки и размеры разделов в символах."""
        result = self.store.read(path, version=version, actor=actor)
        items: list[TocItem] = []
        for section in _split_sections(result.content):
            title = section.title or "preamble"
            items.append(
                TocItem(
                    title=title,
                    level=section.level,
                    chars=len(section.text),
                    lines=len(section.text.splitlines()),
                )
            )
        return items

    # ------------------------------------------------------------------
    # Ссылки
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve(source: str, target: str) -> str:
        target = target.strip()
        if target.startswith("/"):
            joined = target.lstrip("/")
        else:
            joined = posixpath.join(posixpath.dirname(source), target)
        normalized = posixpath.normpath(joined)
        return "" if normalized == "." else normalized

    def _parse_links(self, source: str, text: str, existing: set[str]) -> list[Link]:
        links: list[Link] = []
        for match in _WIKI_RE.finditer(text):
            raw_target = match.group(1).strip()
            label = (match.group(2) or "").strip()
            links.append(
                self._make_link(
                    source, raw_target, match.group(0), label, "wiki", existing
                )
            )
        for match in _MD_RE.finditer(text):
            raw_target = match.group(2).strip()
            if not raw_target:
                continue
            links.append(
                self._make_link(
                    source,
                    raw_target,
                    match.group(0),
                    match.group(1).strip(),
                    "markdown",
                    existing,
                )
            )
        return links

    def _make_link(
        self,
        source: str,
        raw_target: str,
        raw: str,
        text: str,
        kind: str,
        existing: set[str],
    ) -> Link:
        target = raw_target
        external = bool(_SCHEME_RE.match(target)) or target.startswith("//")
        if external:
            return Link(
                source=source,
                target=target,
                raw=raw,
                text=text,
                kind=kind,
                exists=False,
                external=True,
            )
        target = target.split("#", 1)[0].split("?", 1)[0]
        if not target:
            return Link(
                source=source,
                target="",
                raw=raw,
                text=text,
                kind=kind,
                exists=True,
                external=False,
            )
        resolved = self._resolve(source, target)
        if kind == "wiki":
            root_target = posixpath.normpath(target.lstrip("/"))
            if resolved in existing:
                pass
            elif root_target in existing:
                resolved = root_target
        return Link(
            source=source,
            target=resolved,
            raw=raw,
            text=text,
            kind=kind,
            exists=resolved in existing,
            external=False,
        )

    def links(self, path: str, version: int | None = None) -> list[Link]:
        """Исходящие ссылки документа."""
        docs = self._documents()
        existing = {doc["path"] for doc in docs}
        result = self.store.read(path, version=version)
        return self._parse_links(path, result.content, existing)

    def backlinks(self, path: str, prefix: str = "") -> list[Link]:
        """Документы, которые ссылаются на указанный путь."""
        target = posixpath.normpath(path.strip().strip("/"))
        existing = {doc["path"] for doc in self._documents()}
        found: list[Link] = []
        for doc in self._documents():
            if prefix and not self._in_prefix(doc["path"], prefix):
                continue
            try:
                result = self.store.read(doc["path"])
            except Exception:
                continue
            for link in self._parse_links(doc["path"], result.content, existing):
                if not link.external and link.target == target:
                    found.append(link)
        return found

    def broken_links(self, prefix: str = "") -> list[Link]:
        """Внутренние ссылки, ведущие на несуществующие документы."""
        existing = {doc["path"] for doc in self._documents()}
        broken: list[Link] = []
        for doc in self._documents():
            if prefix and not self._in_prefix(doc["path"], prefix):
                continue
            try:
                result = self.store.read(doc["path"])
            except Exception:
                continue
            for link in self._parse_links(doc["path"], result.content, existing):
                if not link.external and link.target and not link.exists:
                    broken.append(link)
        return broken

    # ------------------------------------------------------------------
    # Лента изменений и карта
    # ------------------------------------------------------------------
    def changes_since(self, cursor: int | None = None, limit: int = 100) -> ChangesPage:
        """Изменения после курсора (id аудита), старые сначала."""
        if cursor is None:
            start = 0
        elif isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0:
            start = cursor
        elif isinstance(cursor, str) and cursor.strip().isdigit():
            start = int(cursor.strip())
        else:
            raise ValidationError(
                f"Некорректный курсор {cursor!r}.",
                hint="Передайте next_cursor из предыдущего changes_since() или None.",
            )
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValidationError(
                "limit должен быть целым числом от 1 до 1000.",
                hint="Передайте limit=100 (или другое число в диапазоне).",
            )
        placeholders = ",".join("?" for _ in _CHANGE_ACTIONS)
        with self.store._lock:
            self.store._ensure_open()
            max_row = self.store._conn.execute("SELECT MAX(id) AS m FROM audit").fetchone()
            max_id = int(max_row["m"] or 0)
            rows = self.store._conn.execute(
                f"""
                SELECT * FROM audit
                WHERE id > ? AND action IN ({placeholders})
                ORDER BY id ASC
                """,
                (start, *_CHANGE_ACTIONS),
            ).fetchall()
        items: list[AuditEntry] = []
        for row in rows:
            detail = _parse_detail(row["detail_json"])
            if detail.get("ok") is False:
                continue
            items.append(
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
            if len(items) >= limit:
                break
        next_cursor = max_id if len(items) < limit else items[-1].id
        return ChangesPage(items=items, cursor=start, next_cursor=next_cursor, limit=limit)

    def readme_map(self) -> ReadmeMap:
        """Карта памяти из README и верхнего уровня хранилища."""
        docs = self._documents()
        existing = {doc["path"] for doc in docs}
        readme_path = None
        for candidate in ("README", "README.md"):
            if candidate in existing:
                readme_path = candidate
                break
        top_level = self.ls("")
        if readme_path is None:
            return ReadmeMap(
                readme=None, title=None, sections=[], links=[], top_level=top_level
            )
        result = self.store.read(readme_path)
        sections = self.toc(readme_path)
        links = self._parse_links(readme_path, result.content, existing)
        title = sections[0].title if sections else None
        return ReadmeMap(
            readme=readme_path,
            title=title,
            sections=sections,
            links=links,
            top_level=top_level,
        )
