"""Общий слой операций интерфейсов Artek Brain (CLI, HTTP, MCP).

Слой только вызывает публичный фасад Brain, Access и Coordination. В ядре
по-прежнему нет сети и LLM. Все методы принимают токен и возвращают данные,
готовые к JSON-сериализации.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any, Iterable, Mapping

from .access import Access, IssuedToken, Principal
from .anomaly import AnomalyConfig, AnomalyDetector
from .brain import Brain
from .coord import Coordination
from .errors import ArtekBrainError, PermissionDenied, ValidationError
from .proposals import ProposalStore
from .quota import Quota
from .review_queue import ReviewQueue
from .registry import Registry
from .sandbox import Sandbox, SandboxConfig, SandboxLimits
from .store import Store
from .upkeep import Upkeep

_SANDBOX_SCHEMA = {
    "type": "object",
    "properties": {
        "script": {"type": "string", "description": "Текст скрипта."},
        "language": {
            "type": "string",
            "enum": ["python", "shell"],
            "description": "Язык: python или shell.",
        },
        "inputs": {
            "type": "object",
            "description": (
                "Срез данных: prefix или paths; необязательные "
                "max_documents и max_bytes."
            ),
        },
        "limits": {
            "type": "object",
            "description": "Лимиты запуска в пределах потолка роли.",
        },
        "cache": {"type": "boolean", "description": "Вернуть кэш по hash+срезу."},
    },
    "required": ["script"],
    "additionalProperties": False,
}

DEFAULT_ROOT = ".brain"


def resolve_root(root: str | Path | None = None) -> Path:
    """Найти каталог хранилища: аргумент, BRAIN_HOME, ближайший .brain вверх
    от текущего каталога (так git-копии агентов видят память проекта) или ./.brain."""
    if root is not None:
        return Path(root).expanduser()
    env = os.environ.get("BRAIN_HOME")
    if env:
        return Path(env).expanduser()
    cwd = Path.cwd()
    for folder in (cwd, *cwd.parents):
        candidate = folder / DEFAULT_ROOT
        if candidate.is_dir():
            return candidate
    return cwd / DEFAULT_ROOT


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


ON_BEHALF_MARK = "[по просьбе: "


class Service:
    """Экземпляр хранилища, токенов, поиска и координации агентов."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        quota: Quota | None = None,
        repo_root: str | Path | None = None,
    ) -> None:
        self.root = resolve_root(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root, quota=quota)
        self.access = Access.for_store(self.store)
        self.brain = Brain(self.store, self.access)
        self.review_queue = ReviewQueue(self.store, self.access)
        self.coord = Coordination(self.store)
        self.proposals = ProposalStore(self.store, self.access)
        self.upkeep = Upkeep(self.store, self.coord)
        self.anomalies = AnomalyDetector(
            self.store, access=self.access, coord=self.coord
        )
        self.brain.approval_guard = self.proposals.guard_write
        self.sandbox = Sandbox(SandboxConfig(), self.store, self.access)
        self.registry = Registry(self.store, self.access)
        self.registry.register(
            "sandbox.run",
            (
                "Выполнить Python или shell скрипт в изолированной песочнице "
                "рядом с read-only срезом памяти. Сеть выключена, секреты "
                "хоста не видны, вывод и файлы /out ограничены."
            ),
            _SANDBOX_SCHEMA,
            min_role="agent",
            daily_limit=100,
            handler=self._sandbox_handler,
        )

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
        """Закрыть песочницу, реестр, индекс, токены и хранилище."""
        try:
            self.sandbox.shutdown()
        finally:
            try:
                self.registry.close()
            finally:
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

    def token_suspend(
        self,
        token: str | None,
        *,
        token_id: int | None = None,
        value: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Приостановить токен (только owner)."""
        self._require_owner(token, "token_suspend")
        if token_id is not None:
            changed = self.access.suspend_by_id(int(token_id))
            return {"suspended": int(bool(changed)), "by": "id", "id": int(token_id)}
        if value:
            changed = self.access.suspend(value)
            return {"suspended": int(bool(changed)), "by": "value"}
        if name:
            changed = self.access.suspend_name(name)
            return {"suspended": int(changed), "by": "name", "name": name}
        raise ValidationError(
            "Не указан токен для приостановки.",
            hint="Передайте id, значение токена или имя.",
        )

    def token_resume(
        self,
        token: str | None,
        *,
        token_id: int | None = None,
        value: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Снять приостановку токена (только owner)."""
        self._require_owner(token, "token_resume")
        if token_id is not None:
            changed = self.access.resume_by_id(int(token_id))
            return {"resumed": int(bool(changed)), "by": "id", "id": int(token_id)}
        if value:
            changed = self.access.resume(value)
            return {"resumed": int(bool(changed)), "by": "value"}
        if name:
            changed = self.access.resume_name(name)
            return {"resumed": int(changed), "by": "name", "name": name}
        raise ValidationError(
            "Не указан токен для снятия приостановки.",
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
        data = _jsonable(result)
        if version is None and isinstance(data, dict):
            self._attach_warning(token, data, session, model)
        return data

    def _sync_upkeep(self, **options: Any) -> None:
        """Обновить задачи; сбой проверки не должен мешать работе агента."""
        try:
            self.upkeep.sync(**options)
        except Exception:  # noqa: BLE001 - запись уже прошла, чтение не должно падать
            pass

    def _log_warning(
        self,
        actor: str | None,
        surface: str,
        path: str | None,
        detail: dict[str, Any],
        session: str | None,
        model: str | None,
    ) -> None:
        """Записать показ предупреждения; журнал не должен ломать чтение."""
        try:
            with self.store._lock, self.store._conn:
                self.store._audit_ok(
                    actor, "stale_warning", path, {"surface": surface, **detail}, session, model
                )
        except sqlite3.Error:
            pass

    def _attach_warning(
        self, token: str | None, data: dict[str, Any], session: str | None, model: str | None
    ) -> None:
        path = str(data.get("path") or "")
        if not path:
            return
        self._sync_upkeep()
        try:
            warning = self.upkeep.warning_for(path)
        except (sqlite3.Error, OSError, ValueError):
            return
        if warning is None:
            return
        data["freshness"] = warning
        self._log_warning(
            self.principal(token).name, "read", path,
            {"status": warning["status"], "tasks": [item["id"] for item in warning["tasks"]]},
            session, model,
        )

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
        on_behalf: str | None = None,
    ) -> dict[str, Any]:
        """Записать новую версию; on_behalf — по чьей просьбе сделана правка."""
        from .quality import Quality

        if on_behalf:
            reason = f"{reason} {ON_BEHALF_MARK}{str(on_behalf).strip()[:60]}]"
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
        self.store.checkpoint_passive()
        self._sync_upkeep()
        return _jsonable(result)

    def overwrite(
        self,
        token: str | None,
        path: str,
        content: str,
        *,
        session: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Правка текущей версии на месте без новой версии; след в журнале (владелец)."""
        return _jsonable(
            self.brain.overwrite(
                token or "",
                path,
                content,
                session=session,
                model=model,
            )
        )

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
        session: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Поиск по текущим версиям (fts/semantic/hybrid) или истории.

        Успешный поиск пишется в журнал аудита (action=search): запрос,
        режим и число найденного — так видно, что агенты ищут и не находят.
        """
        mode = str(mode or "fts").strip().lower()
        if history and mode != "fts":
            raise ValidationError(
                "Семантический и гибридный поиск доступны только по текущим версиям.",
                hint="Уберите history=true или выберите mode=fts.",
            )
        warning: str | None = None
        if mode == "hybrid" and self.brain.semantic is None:
            mode = "fts"
            warning = (
                "Семантика выключена, поэтому hybrid-поиск выполнен как fts. "
                "Включите её: brain semantic enable --embedder hash."
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
        data = _jsonable(page)
        if warning:
            data["warning"] = warning
        try:
            actor = self.brain.access.verify(token or "").name
        except ArtekBrainError:  # pragma: no cover - токен уже проверен поиском
            actor = None
        # Запись в журнал — в своей короткой транзакции с коммитом: без
        # with-блока sqlite3 оставляет транзакцию открытой, и процесс держит
        # блокировку записи (database is locked у всех остальных).
        with self.store._lock, self.store._conn:
            self.store._audit_ok(
                actor,
                "search_history" if history else "search",
                path_prefix,
                {
                    "query": str(query)[:200],
                    "mode": mode,
                    "hits": int(data.get("total", 0) or 0),
                    "match": data.get("match", "all"),
                },
                session,
                model,
            )
        if not history:
            self._annotate_search(token, data, session, model)
        return data

    def _annotate_search(
        self, token: str | None, data: dict[str, Any], session: str | None, model: str | None
    ) -> None:
        """Пометить устаревшие документы прямо в результатах поиска."""
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return
        self._sync_upkeep()
        try:
            marks = self.upkeep.warnings_for([str(item.get("path")) for item in items])
        except (sqlite3.Error, OSError, ValueError):
            return
        if not marks:
            return
        for item in items:
            mark = marks.get(str(item.get("path")))
            if mark:
                item["stale"] = True
                item["stale_reason"] = mark
        data["stale_paths"] = sorted(marks)
        data["stale_note"] = "Часть результатов устарела: сверь факты перед опорой."
        self._log_warning(
            self.principal(token).name, "search", None, {"paths": sorted(marks)[:20]},
            session, model,
        )

    # ------------------------------------------------------------------
    # Задачи на обновление
    # ------------------------------------------------------------------
    def upkeep_list(
        self,
        token: str | None,
        *,
        path: str | None = None,
        status: str = "open",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Задачи на обновление памяти."""
        self.principal(token)
        self._sync_upkeep()
        normalized = self.store._normalize_path(path) if path else None
        items = self.upkeep.tasks(
            path=normalized, status=status, limit=_as_int(limit, "limit") or 100
        )
        return {"status": status, "count": len(items), "items": items}

    def upkeep_sync(self, token: str | None) -> dict[str, Any]:
        """Принудительно пересчитать задачи (только владелец)."""
        self._require_owner(token, "upkeep_sync")
        return self.upkeep.sync(force=True)

    def confirm_fresh(
        self,
        token: str | None,
        path: str,
        reason: str,
        *,
        task_id: int | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Подтвердить актуальность документа: нужно право записи и причина."""
        normalized = self.store._normalize_path(path)
        principal = self.brain._guard_write(token or "", "confirm_fresh", normalized)
        return self.upkeep.confirm(
            principal.name, normalized, reason,
            task_id=_as_int(task_id, "task_id"), session=session, model=model,
        )

    def propose(
        self, token: str | None, target: str, content: str, reason: str
    ) -> dict[str, Any]:
        """Подать правку решения на утверждение и уведомить владельца."""
        principal = self.principal(token)
        normalized = self.store._normalize_path(target)
        proposal_path = f"proposals/{principal.name}/{normalized}"
        base = None
        with self.store._lock:
            self.store._ensure_open()
            doc = self.store._get_doc_locked(proposal_path)
            if doc is not None and not doc["deleted"]:
                base = int(doc["current_version"])
        result = self.proposals.submit(
            token or "", normalized, content, reason, base_version=base, target=normalized
        )
        owner = self.upkeep.settings.owner
        self.coord.send(
            owner,
            f"Предложение к {normalized}",
            f"{principal.name}: {reason}\nУтвердить: brain proposal approve {result.path}",
            sender=principal.name,
        )
        return _jsonable(result)

    def authorship(self, token: str | None) -> dict[str, Any]:
        """Кто на деле писал память: люди, агенты и правки по просьбе владельца."""
        principal = self.principal(token)
        self.access.require_journal(principal, action="authorship")
        with self.store._lock:
            self.store._ensure_open()
            rows = self.store._conn.execute(
                """
                SELECT v.author AS author, v.reason AS reason,
                       (SELECT a.model FROM audit a WHERE a.action='write' AND a.path=v.path
                        AND a.actor=v.author AND a.ts>=v.ts ORDER BY a.ts LIMIT 1) AS model,
                       (SELECT a.session FROM audit a WHERE a.action='write' AND a.path=v.path
                        AND a.actor=v.author AND a.ts>=v.ts ORDER BY a.ts LIMIT 1) AS session
                FROM versions v
                """
            ).fetchall()
            roles = {
                str(item.get("name")): str(item.get("role"))
                for item in _jsonable(self.access.list_tokens())
            }
        result = {
            "total": len(rows), "agent": 0, "agent_on_request": 0,
            "owner_token_setup": 0, "owner_token_by_agent": 0, "owner_token_unknown": 0,
        }
        for row in rows:
            reason = str(row["reason"] or "")
            if roles.get(str(row["author"])) in ("owner", "human"):
                if reason.startswith("brain setup"):
                    result["owner_token_setup"] += 1
                elif row["model"] or row["session"]:
                    result["owner_token_by_agent"] += 1
                else:
                    result["owner_token_unknown"] += 1
            elif ON_BEHALF_MARK in reason:
                result["agent_on_request"] += 1
            else:
                result["agent"] += 1
        return result

    # ------------------------------------------------------------------
    # Семантика: настройка, состояние, переиндексация
    # ------------------------------------------------------------------
    def _require_owner(self, token: str | None, action: str) -> Principal:
        """Проверить, что токен принадлежит owner."""
        principal = self.brain.access.verify(token or "")
        if principal.role != "owner":
            raise PermissionDenied(
                action,
                None,
                "owner",
                actor=principal.name,
                reason=f"действие «{action}» доступно только owner",
            )
        return principal

    def _reload_semantic(self) -> None:
        """Пересоздать семантический индекс после смены настроек."""
        from .semantic import SemanticIndex

        if self.brain.semantic is not None:
            self.brain.semantic.close()
        index = SemanticIndex.from_store(self.store)
        self.brain.semantic = index
        if index is not None:
            index.set_candidate_provider(self.brain._semantic_candidates)

    def semantic_status(self, token: str | None) -> dict[str, Any]:
        """Состояние семантики: включена ли, модель, размерность, чанки, stale."""
        from .semantic import INDEX_FILE, SemanticIndex, load_settings

        self.brain.access.verify(token or "")
        settings = load_settings(self.store)
        status: dict[str, Any] = {
            "enabled": bool(settings.enabled),
            "embedder": settings.embedder,
            "model": settings.model,
            "dim": settings.dim,
            "url": settings.url,
            "chunks": 0,
            "stale": False,
            "stale_reason": None,
        }
        if self.brain.semantic is not None:
            stats = self.brain.semantic.stats()
            status.update(
                {
                    "enabled": True,
                    "chunks": stats.get("chunks", 0),
                    "stale": bool(stats.get("stale")),
                    "stale_reason": stats.get("stale_reason"),
                }
            )
        elif (Path(self.store.root) / INDEX_FILE).exists():
            index = SemanticIndex(self.store)
            try:
                status["chunks"] = index.chunk_count()
            finally:
                index.close()
        return _jsonable(status)

    def semantic_enable(
        self,
        token: str | None,
        *,
        embedder: str = "hash",
        dim: int | None = None,
        model: str | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        """Включить семантику и сохранить settings.json (только owner)."""
        from .embeddings import DEFAULT_HASH_DIM, DEFAULT_OLLAMA_URL
        from .semantic import configure_semantic, load_settings

        principal = self._require_owner(token, "semantic_enable")
        kind = str(embedder or "").strip().lower()
        if kind not in ("hash", "ollama"):
            raise ValidationError(
                f"Неизвестный эмбеддер «{embedder}».",
                hint="Допустимо: hash или ollama.",
            )
        if kind == "ollama" and not (model or "").strip():
            raise ValidationError(
                "Для Ollama нужна модель эмбеддингов.",
                hint="Передайте --model, например nomic-embed-text.",
            )
        if dim is None:
            dim = DEFAULT_HASH_DIM
        if not isinstance(dim, int) or isinstance(dim, bool) or dim < 8:
            raise ValidationError(
                "dim должен быть целым числом не меньше 8.",
                hint="Например, --dim 256.",
            )
        configure_semantic(
            self.store,
            enabled=True,
            embedder=kind,
            model=(model or "").strip() or None,
            url=(url or "").strip() or DEFAULT_OLLAMA_URL,
            dim=dim,
        )
        self._reload_semantic()
        with self.store._lock:
            self.store._ensure_open()
            with self.store._conn:
                self.store._audit_ok(
                    principal.name,
                    "semantic_enable",
                    None,
                    {"embedder": kind, "dim": dim},
                )
        return self.semantic_status(token)

    def semantic_disable(self, token: str | None) -> dict[str, Any]:
        """Выключить семантику, сохранив остальные настройки (только owner)."""
        from .semantic import configure_semantic, load_settings

        principal = self._require_owner(token, "semantic_disable")
        settings = load_settings(self.store)
        configure_semantic(
            self.store,
            enabled=False,
            embedder=settings.embedder,
            model=settings.model,
            url=settings.url,
            dim=settings.dim,
            timeout=settings.timeout,
        )
        self._reload_semantic()
        with self.store._lock:
            self.store._ensure_open()
            with self.store._conn:
                self.store._audit_ok(principal.name, "semantic_disable", None, {})
        return self.semantic_status(token)

    def semantic_reindex(self, token: str | None) -> dict[str, Any]:
        """Перестроить семантический индекс (только owner, с аудитом)."""
        principal = self._require_owner(token, "semantic_reindex")
        result = self.brain.reindex_semantic(token or "")
        with self.store._lock:
            self.store._ensure_open()
            with self.store._conn:
                self.store._audit_ok(
                    principal.name,
                    "semantic_reindex",
                    None,
                    {"chunks": result.get("chunks", 0)},
                )
        return _jsonable(result)

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

    def repack(
        self,
        token: str | None,
        *,
        snapshot_every: int | None = None,
    ) -> dict[str, Any]:
        """Переупаковать версии в дельты; только owner."""
        principal = self._require_owner(token, "repack")
        result = self.store.repack(
            snapshot_every=snapshot_every, actor=principal.name
        )
        return _jsonable(result.as_dict())

    def disk_report(
        self,
        token: str | None,
        *,
        days: int = 30,
        forecast_days: int = 30,
    ) -> dict[str, Any]:
        """Отчёт о месте на диске; только owner."""
        self._require_owner(token, "disk_report")
        return _jsonable(
            self.store.disk_report(days=days, forecast_days=forecast_days)
        )

    def backup_create(
        self,
        token: str | None,
        output: str | Path,
        *,
        keep: int | None = None,
    ) -> dict[str, Any]:
        """Создать резервную копию хранилища; только owner."""
        principal = self._require_owner(token, "backup_create")
        from .backup import create_backup

        result = create_backup(self.root, output, keep=keep)
        with self.store._lock:
            self.store._ensure_open()
            with self.store._conn:
                self.store._audit_ok(
                    principal.name, "backup_create", None, result
                )
        return _jsonable(result)

    def backup_verify(
        self, token: str | None, archive: str | Path
    ) -> dict[str, Any]:
        """Проверить резервную копию; только owner."""
        principal = self._require_owner(token, "backup_verify")
        from .backup import verify_backup

        result = verify_backup(archive)
        with self.store._lock:
            self.store._ensure_open()
            with self.store._conn:
                self.store._audit_ok(
                    principal.name, "backup_verify", None, result
                )
        return _jsonable(result)

    def maintain(self, token: str | None) -> dict[str, Any]:
        """Checkpoint WAL, gc, repack и отчёт; только owner."""
        principal = self._require_owner(token, "maintain")
        from .maintenance import maintain

        return _jsonable(maintain(self.store, actor=principal.name))

    def who_knows(self, token: str | None, path: str) -> dict[str, Any] | None:
        """Кто последним менял путь."""
        self.principal(token)
        return _jsonable(self.coord.who_knows(path))

    # ------------------------------------------------------------------
    # Песочница и реестр инструментов
    # ------------------------------------------------------------------
    def _sandbox_handler(self, token: str, args: Mapping[str, Any]) -> Any:
        return self.sandbox.run(
            args.get("script", ""),
            args.get("language", "python"),
            token,
            inputs=args.get("inputs"),
            limits=args.get("limits"),
            cache=bool(args.get("cache", False)),
        )

    def sandbox_run(
        self,
        token: str | None,
        script: str,
        language: str = "python",
        *,
        inputs: Any = None,
        limits: Mapping[str, Any] | None = None,
        cache: bool = False,
    ) -> dict[str, Any]:
        """Выполнить скрипт в песочнице через реестр инструментов."""
        return _jsonable(
            self.registry.call(
                "sandbox.run",
                {
                    "script": script,
                    "language": language,
                    "inputs": inputs,
                    "limits": limits,
                    "cache": cache,
                },
                token or "",
            )
        )

    def sandbox_status(self, token: str | None, job_id: str) -> dict[str, Any]:
        """Состояние запуска песочницы по id."""
        return _jsonable(self.sandbox.status(token or "", job_id))

    def sandbox_cancel(self, token: str | None, job_id: str) -> dict[str, Any]:
        """Отменить запуск песочницы по id."""
        return _jsonable(self.sandbox.cancel(token or "", job_id))

    def tool_list(self, token: str | None) -> list[dict[str, Any]]:
        """Список зарегистрированных инструментов без секретов."""
        self.principal(token)
        return _jsonable(self.registry.list_tools())

    # ------------------------------------------------------------------
    # Письма, присутствие и бриф
    # ------------------------------------------------------------------
    def _brief(self, name: str, limit: int) -> str:
        """Бриф с задачами на обновление; показ пишется в журнал."""
        self._sync_upkeep(max_age=0)
        try:
            lines = self.upkeep.brief_lines()
        except (sqlite3.Error, OSError, ValueError):
            lines = []
        text = self.coord.brief(name, limit=limit, upkeep_lines=lines)
        if lines or "Проверь перед опорой" in text:
            self._log_warning(name, "brief", None, {"lines": len(lines)}, None, None)
        return text

    def coord_action(self, token: str | None, action: str, **params: Any) -> Any:
        """Мультиплексор координации: письма, присутствие, who_knows, бриф.

        Автор и отправитель всегда берутся из токена. Доски задач нет.
        """
        principal = self.principal(token)
        action = str(action or "").strip().lower().replace("-", "_")
        name = principal.name
        if action == "presence":
            return _jsonable(self.coord.presence(name, params.get("activity") or ""))
        if action in ("agents", "agents_online"):
            return _jsonable(
                self.coord.agents_online(
                    ttl_seconds=_as_int(params.get("ttl_seconds"), "ttl_seconds") or 300
                )
            )
        if action in ("who_knows", "who"):
            return _jsonable(self.coord.who_knows(str(params.get("path") or "")))
        if action == "send":
            return _jsonable(
                self.coord.send(
                    str(params.get("to") or ""),
                    params.get("subject") or "",
                    params.get("body") or "",
                    sender=name,
                )
            )
        if action == "inbox":
            return _jsonable(
                self.coord.inbox(name, bool(params.get("unread", False)))
            )
        if action in ("mark_read", "read"):
            message_id = params.get("message_id")
            if message_id is None:
                raise ValidationError(
                    "Не указан message_id.",
                    hint="Возьмите id из inbox(action='inbox').",
                )
            changed = self.coord.mark_read(message_id, name)
            return {"changed": changed}
        if action == "brief":
            return {
                "agent": name,
                "brief": self._brief(name, _as_int(params.get("limit"), "limit") or 2000),
            }
        raise ValidationError(
            f"Неизвестное действие «{action}».",
            hint=(
                "Доступно: presence, agents, who-knows, send, inbox, mark-read, "
                "brief. Доски задач нет: ориентир — заметки в памяти "
                "(README.md и backlog.md)."
            ),
        )

    # ------------------------------------------------------------------
    # Предложения и аномалии
    # ------------------------------------------------------------------
    def proposal(self, token: str | None, action: str, **params: Any) -> Any:
        """Мультиплексор предложений: list, show, approve, reject, verify."""
        action = str(action or "").strip().lower().replace("-", "_")
        if action in ("list", "ls", ""):
            return _jsonable(
                self.proposals.list(
                    token or "", status=params.get("status") or None
                )
            )
        if action in ("show", "get"):
            path = params.get("path") or params.get("target")
            if not path:
                raise ValidationError(
                    "Не указан путь предложения.",
                    hint="Передайте path вида proposals/<agent>/<файл>.",
                )
            return _jsonable(self.proposals.show(token or "", str(path)))
        if action == "approve":
            path = params.get("path") or params.get("target")
            if not path:
                raise ValidationError(
                    "Не указан путь предложения.",
                    hint="Передайте path предложения.",
                )
            return _jsonable(
                self.proposals.approve(
                    token or "",
                    str(path),
                    target=params.get("to") or params.get("approved_path"),
                    reason=params.get("reason") or "утверждено ревьюером",
                )
            )
        if action == "reject":
            path = params.get("path") or params.get("target")
            if not path:
                raise ValidationError(
                    "Не указан путь предложения.",
                    hint="Передайте path предложения.",
                )
            return _jsonable(
                self.proposals.reject(
                    token or "", str(path), reason=params.get("reason") or ""
                )
            )
        if action in ("verify", "drift"):
            return {"drift": _jsonable(self.proposals.verify_approvals(token or ""))}
        raise ValidationError(
            f"Неизвестное действие предложения «{action}».",
            hint="Допустимо: list, show, approve, reject, verify.",
        )

    def anomaly_scan(
        self,
        token: str | None,
        *,
        auto_suspend: bool | None = None,
    ) -> dict[str, Any]:
        """Найти аномалии (owner) и записать инциденты в очередь ревью."""
        self._require_owner(token, "anomaly_scan")
        incidents = self.anomalies.scan(auto_suspend=auto_suspend)
        return {
            "incidents": _jsonable(incidents),
            "count": len(incidents),
            "config": self.anomalies.config.as_dict(),
        }

    def anomaly_list(
        self,
        token: str | None,
        *,
        actor: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Список инцидентов: owner или auditor."""
        principal = self.principal(token)
        if principal.role not in ("owner", "auditor"):
            raise PermissionDenied(
                "anomaly_list",
                None,
                "audit",
                actor=principal.name,
                reason="инциденты доступны owner и auditor",
            )
        return _jsonable(
            self.anomalies.incidents(actor=actor, kind=kind, limit=limit)
        )

    def inbox(
        self,
        token: str | None,
        action: str = "list",
        **params: Any,
    ) -> Any:
        """Список входящих, отправка или отметка прочтения."""
        action = (action or "list").strip().lower().replace("-", "_")
        if action in ("list", "ls", ""):
            return self.coord_action(token, "inbox", **params)
        if action == "send":
            return self.coord_action(token, "send", **params)
        if action in ("read", "mark_read"):
            return self.coord_action(token, "mark_read", **params)
        raise ValidationError(
            f"Неизвестное действие inbox «{action}».",
            hint="Допустимо: list, send, read.",
        )

    def brief(self, token: str | None, *, limit: int = 2000) -> dict[str, Any]:
        """Короткий бриф для агента из токена."""
        principal = self.principal(token)
        return {
            "agent": principal.name,
            "brief": self.coord.brief(
                principal.name, limit=_as_int(limit, "limit") or 2000
            ),
        }
