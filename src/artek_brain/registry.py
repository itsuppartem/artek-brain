"""Реестр инструментов Artek Brain (ТЗ 3.8).

Каркас реестра: зарегистрированные инструменты, JSON-схемы входа, требуемая
роль/scope, суточный лимит вызовов на токен и флаг включения. Вызов идёт
через ``Registry.call`` с проверкой прав, лимитов и записью в аудит.

Секреты инструментов лежат в отдельной таблице ``tool_secrets`` рядом с
реестром (файл ``registry.sqlite3`` вне Store) и никогда не отдаются агенту.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .access import Access, Principal
from .errors import ArtekBrainError, PermissionDenied, ValidationError

_ROLE_RANK = {"agent": 1, "human": 2, "owner": 3}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    min_role TEXT NOT NULL,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    daily_limit INTEGER NOT NULL DEFAULT 100,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_secrets (
    tool TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (tool, key),
    FOREIGN KEY (tool) REFERENCES tools(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL,
    token_id INTEGER,
    actor TEXT,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_ts ON tool_calls(tool, ts);
CREATE INDEX IF NOT EXISTS idx_tool_calls_token_ts ON tool_calls(token_id, ts);
"""


class ToolError(ArtekBrainError):
    """Ошибка реестра инструментов."""


class ToolNotFound(ToolError):
    """Инструмент не зарегистрирован."""


class ToolDisabled(ToolError):
    """Инструмент выключен."""


class ToolLimitError(ToolError):
    """Превышен суточный лимит вызовов инструмента."""


