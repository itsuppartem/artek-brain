"""Фасад Brain: операции хранилища с проверкой токена и scope.

Каждый вызов принимает токен первым аргументом. Автор записи всегда берётся
из токена. Отказы по правам пишутся в журнал аудита хранилища.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .upkeep import load_settings as load_upkeep_settings
from .access import Access, Principal, is_hidden
from .errors import PermissionDenied, ValidationError
from .nav import ChangesPage, Link, Nav, Node, ReadmeMap, TocItem
from .search import Search, SearchPage
from .semantic import (
    SemanticDisabledError,
    SemanticIndex,
    hybrid_search as _hybrid_search,
)
from .store import (
    AuditEntry,
    ReadResult,
    RollbackReport,
    SectionBlame,
    VersionInfo,
    WriteResult,
)


class Brain:
    """Проверяющий права фасад над Store, поиском и навигацией."""

    def __init__(
        self,
        store: Any,
        access: Access,
        *,
        semantic: SemanticIndex | None = None,
    ) -> None:
        self.store = store
        self.access = access
        self.search_engine = Search(store)
        self.nav = Nav(store)
        self.approval_guard: Any = None
        self.semantic = (
            semantic if semantic is not None else SemanticIndex.from_store(store)
        )
        if self.semantic is not None:
            self.semantic.set_candidate_provider(self._semantic_candidates)

    def __enter__(self) -> "Brain":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Закрыть индексы поиска (Store и Access закрывает вызывающий)."""
        try:
            self.search_engine.close()
        finally:
            if self.semantic is not None:
                self.semantic.close()

    def _semantic_candidates(self, query: str) -> list[str]:
        """FTS-кандидаты для предфильтра большого семантического индекса."""
        page = self.search_engine.search(query, limit=1000)
        return [item.path for item in page.items]

    def _update_semantic(self, path: str) -> None:
        """Хук рядом с FTS: обновить семантический индекс, если он включён."""
        if self.semantic is not None:
            self.semantic.update(path)

    # ------------------------------------------------------------------
    # Проверки
    # ------------------------------------------------------------------
    def _normalize(self, path: str) -> str:
        return self.store._normalize_path(path)

    def _deny(self, action: str, path: str | None, exc: PermissionDenied) -> None:
        try:
            self.store._log_failure(action, path, exc.actor, exc, None, None)
        except Exception:
            pass

    def _verify(self, token: str, action: str, path: str | None) -> Principal:
        try:
            return self.access.verify(token)
        except PermissionDenied as exc:
            self._deny(action, path, exc)
            raise

    def _guard_read(self, token: str, action: str, path: str) -> Principal:
        principal = self._verify(token, action, path)
        try:
            self.access.require_read(principal, path, action=action)
        except PermissionDenied as exc:
            self._deny(action, path, exc)
            raise
        return principal

    def _guard_write(
        self, token: str, action: str, path: str, *, creating: bool = False
    ) -> Principal:
        principal = self._verify(token, action, path)
        try:
            self.access.require_write(
                principal, path, action=action, creating=creating
            )
            self._guard_decision(principal, action, path)
        except PermissionDenied as exc:
            self._deny(action, path, exc)
            raise
        return principal

    def _guard_decision(self, principal: Principal, action: str, path: str) -> None:
        """Решения меняет только владелец, если это не выключено в настройках."""
        settings = load_upkeep_settings(self.store.root)
        if (
            settings.require_owner
            and settings.is_decision(path)
            and principal.role not in ("owner", "human")
        ):
            raise PermissionDenied(
                action,
                path,
                "owner",
                actor=principal.name,
                reason=(
                    "решения утверждает владелец: подайте предложение "
                    f'(brain_op(action="propose", target="{path}", content=…, reason=…))'
                ),
            )

    def _guard_journal(self, token: str, action: str) -> Principal:
        principal = self._verify(token, action, None)
        try:
            self.access.require_journal(principal, action=action)
        except PermissionDenied as exc:
            self._deny(action, None, exc)
            raise
        return principal

    def _document_exists(self, path: str) -> bool:
        with self.store._lock:
            self.store._ensure_open()
            return self.store._get_doc_locked(path) is not None

    def _versions_by(self, sql: str, params: Sequence[Any]) -> list[Any]:
        with self.store._lock:
            self.store._ensure_open()
            return self.store._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------
    def read(
        self,
        token: str,
        path: str,
        version: int | None = None,
        section: str | None = None,
        line_range: Sequence[int] | None = None,
        max_chars: int | None = None,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> ReadResult:
        """Прочитать документ с проверкой права read."""
        path = self._normalize(path)
        principal = self._guard_read(token, "read", path)
        return self.store.read(
            path,
            version=version,
            section=section,
            line_range=line_range,
            max_chars=max_chars,
            actor=principal.name,
            session=session,
            model=model,
        )

    def history(
        self,
        token: str,
        path: str,
        limit: int = 50,
        offset: int = 0,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> list[VersionInfo]:
        """История версий с проверкой права read."""
        path = self._normalize(path)
        principal = self._guard_read(token, "history", path)
        return self.store.history(
            path, limit=limit, offset=offset, actor=principal.name,
            session=session, model=model,
        )

    def diff(
        self,
        token: str,
        path: str,
        a: int | None = None,
        b: int | None = None,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> str:
        """Сравнить версии с проверкой права read."""
        path = self._normalize(path)
        principal = self._guard_read(token, "diff", path)
        return self.store.diff(
            path, a, b, actor=principal.name, session=session, model=model
        )

    def blame(
        self,
        token: str,
        path: str,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> list[SectionBlame]:
        """Авторы разделов с проверкой права read."""
        path = self._normalize(path)
        principal = self._guard_read(token, "blame", path)
        return self.store.blame(
            path, actor=principal.name, session=session, model=model
        )

    # ------------------------------------------------------------------
    # Запись и откаты
    # ------------------------------------------------------------------
    def write(
        self,
        token: str,
        path: str,
        content: str,
        reason: str,
        base_version: int | None = None,
        expected_sha256: str | None = None,
        batch_id: str | None = None,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> WriteResult:
        """Записать версию; автор — имя из токена."""
        path = self._normalize(path)
        creating = not self._document_exists(path)
        principal = self._guard_write(token, "write", path, creating=creating)
        if self.approval_guard is not None:
            self.approval_guard(principal, path, content)
        result = self.store.write(
            path,
            content,
            principal.name,
            reason,
            base_version,
            expected_sha256,
            batch_id,
            session=session,
            model=model,
        )
        self.search_engine.update(path)
        self._update_semantic(path)
        return result

    def overwrite(
        self,
        token: str,
        path: str,
        content: str,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> WriteResult:
        """Правка текущей версии на месте без новой версии; след остаётся в журнале.

        Только для владельца: это сознательный отказ от истории версий.
        """
        path = self._normalize(path)
        principal = self._guard_write(token, "write", path)
        if principal.role != "owner":
            exc = PermissionDenied(
                "overwrite",
                path,
                "owner",
                actor=principal.name,
                reason="правка на горячую доступна только владельцу",
            )
            self._deny("write", path, exc)
            raise exc
        if self.approval_guard is not None:
            self.approval_guard(principal, path, content)
        result = self.store.overwrite_current(
            path, content, principal.name, session=session, model=model
        )
        self.search_engine.update(path)
        self._update_semantic(path)
        return result

    def delete(
        self,
        token: str,
        path: str,
        reason: str,
        base_version: int | None = None,
        batch_id: str | None = None,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> WriteResult:
        """Мягко удалить документ с проверкой права write."""
        path = self._normalize(path)
        principal = self._guard_write(token, "delete", path)
        result = self.store.delete(
            path,
            principal.name,
            reason,
            base_version,
            batch_id,
            session=session,
            model=model,
        )
        self.search_engine.update(path)
        self._update_semantic(path)
        return result

    def restore(
        self,
        token: str,
        path: str,
        reason: str,
        base_version: int | None = None,
        batch_id: str | None = None,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> WriteResult:
        """Восстановить документ с проверкой права write."""
        path = self._normalize(path)
        principal = self._guard_write(token, "restore", path)
        result = self.store.restore(
            path,
            principal.name,
            reason,
            base_version,
            batch_id,
            session=session,
            model=model,
        )
        self.search_engine.update(path)
        self._update_semantic(path)
        return result

    def _deny_agent_rollback(
        self, action: str, path: str | None, principal: Principal, version: int, author: str
    ) -> None:
        exc = PermissionDenied(
            action,
            path,
            "owner",
            actor=principal.name,
            reason=(
                f"агент не может откатывать чужие правки "
                f"(версия {version}, автор {author})"
            ),
        )
        self._deny(action, path, exc)
        raise exc

    def rollback_version(
        self,
        token: str,
        path: str,
        to_version: int,
        reason: str,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> WriteResult:
        """Откатить документ к версии; агент не трогает чужие правки."""
        path = self._normalize(path)
        principal = self._guard_write(token, "rollback_version", path)
        if principal.role == "agent":
            history = self.store.history(path, limit=5000)
            for item in history:
                if item.n > int(to_version) and item.author != principal.name:
                    self._deny_agent_rollback(
                        "rollback_version", path, principal, item.n, item.author
                    )
        result = self.store.rollback_version(
            path,
            to_version,
            principal.name,
            reason,
            session=session,
            model=model,
        )
        self.search_engine.update(path)
        self._update_semantic(path)
        return result

    def rollback_author(
        self,
        token: str,
        author: str,
        since: str | None,
        until: str | None,
        reason: str,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> RollbackReport:
        """Откатить правки автора за период с проверкой прав на их пути."""
        principal = self._verify(token, "rollback_author", None)
        if principal.role == "agent" and author != principal.name:
            exc = PermissionDenied(
                "rollback_author",
                None,
                "owner",
                actor=principal.name,
                reason=f"агент не может откатывать чужие правки (автор {author})",
            )
            self._deny("rollback_author", None, exc)
            raise exc
        rows = self._versions_by(
            "SELECT DISTINCT path FROM versions WHERE author=? ORDER BY path", (author,)
        )
        for row in rows:
            path = row["path"]
            try:
                self.access.require_write(
                    principal, path, action="rollback_author", creating=False
                )
            except PermissionDenied as exc:
                self._deny("rollback_author", path, exc)
                raise
        result = self.store.rollback_author(
            author, since, until, principal.name, reason,
            session=session, model=model,
        )
        for item in result.rolled_back:
            self.search_engine.update(item.path)
            self._update_semantic(item.path)
        return result

    def rollback_batch(
        self,
        token: str,
        batch_id: str,
        reason: str,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> RollbackReport:
        """Откатить пачку; агент может только свою и на доступных путях."""
        principal = self._verify(token, "rollback_batch", None)
        rows = self._versions_by(
            "SELECT path, author FROM versions WHERE batch_id=? ORDER BY path",
            (batch_id,),
        )
        if principal.role == "agent":
            for row in rows:
                if row["author"] != principal.name:
                    self._deny_agent_rollback(
                        "rollback_batch", row["path"], principal, 0, row["author"]
                    )
        seen: set[str] = set()
        for row in rows:
            path = row["path"]
            if path in seen:
                continue
            seen.add(path)
            try:
                self.access.require_write(
                    principal, path, action="rollback_batch", creating=False
                )
            except PermissionDenied as exc:
                self._deny("rollback_batch", path, exc)
                raise
        result = self.store.rollback_batch(
            batch_id, reason=reason, actor=principal.name,
            session=session, model=model,
        )
        for item in result.rolled_back:
            self.search_engine.update(item.path)
            self._update_semantic(item.path)
        return result

    # ------------------------------------------------------------------
    # Аудит и статистика
    # ------------------------------------------------------------------
    def audit(
        self,
        token: str,
        limit: int = 100,
        actor: str | None = None,
        action: str | None = None,
        since: str | None = None,
    ) -> list[AuditEntry]:
        """Журнал аудита: owner или auditor; hidden скрыт от не-owner."""
        principal = self._guard_journal(token, "audit")
        entries = self.store.audit(limit=limit, actor=actor, action=action, since=since)
        if principal.role == "owner":
            return entries
        result: list[AuditEntry] = []
        for entry in entries:
            if entry.path and is_hidden(entry.path):
                result.append(
                    AuditEntry(
                        id=entry.id,
                        ts=entry.ts,
                        actor=entry.actor,
                        action=entry.action,
                        path=None,
                        detail={"hidden": True},
                        session=entry.session,
                        model=entry.model,
                    )
                )
            else:
                result.append(entry)
        return result

    def stats(self, token: str) -> dict[str, Any]:
        """Статистика хранилища: owner или auditor."""
        self._guard_journal(token, "stats")
        return self.store.stats()

    def export(
        self,
        token: str,
        path: str,
        version: int | None = None,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> str:
        """Экспорт Markdown с проверкой права read."""
        path = self._normalize(path)
        principal = self._guard_read(token, "export", path)
        return self.store.export_markdown(
            path, version=version, actor=principal.name,
            session=session, model=model,
        )

    # ------------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------------
    def _check_prefix(
        self,
        principal: Principal,
        path_prefix: str | None,
        *,
        action: str = "search",
    ) -> None:
        if principal.role == "owner" or not path_prefix:
            return
        prefix = path_prefix.strip().strip("/")
        if not prefix:
            return
        if not principal.can_read(prefix):
            exc = PermissionDenied(
                action,
                prefix,
                "read",
                actor=principal.name,
                reason="нет права read на запрошенный префикс пути",
            )
            self._deny(action, prefix, exc)
            raise exc

    def search(
        self,
        token: str,
        query: str,
        *,
        path_prefix: str | None = None,
        type: str | None = None,
        doc_type: str | None = None,
        status: str | None = None,
        tags: Sequence[str] | str | None = None,
        author: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
        offset: int = 0,
        mode: str = "fts",
    ) -> SearchPage:
        """Поиск по текущим версиям: fts, semantic или hybrid (RRF)."""
        doc_type = doc_type or type
        mode = str(mode or "fts").strip().lower()
        if mode not in ("fts", "semantic", "hybrid"):
            raise ValidationError(
                f"Неизвестный режим поиска «{mode}».",
                hint="Допустимо: fts, semantic или hybrid.",
            )
        principal = self._verify(token, "search", path_prefix)
        if principal.role == "auditor":
            exc = PermissionDenied(
                "search",
                path_prefix,
                "read",
                actor=principal.name,
                reason="роль auditor читает только журнал аудита и статистику",
            )
            self._deny("search", path_prefix, exc)
            raise exc
        self._check_prefix(principal, path_prefix, action="search")
        common = {
            "path_prefix": path_prefix,
            "doc_type": doc_type,
            "status": status,
            "tags": tags,
            "author": author,
            "since": since,
            "until": until,
            "limit": limit,
            "offset": offset,
            "can_read": principal.can_read,
        }
        if mode == "fts":
            return self.search_engine.search(query, **common)
        if self.semantic is None:
            raise SemanticDisabledError(
                "Семантический поиск выключен в настройках хранилища.",
                hint=(
                    "Включите блок semantic в settings.json "
                    "(enabled=true, embedder=hash или ollama) и пересоздайте Brain."
                ),
            )
        if mode == "semantic":
            return self.semantic.search(query, **common)
        return _hybrid_search(self.search_engine, self.semantic, query, **common)

    def semantic_search(self, token: str, query: str, **params: Any) -> SearchPage:
        """Семантический поиск через фасад Brain."""
        return self.search(token, query, mode="semantic", **params)

    def hybrid_search(self, token: str, query: str, **params: Any) -> SearchPage:
        """Гибридный поиск (FTS + семантика, RRF) через фасад Brain."""
        return self.search(token, query, mode="hybrid", **params)

    def reindex_semantic(self, token: str) -> dict[str, Any]:
        """Перестроить семантический индекс (только owner)."""
        principal = self._verify(token, "reindex_semantic", None)
        if principal.role != "owner":
            exc = PermissionDenied(
                "reindex_semantic",
                None,
                "owner",
                actor=principal.name,
                reason="перестройка семантического индекса доступна только owner",
            )
            self._deny("reindex_semantic", None, exc)
            raise exc
        if self.semantic is None:
            raise SemanticDisabledError(
                "Семантический поиск выключен в настройках хранилища.",
                hint="Включите semantic в settings.json и пересоздайте Brain.",
            )
        chunks = self.semantic.reindex()
        return {"chunks": chunks, **self.semantic.stats()}

    def search_history(
        self,
        token: str,
        query: str,
        *,
        path_prefix: str | None = None,
        type: str | None = None,
        doc_type: str | None = None,
        status: str | None = None,
        tags: Sequence[str] | str | None = None,
        author: str | None = None,
        since: str | None = None,
        until: str | None = None,
        version: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchPage:
        """Поиск по всем версиям документов с фильтрами и правами."""
        doc_type = doc_type or type
        principal = self._verify(token, "search_history", path_prefix)
        if principal.role == "auditor":
            exc = PermissionDenied(
                "search_history",
                path_prefix,
                "read",
                actor=principal.name,
                reason="роль auditor читает только журнал аудита и статистику",
            )
            self._deny("search_history", path_prefix, exc)
            raise exc
        self._check_prefix(principal, path_prefix, action="search_history")
        return self.search_engine.search_history(
            query,
            path_prefix=path_prefix,
            doc_type=doc_type,
            status=status,
            tags=tags,
            author=author,
            since=since,
            until=until,
            version=version,
            limit=limit,
            offset=offset,
            can_read=principal.can_read,
        )

    # ------------------------------------------------------------------
    # Навигация
    # ------------------------------------------------------------------
    def _node_readable(self, principal: Principal, node: Node) -> bool:
        if principal.role == "owner":
            return True
        if principal.can_read(node.path):
            return True
        if node.kind == "dir":
            return any(
                principal.can_read(doc["path"])
                for doc in self.nav._documents()
                if doc["path"].startswith(node.path + "/")
            )
        return False

    def _filter_nodes(
        self, principal: Principal, nodes: Iterable[Node]
    ) -> list[Node]:
        return [node for node in nodes if self._node_readable(principal, node)]

    def ls(self, token: str, prefix: str = "") -> list[Node]:
        """Содержимое каталога с учётом прав."""
        principal = self._verify(token, "ls", prefix or None)
        return self._filter_nodes(principal, self.nav.ls(prefix))

    def tree(
        self, token: str, prefix: str = "", max_depth: int | None = None
    ) -> list[Node]:
        """Дерево памяти с учётом прав."""
        principal = self._verify(token, "tree", prefix or None)
        return self._filter_nodes(principal, self.nav.tree(prefix, max_depth))

    def toc(
        self, token: str, path: str, version: int | None = None
    ) -> list[TocItem]:
        """Оглавление документа с проверкой права read."""
        path = self._normalize(path)
        principal = self._guard_read(token, "toc", path)
        return self.nav.toc(path, version=version, actor=principal.name)

    def links(self, token: str, path: str, version: int | None = None) -> list[Link]:
        """Исходящие ссылки документа с проверкой права read."""
        path = self._normalize(path)
        self._guard_read(token, "links", path)
        return self.nav.links(path, version=version)

    def backlinks(self, token: str, path: str) -> list[Link]:
        """Обратные ссылки с проверкой права read на цель и источники."""
        path = self._normalize(path)
        principal = self._guard_read(token, "backlinks", path)
        return [
            link
            for link in self.nav.backlinks(path)
            if principal.role == "owner" or principal.can_read(link.source)
        ]

    def broken_links(self, token: str, prefix: str = "") -> list[Link]:
        """Битые внутренние ссылки в доступных документах."""
        principal = self._verify(token, "broken_links", prefix or None)
        return [
            link
            for link in self.nav.broken_links(prefix)
            if principal.role == "owner" or principal.can_read(link.source)
        ]

    def changes_since(self, token: str, cursor: int | None = None, limit: int = 100) -> ChangesPage:
        """Лента изменений после курсора с фильтром по доступным путям."""
        principal = self._verify(token, "changes_since", None)
        if principal.role == "auditor":
            exc = PermissionDenied(
                "changes_since",
                None,
                "read",
                actor=principal.name,
                reason="роль auditor читает только журнал аудита и статистику",
            )
            self._deny("changes_since", None, exc)
            raise exc
        page = self.nav.changes_since(cursor, limit)
        items = [
            item
            for item in page.items
            if item.path is None
            or principal.role == "owner"
            or principal.can_read(item.path)
        ]
        return ChangesPage(
            items=items,
            cursor=page.cursor,
            next_cursor=page.next_cursor,
            limit=page.limit,
        )

    def readme_map(self, token: str) -> ReadmeMap:
        """Карта памяти из README и верхнего уровня с учётом прав."""
        principal = self._verify(token, "readme_map", None)
        if principal.role == "auditor":
            exc = PermissionDenied(
                "readme_map",
                None,
                "read",
                actor=principal.name,
                reason="роль auditor читает только журнал аудита и статистику",
            )
            self._deny("readme_map", None, exc)
            raise exc
        result = self.nav.readme_map()
        readme = result.readme
        if readme is not None and principal.role != "owner" and not principal.can_read(readme):
            readme = None
        links = [
            link
            for link in result.links
            if principal.role == "owner"
            or principal.can_read(link.target or result.readme or "")
        ]
        top_level = self._filter_nodes(principal, result.top_level)
        return ReadmeMap(
            readme=readme,
            title=result.title if readme else None,
            sections=result.sections if readme else [],
            links=links if readme else [],
            top_level=top_level,
        )


# Удобные псевдонимы фасада.
Brain.rollback = Brain.rollback_version
Brain.search_versions = Brain.search_history
