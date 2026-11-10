"""Общий слой операций интерфейсов Artek Brain (CLI, HTTP, MCP).

Слой только вызывает публичный фасад Brain, Access и TaskBoard. В ядре
по-прежнему нет сети и LLM. Все методы принимают токен и возвращают данные,
готовые к JSON-сериализации.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import Any, Iterable

from .access import Access, IssuedToken, Principal
from .brain import Brain
from .errors import PermissionDenied, ValidationError
from .quota import Quota
from .store import Store
from .tasks import TaskBoard

DEFAULT_ROOT = ".brain"


def resolve_root(root: str | Path | None = None) -> Path:
    """Найти каталог хранилища: аргумент, BRAIN_HOME или ./.brain."""
    if root is not None:
        return Path(root).expanduser()
    env = os.environ.get("BRAIN_HOME")
    if env:
        return Path(env).expanduser()
    return Path.cwd() / DEFAULT_ROOT


def _jsonable(value: Any) -> Any:
    """Привести результат фасада к JSON-совместимому виду."""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if hasattr(value, "as_dict"):
        return _jsonable(value.as_dict())
    if isinstance(value, str):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    return value


def _as_list(value: Iterable[Any] | str | None) -> list[Any] | None:
    """Разобрать список, строку с запятыми или None."""
    if value is None:
        return None
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    else:
        items = [item for item in value]
    cleaned = [item for item in items if item not in ("", None)]
    return cleaned or None


def _as_int(value: Any, name: str) -> int | None:
    """Привести значение к int с понятной ошибкой."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{name} должен быть целым числом.",
            hint=f"Передайте {name} числом, например {name}=1.",
        ) from exc