@dataclass(frozen=True)
class ToolSpec:
    """Публичное описание инструмента (без секретов)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    min_role: str = "agent"
    scopes: tuple[str, ...] = ()
    daily_limit: int = 100
    enabled: bool = True
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Представить описание словарём."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "min_role": self.min_role,
            "scopes": list(self.scopes),
            "daily_limit": self.daily_limit,
            "enabled": self.enabled,
            "created_at": self.created_at,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _role_allows(role: str, min_role: str) -> bool:
    if min_role in ("any", ""):
        return True
    if role == "auditor":
        return min_role == "auditor"
    if min_role == "auditor":
        return role == "auditor"
    return _ROLE_RANK.get(role, 0) >= _ROLE_RANK.get(min_role, 0)


def _parse_scope(item: str) -> tuple[str, str]:
    if not isinstance(item, str) or ":" not in item:
        raise ValidationError(
            f"Некорректный scope инструмента: {item!r}.",
            hint="Передайте строку вида \"docs:read\".",
        )
    prefix, level = item.rsplit(":", 1)
    prefix = prefix.strip().strip("/")
    level = level.strip().lower()
    if level not in ("read", "write", "admin"):
        raise ValidationError(
            f"Некорректный уровень scope «{level}».",
            hint="Уровень должен быть read, write или admin.",
        )
    return prefix, level


class Registry:
    """SQLite-реестр инструментов и журнал их вызовов."""

    def __init__(
        self,
        store: Any,
        access: Access,
        path: str | Path | None = None,
    ) -> None:
        self.store = store
        self.access = access
        self._lock = threading.RLock()
        self._handlers: dict[str, Callable[[str, Mapping[str, Any]], Any]] = {}
        if path is None:
            root = Path(getattr(store, "root", Path.cwd()))
            path = root / "registry.sqlite3"
        self.db_path = Path(path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level="DEFERRED",
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        with self._conn:
            self._conn.executescript(_SCHEMA)
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def __enter__(self) -> "Registry":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Закрыть соединение реестра (повторный вызов безопасен)."""
        with self._lock:
            if getattr(self, "_closed", False):
                return
            self._conn.close()
            self._closed = True

    # ------------------------------------------------------------------
    # Регистрация
    # ------------------------------------------------------------------
    def register(
        self,
        name: str,
        description: str,
        input_schema: Mapping[str, Any] | None = None,
        *,
        min_role: str = "agent",
        scopes: Iterable[str] | None = None,
        daily_limit: int | None = 100,
        enabled: bool = True,
        handler: Callable[[str, Mapping[str, Any]], Any] | None = None,
    ) -> ToolSpec:
        """Зарегистрировать или обновить инструмент."""
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(
                "Имя инструмента обязательно.",
                hint="Например: sandbox.run.",
            )
        name = name.strip()
        if min_role not in ("any", "agent", "human", "owner", "auditor"):
            raise ValidationError(
                f"Неизвестная минимальная роль «{min_role}».",
                hint="Допустимо: any, agent, human, owner, auditor.",
            )
        schema = dict(input_schema or {"type": "object"})
        if daily_limit is not None:
            if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or daily_limit < 0:
                raise ValidationError(
                    "daily_limit должен быть целым числом >= 0.",
                    hint="0 означает «без суточного лимита».",
                )
        scope_list = tuple(sorted(scopes or ()))
        for item in scope_list:
            _parse_scope(item)
        created_at = _format_ts(_utc_now())
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO tools
                        (name, description, schema_json, min_role, scopes_json,
                         daily_limit, enabled, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        description=excluded.description,
                        schema_json=excluded.schema_json,
                        min_role=excluded.min_role,
                        scopes_json=excluded.scopes_json,
                        daily_limit=excluded.daily_limit,
                        enabled=excluded.enabled
                    """,
                    (
                        name,
                        str(description),
                        json.dumps(schema, ensure_ascii=False, sort_keys=True),
                        min_role,
                        json.dumps(list(scope_list), ensure_ascii=False),
                        int(daily_limit if daily_limit is not None else 0),
                        1 if enabled else 0,
                        created_at,
                    ),
                )
            if handler is not None:
                self._handlers[name] = handler
        return self.get(name)

    def get(self, name: str) -> ToolSpec:
        """Описание инструмента без секретов."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tools WHERE name=?", (name,)
            ).fetchone()
        if row is None:
            raise ToolNotFound(
                f"Инструмент «{name}» не зарегистрирован.",
                hint="Посмотрите список зарегистрированных инструментов.",
            )
        return self._row_to_spec(row)

    def list_tools(self) -> list[ToolSpec]:
        """Все инструменты без секретов."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tools ORDER BY name"
            ).fetchall()
        return [self._row_to_spec(row) for row in rows]

    @staticmethod
    def _row_to_spec(row: sqlite3.Row) -> ToolSpec:
        try:
            schema = json.loads(row["schema_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            schema = {}
        try:
            scopes = json.loads(row["scopes_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            scopes = []
        return ToolSpec(
            name=row["name"],
            description=row["description"],
            input_schema=schema if isinstance(schema, dict) else {},
            min_role=row["min_role"],
            scopes=tuple(str(item) for item in scopes),
            daily_limit=int(row["daily_limit"]),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # Секреты инструментов
    # ------------------------------------------------------------------
    def set_secret(self, tool: str, key: str, value: str) -> None:
        """Сохранить секрет инструмента (агенту не отдаётся)."""
        self.get(tool)
        if not isinstance(key, str) or not key.strip():
            raise ValidationError(
                "Ключ секрета обязателен.",
                hint="Например: api_token.",
            )
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO tool_secrets (tool, key, value) VALUES (?, ?, ?)
                    ON CONFLICT(tool, key) DO UPDATE SET value=excluded.value
                    """,
                    (tool, key.strip(), str(value)),
                )

    def delete_secret(self, tool: str, key: str) -> bool:
        """Удалить секрет инструмента."""
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM tool_secrets WHERE tool=? AND key=?",
                    (tool, key),
                )
                return bool(cursor.rowcount)

    def _get_secret(self, tool: str, key: str) -> str | None:
        """Внутреннее чтение секрета для обработчика (не для интерфейсов)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM tool_secrets WHERE tool=? AND key=?",
                (tool, key),
            ).fetchone()
        return None if row is None else row["value"]

    # ------------------------------------------------------------------
    # Вызов
    # ------------------------------------------------------------------
    def call(self, name: str, args: Mapping[str, Any] | None, token: str) -> Any:
        """Вызвать инструмент с проверкой прав, лимита и записью в аудит."""
        spec = self.get(name)
        if not spec.enabled:
            raise ToolDisabled(
                f"Инструмент «{name}» выключен.",
                hint="Включите его в реестре инструментов.",
            )
        principal = self.access.verify(token or "")
        if not _role_allows(principal.role, spec.min_role):
            exc = PermissionDenied(
                f"tool:{name}",
                None,
                spec.min_role,
                actor=principal.name,
                reason="роль токена ниже требуемой для инструмента",
            )
            self._audit_failure(principal.name, name, exc, principal.id)
            raise exc
        for item in spec.scopes:
            prefix, level = _parse_scope(item)
            if level == "read":
                allowed = principal.can_read(prefix)
            elif level == "write":
                allowed = principal.can_write(prefix)
            else:
                allowed = principal.can_admin(prefix)
            if not allowed:
                exc = PermissionDenied(
                    f"tool:{name}",
                    prefix,
                    level,
                    actor=principal.name,
                    reason="нет требуемого scope инструмента",
                )
                self._audit_failure(principal.name, name, exc, principal.id)
                raise exc
        if spec.daily_limit > 0:
            used = self._calls_today(name, principal.id)
            if used >= spec.daily_limit:
                exc = ToolLimitError(
                    f"Суточный лимит инструмента «{name}» исчерпан: {used} из {spec.daily_limit}.",
                    hint="Повторите завтра или попросите увеличить лимит.",
                )
                self._audit_failure(principal.name, name, exc, principal.id)
                raise exc
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError(
                f"У инструмента «{name}» нет обработчика.",
                hint="Зарегистрируйте обработчик через register(handler=...).",
            )
        call_id = self._record_call(name, principal.id, principal.name, ok=False)
        payload = dict(args or {})
        try:
            result = handler(token, payload)
        except BaseException as exc:
            self._finish_call(call_id, ok=False, error=type(exc).__name__)
            if isinstance(exc, ArtekBrainError):
                self._audit_failure(principal.name, name, exc, principal.id)
            raise
        self._finish_call(call_id, ok=True)
        self._audit_ok(
            principal.name,
            name,
            {
                "tool": name,
                "token_id": principal.id,
                "role": principal.role,
                "ok": True,
            },
        )
        return result

    def _calls_today(self, name: str, token_id: int) -> int:
        start = _format_ts(
            _utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        )
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS c FROM tool_calls
                WHERE tool=? AND token_id=? AND ts>=?
                """,
                (name, token_id, start),
            ).fetchone()
        return int(row["c"] if row is not None else 0)

    def _record_call(
        self, name: str, token_id: int, actor: str, *, ok: bool
    ) -> int:
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO tool_calls (tool, token_id, actor, ts, ok)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, token_id, actor, _format_ts(_utc_now()), 1 if ok else 0),
                )
                return int(cursor.lastrowid)

    def _finish_call(self, call_id: int, *, ok: bool, error: str | None = None) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE tool_calls SET ok=?, error=? WHERE id=?",
                    (1 if ok else 0, error, call_id),
                )

    def calls(self, name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Журнал вызовов инструментов (без секретов)."""
        sql = "SELECT * FROM tool_calls"
        params: list[Any] = []
        if name is not None:
            sql += " WHERE tool=?"
            params.append(name)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "tool": row["tool"],
                "token_id": row["token_id"],
                "actor": row["actor"],
                "ts": row["ts"],
                "ok": bool(row["ok"]),
                "error": row["error"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Аудит Store
    # ------------------------------------------------------------------
    def _audit_ok(self, actor: str, name: str, detail: dict[str, Any]) -> None:
        try:
            with self.store._lock:
                self.store._ensure_open()
                with self.store._conn:
                    self.store._audit_ok(actor, "tool_call", name, detail)
        except Exception:
            pass

    def _audit_failure(
        self,
        actor: str | None,
        name: str,
        exc: ArtekBrainError,
        token_id: int | None,
    ) -> None:
        try:
            with self.store._lock:
                if self.store._closed:
                    return
                with self.store._conn:
                    self.store._audit_row(
                        actor,
                        "tool_call",
                        name,
                        {
                            "ok": False,
                            "tool": name,
                            "token_id": token_id,
                            "error": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
        except Exception:
            pass