class Service:
    """Экземпляр хранилища, токенов, поиска и доски задач."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        quota: Quota | None = None,
        max_claims: int = 2,
    ) -> None:
        self.root = resolve_root(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root, quota=quota)
        self.access = Access.for_store(self.store)
        self.brain = Brain(self.store, self.access)
        self.board = TaskBoard(self.store, max_claims=max_claims)

    def __enter__(self) -> "Service":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Закрыть поисковый индекс, токены и хранилище."""
        try:
            self.brain.close()
        finally:
            try:
                self.access.close()
            finally:
                self.store.close()

    # ------------------------------------------------------------------
    # Токены
    # ------------------------------------------------------------------
    def principal(self, token: str | None) -> Principal:
        """Проверить токен и вернуть принципала."""
        return self.access.verify(token or "")

    def _require_owner(self, token: str | None, action: str) -> Principal:
        principal = self.principal(token)
        if principal.role != "owner":
            raise PermissionDenied(
                action,
                None,
                "owner",
                actor=principal.name,
                reason="управление токенами доступно только роли owner",
            )
        return principal

    def token_create(
        self,
        token: str | None,
        name: str,
        role: str = "agent",
        scopes: Iterable[Any] | str | None = None,
        ttl: int | float | None = None,
    ) -> dict[str, Any]:
        """Создать токен; сам токен возвращается один раз."""
        self._require_owner(token, "token_create")
        scope_list = _as_list(scopes)
        issued: IssuedToken = self.access.create_token(
            name, role=role, scopes=scope_list, ttl=ttl
        )
        return _jsonable(issued)

    def token_list(self, token: str | None) -> list[dict[str, Any]]:
        """Список токенов без секретов."""
        self._require_owner(token, "token_list")
        return _jsonable(self.access.list_tokens())

    def token_revoke(
        self,
        token: str | None,
        *,
        token_id: int | None = None,
        value: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Отозвать токен по id, значению или имени."""
        self._require_owner(token, "token_revoke")
        if token_id is not None:
            changed = self.access.revoke_by_id(int(token_id))
            return {"revoked": int(bool(changed)), "by": "id", "id": int(token_id)}
        if value:
            changed = self.access.revoke(value)
            return {"revoked": int(bool(changed)), "by": "value"}
        if name:
            changed = self.access.revoke_name(name)
            return {"revoked": int(changed), "by": "name", "name": name}
        raise ValidationError(
            "Не указан токен для отзыва.",
            hint="Передайте id, значение токена или имя.",
        )

    # ------------------------------------------------------------------
    # Документы
    # ------------------------------------------------------------------
    def read(
        self,
        token: str | None,
        path: str,
        *,
        version: int | None = None,
        section: str | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
        max_chars: int | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Прочитать документ, версию, раздел или диапазон строк."""
        line_range = None
        if line_start is not None or line_end is not None:
            start = _as_int(line_start, "line_start") or 1
            end = _as_int(line_end, "line_end")
            if end is None:
                end = 2**31 - 1
            line_range = (start, end)
        result = self.brain.read(
            token or "",
            path,
            version=_as_int(version, "version"),
            section=section,
            line_range=line_range,
            max_chars=_as_int(max_chars, "max_chars"),
            session=session,
            model=model,
        )
        return _jsonable(result)

    def write(
        self,
        token: str | None,
        path: str,
        content: str,
        reason: str,
        *,
        base_version: int | None = None,
        expected_sha256: str | None = None,
        batch_id: str | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Записать новую версию документа."""
        from .quality import Quality

        if token:
            Quality(self.brain).strict_check(token, path, content)
        result = self.brain.write(
            token or "",
            path,
            content,
            reason,
            base_version=_as_int(base_version, "base_version"),
            expected_sha256=expected_sha256,
            batch_id=batch_id,
            session=session,
            model=model,
        )
        return _jsonable(result)

    def delete(
        self,
        token: str | None,
        path: str,
        reason: str,
        *,
        base_version: int | None = None,
        batch_id: str | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Мягко удалить документ."""
        return _jsonable(
            self.brain.delete(
                token or "",
                path,
                reason,
                base_version=_as_int(base_version, "base_version"),
                batch_id=batch_id,
                session=session,
                model=model,
            )
        )

    def restore(
        self,
        token: str | None,
        path: str,
        reason: str,
        *,
        base_version: int | None = None,
        batch_id: str | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Восстановить мягко удалённый документ."""
        return _jsonable(
            self.brain.restore(
                token or "",
                path,
                reason,
                base_version=_as_int(base_version, "base_version"),
                batch_id=batch_id,
                session=session,
                model=model,
            )
        )

    def history(
        self,
        token: str | None,
        path: str,
        *,
        limit: int = 50,
        offset: int = 0,
        session: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """История версий документа."""
        return _jsonable(
            self.brain.history(
                token or "",
                path,
                limit=_as_int(limit, "limit") or 50,
                offset=_as_int(offset, "offset") or 0,
                session=session,
                model=model,
            )
        )

    def diff(
        self,
        token: str | None,
        path: str,
        *,
        a: int | None = None,
        b: int | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Unified diff между версиями."""
        text = self.brain.diff(
            token or "",
            path,
            _as_int(a, "a"),
            _as_int(b, "b"),
            session=session,
            model=model,
        )
        return {"path": path, "diff": text, "chars": len(text)}

    def blame(
        self,
        token: str | None,
        path: str,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Авторы разделов текущей версии."""
        return _jsonable(
            self.brain.blame(token or "", path, session=session, model=model)
        )

    def rollback_version(
        self,
        token: str | None,
        path: str,
        to_version: int,
        reason: str,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Откатить документ к указанной версии."""
        return _jsonable(
            self.brain.rollback_version(
                token or "",
                path,
                int(to_version),
                reason,
                session=session,
                model=model,
            )
        )

    def rollback_author(
        self,
        token: str | None,
        author: str,
        *,
        since: str | None = None,
        until: str | None = None,
        reason: str = "",
        session: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Откатить правки автора за период."""
        return _jsonable(
            self.brain.rollback_author(
                token or "",
                author,
                since,
                until,
                reason,
                session=session,
                model=model,
            )
        )

    def rollback_batch(
        self,
        token: str | None,
        batch_id: str,
        reason: str,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Откатить пачку правок."""
        return _jsonable(
            self.brain.rollback_batch(
                token or "",
                batch_id,
                reason,
                session=session,
                model=model,
            )
        )

    def export(
        self,
        token: str | None,
        path: str,
        *,
        version: int | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Экспортировать Markdown документа."""
        content = self.brain.export(
            token or "",
            path,
            version=_as_int(version, "version"),
            session=session,
            model=model,
        )
        return {"path": path, "version": version, "content": content}

    # ------------------------------------------------------------------
    # Поиск и навигация
    # ------------------------------------------------------------------
    def search(
        self,
        token: str | None,
        query: str,
        *,
        path_prefix: str | None = None,
        doc_type: str | None = None,
        status: str | None = None,
        tags: Iterable[str] | str | None = None,
        author: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
        offset: int = 0,
        history: bool = False,
        mode: str = "fts",
    ) -> dict[str, Any]:
        """Поиск по текущим версиям (fts/semantic/hybrid) или истории."""
        mode = str(mode or "fts").strip().lower()
        if history and mode != "fts":
            raise ValidationError(
                "Семантический и гибридный поиск доступны только по текущим версиям.",
                hint="Уберите history=true или выберите mode=fts.",
            )
        page = (
            self.brain.search_history
            if history
            else self.brain.search
        )(
            token or "",
            query,
            path_prefix=path_prefix,
            doc_type=doc_type,
            status=status,
            tags=_as_list(tags),
            author=author,
            since=since,
            until=until,
            limit=_as_int(limit, "limit") or 20,
            offset=_as_int(offset, "offset") or 0,
            **({} if history else {"mode": mode}),
        )
        return _jsonable(page)

    def ls(
        self, token: str | None, prefix: str = "", *, tree: bool = False,
        max_depth: int | None = None,
    ) -> dict[str, Any]:
        """Список каталога или дерево памяти."""
        if tree:
            nodes = self.brain.tree(
                token or "", prefix, _as_int(max_depth, "max_depth")
            )
        else:
            nodes = self.brain.ls(token or "", prefix)
        return {
            "prefix": prefix,
            "tree": bool(tree),
            "nodes": _jsonable(nodes),
        }

    def toc(
        self, token: str | None, path: str, *, version: int | None = None
    ) -> list[dict[str, Any]]:
        """Оглавление документа."""
        return _jsonable(
            self.brain.toc(token or "", path, version=_as_int(version, "version"))
        )

    def links(
        self, token: str | None, path: str, *, version: int | None = None
    ) -> dict[str, Any]:
        """Исходящие ссылки документа."""
        return {
            "path": path,
            "links": _jsonable(
                self.brain.links(
                    token or "", path, version=_as_int(version, "version")
                )
            ),
        }

    def backlinks(self, token: str | None, path: str) -> dict[str, Any]:
        """Обратные ссылки на документ."""
        return {"path": path, "backlinks": _jsonable(self.brain.backlinks(token or "", path))}

    def broken_links(self, token: str | None, prefix: str = "") -> dict[str, Any]:
        """Битые внутренние ссылки."""
        return {"prefix": prefix, "links": _jsonable(self.brain.broken_links(token or "", prefix))}

    def changes(
        self,
        token: str | None,
        *,
        cursor: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Лента изменений после курсора."""
        return _jsonable(
            self.brain.changes_since(
                token or "",
                cursor=_as_int(cursor, "cursor"),
                limit=_as_int(limit, "limit") or 100,
            )
        )

    def readme(self, token: str | None) -> dict[str, Any]:
        """Карта памяти из README и верхнего уровня."""
        return _jsonable(self.brain.readme_map(token or ""))

    # ------------------------------------------------------------------
    # Аудит и статистика
    # ------------------------------------------------------------------
    def audit(
        self,
        token: str | None,
        *,
        limit: int = 100,
        actor: str | None = None,
        action: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Журнал аудита."""
        return _jsonable(
            self.brain.audit(
                token or "",
                limit=_as_int(limit, "limit") or 100,
                actor=actor,
                action=action,
                since=since,
            )
        )

    def stats(self, token: str | None) -> dict[str, Any]:
        """Метрики хранилища."""
        return _jsonable(self.brain.stats(token or ""))

    def who_knows(self, token: str | None, path: str) -> dict[str, Any] | None:
        """Кто последним менял путь."""
        self.principal(token)
        return _jsonable(self.board.who_knows(path))

    # ------------------------------------------------------------------
    # Доска задач, inbox и бриф
    # ------------------------------------------------------------------
    def task(self, token: str | None, action: str, **params: Any) -> Any:
        """Мультиплексор операций доски задач.

        Автор, агент и отправитель всегда берутся из токена.
        """
        principal = self.principal(token)
        action = str(action or "").strip().lower().replace("-", "_")
        if action.startswith("task_") and len(action) > 5:
            action = action[5:]
        name = principal.name
        task_id = params.get("task_id") or params.get("id") or params.get("target")

        if action == "create":
            task = self.board.create(
                params.get("title") or "",
                params.get("body") or "",
                area=_as_list(params.get("area")) or [],
                author=name,
                priority=_as_int(params.get("priority"), "priority") or 2,
                depends_on=_as_list(params.get("depends_on")),
                key=params.get("key"),
            )
            return _jsonable(task)
        if action in ("list", "ls"):
            status = params.get("status")
            if isinstance(status, str) and "," in status:
                status = _as_list(status)
            tasks = self.board.list(
                status=status,
                assignee=params.get("assignee"),
                area=params.get("area") if isinstance(params.get("area"), str) else None,
                priority=_as_int(params.get("priority"), "priority"),
                limit=_as_int(params.get("limit"), "limit"),
                offset=_as_int(params.get("offset"), "offset") or 0,
            )
            return _jsonable(tasks)
        if action in ("show", "get"):
            return _jsonable(self.board.show(str(task_id or "")))
        if action == "claim":
            claim = self.board.claim(
                str(task_id or ""),
                name,
                revision=int(_as_int(params.get("revision"), "revision") or 0),
                minutes=_as_int(params.get("minutes"), "minutes") or 30,
            )
            return _jsonable(claim)
        if action == "start":
            return _jsonable(
                self.board.start(
                    str(task_id or ""),
                    name,
                    revision=int(_as_int(params.get("revision"), "revision") or 0),
                    token=params.get("claim_token"),
                )
            )
        if action == "heartbeat":
            claim = self.board.heartbeat(
                str(params.get("claim_token") or ""),
                minutes=_as_int(params.get("minutes"), "minutes") or 30,
                revision=_as_int(params.get("revision"), "revision"),
            )
            return _jsonable(claim)
        if action == "release":
            claim_token = params.get("claim_token")
            revision = _as_int(params.get("revision"), "revision")
            if claim_token:
                task = self.board.release(
                    str(task_id or ""), token=str(claim_token), revision=revision
                )
            else:
                task = self.board.release(
                    str(task_id or ""), name, revision=revision
                )
            return _jsonable(task)
        if action == "submit":
            return _jsonable(
                self.board.submit(
                    str(task_id or ""),
                    name,
                    evidence=params.get("evidence") or "",
                    revision=int(_as_int(params.get("revision"), "revision") or 0),
                    token=params.get("claim_token"),
                    body=params.get("body"),
                )
            )
        if action in ("accept", "rework"):
            self._ensure_reviewer(principal)
        if action == "accept":
            return _jsonable(
                self.board.accept(
                    str(task_id or ""),
                    name,
                    revision=int(_as_int(params.get("revision"), "revision") or 0),
                    note=params.get("note") or "",
                )
            )
        if action == "rework":
            return _jsonable(
                self.board.rework(
                    str(task_id or ""),
                    name,
                    revision=int(_as_int(params.get("revision"), "revision") or 0),
                    reason=params.get("reason") or "",
                    note=params.get("note"),
                )
            )
        if action == "block":
            return _jsonable(
                self.board.block(
                    str(task_id or ""),
                    name,
                    revision=int(_as_int(params.get("revision"), "revision") or 0),
                    reason=params.get("reason") or "",
                )
            )
        if action == "cancel":
            return _jsonable(
                self.board.cancel(
                    str(task_id or ""),
                    name,
                    revision=int(_as_int(params.get("revision"), "revision") or 0),
                    reason=params.get("reason") or "",
                )
            )
        if action in ("grant_role", "role_grant", "grant"):
            self._require_owner(token, "task_grant_role")
            actor = str(params.get("actor") or params.get("name") or "")
            self.board.grant_role(actor, str(params.get("role") or ""))
            return {"actor": actor, "role": params.get("role")}
        if action in ("revoke_role", "role_revoke", "revoke"):
            self._require_owner(token, "task_revoke_role")
            actor = str(params.get("actor") or params.get("name") or "")
            self.board.revoke_role(actor, str(params.get("role") or ""))
            return {"actor": actor, "role": params.get("role")}
        if action == "roles":
            actor = str(params.get("actor") or params.get("name") or name)
            return {"actor": actor, "roles": sorted(self.board.roles(actor))}
        if action == "presence":
            return _jsonable(self.board.presence(name, params.get("activity") or ""))
        if action in ("agents", "agents_online"):
            return _jsonable(
                self.board.agents_online(
                    ttl_seconds=_as_int(params.get("ttl_seconds"), "ttl_seconds") or 300
                )
            )
        if action in ("who_knows", "who"):
            return _jsonable(self.board.who_knows(str(params.get("path") or "")))
        if action == "handoff":
            return _jsonable(
                self.board.handoff(
                    str(params.get("task") or task_id or ""),
                    params.get("done") or "",
                    params.get("left") or "",
                    params.get("important", ""),
                    author=name,
                )
            )
        if action == "send":
            return _jsonable(
                self.board.send(
                    str(params.get("to") or ""),
                    params.get("subject") or "",
                    params.get("body") or "",
                    sender=name,
                )
            )
        if action == "inbox":
            return _jsonable(
                self.board.inbox(name, bool(params.get("unread", False)))
            )
        if action in ("mark_read", "read"):
            message_id = params.get("message_id")
            if message_id is None:
                raise ValidationError(
                    "Не указан message_id.",
                    hint="Возьмите id из inbox(action='inbox').",
                )
            changed = self.board.mark_read(message_id, name)
            return {"changed": changed}
        if action == "brief":
            return {
                "agent": name,
                "brief": self.board.brief(
                    name, limit=_as_int(params.get("limit"), "limit") or 2000
                ),
            }
        raise ValidationError(
            f"Неизвестное действие задачи «{action}».",
            hint=(
                "Допустимые действия: create, list, show, claim, start, "
                "heartbeat, release, submit, accept, rework, block, cancel, "
                "grant-role, revoke-role, roles, presence, agents, who-knows, "
                "handoff, send, inbox, mark-read, brief."
            ),
        )

    def _ensure_reviewer(self, principal: Principal) -> None:
        """Владельцу токена автоматически дать право ревью на доске."""
        if principal.role == "owner":
            roles = self.board.roles(principal.name)
            if "owner" not in roles:
                self.board.grant_role(principal.name, "owner")

    def inbox(
        self,
        token: str | None,
        action: str = "list",
        **params: Any,
    ) -> Any:
        """Список входящих, отправка или отметка прочтения."""
        action = (action or "list").strip().lower().replace("-", "_")
        if action in ("list", "ls", ""):
            return self.task(token, "inbox", **params)
        if action == "send":
            return self.task(token, "send", **params)
        if action in ("read", "mark_read"):
            return self.task(token, "mark_read", **params)
        raise ValidationError(
            f"Неизвестное действие inbox «{action}».",
            hint="Допустимо: list, send, read.",
        )

    def brief(self, token: str | None, *, limit: int = 2000) -> dict[str, Any]:
        """Короткий бриф для агента из токена."""
        principal = self.principal(token)
        return {
            "agent": principal.name,
            "brief": self.board.brief(
                principal.name, limit=_as_int(limit, "limit") or 2000
            ),
        }
