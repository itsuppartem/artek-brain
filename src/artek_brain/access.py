"""Токены, роли и области доступа Artek Brain (только стандартная библиотека).

В базе хранится только sha256 токена. Сам токен возвращается один раз при
создании. Роли: owner, human, agent, auditor. Права задаются списком scope
(префикс пути + уровень read/write/admin).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import PermissionDenied, ValidationError

ROLES = ("owner", "human", "agent", "auditor")
LEVELS = {"read": 1, "write": 2, "admin": 3}
_PROTECTED_PREFIXES = ("owner", "rules", "approved")
_PROTECTED_EXACT = ("README", "README.md")
_INBOX_RE = r"owner/inbox/"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_name ON tokens(name);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    if prefix in ("", "*"):
        return True
    return path == prefix or path.startswith(prefix + "/")


def is_protected(path: str) -> bool:
    """Защищён ли путь: писать может только owner (кроме owner/inbox)."""
    if path in _PROTECTED_EXACT:
        return True
    return any(_is_under(path, prefix) for prefix in _PROTECTED_PREFIXES)


def is_inbox_path(path: str) -> bool:
    """Путь вида owner/inbox/<имя-файла> (ровно один сегмент после inbox)."""
    if not path.startswith(_INBOX_RE):
        return False
    rest = path[len(_INBOX_RE) :]
    return bool(rest) and "/" not in rest and rest not in (".", "..")


@dataclass(frozen=True)
class Scope:
    """Область доступа: префикс пути и минимальный уровень."""

    prefix: str
    level: str = "read"

    def covers(self, path: str) -> bool:
        """Попадает ли путь в префикс области."""
        return _is_under(path, self.prefix)

    def as_dict(self) -> dict[str, str]:
        """Представить область словарём."""
        return {"prefix": self.prefix, "level": self.level}


@dataclass(frozen=True)
class TokenRecord:
    """Метаданные токена без самого токена и без хеша."""

    id: int
    name: str
    role: str
    scopes: tuple[Scope, ...]
    created_at: str
    expires_at: str | None
    revoked_at: str | None

    @property
    def revoked(self) -> bool:
        """Отозван ли токен."""
        return self.revoked_at is not None

    def as_dict(self) -> dict[str, Any]:
        """Представить запись словарём."""
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "scopes": [scope.as_dict() for scope in self.scopes],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
        }


class IssuedToken(str):
    """Выданный токен: строка с токеном плюс метаданные в .record."""

    record: TokenRecord

    def __new__(cls, token: str, record: TokenRecord) -> "IssuedToken":
        instance = super().__new__(cls, token)
        instance.record = record
        return instance

    @property
    def token(self) -> str:
        """Сам токен строкой."""
        return str(self)

    def as_dict(self) -> dict[str, Any]:
        """Представить выдачу словарём (токен показывается один раз)."""
        return {"token": str(self), **self.record.as_dict()}


@dataclass(frozen=True)
class Principal:
    """Прошедший проверку токен и его права."""

    id: int
    name: str
    role: str
    scopes: tuple[Scope, ...]
    expires_at: str | None = None

    def _has(self, path: str, level: str) -> bool:
        need = LEVELS[level]
        return any(
            scope.covers(path) and LEVELS[scope.level] >= need
            for scope in self.scopes
        )

    def can_read(self, path: str) -> bool:
        """Есть ли право read на путь."""
        if self.role == "owner":
            return True
        if self.role == "auditor":
            return False
        return self._has(path, "read")

    def can_write(self, path: str) -> bool:
        """Есть ли право write на путь."""
        if self.role == "owner":
            return True
        if self.role == "auditor":
            return False
        return self._has(path, "write")

    def can_admin(self, path: str) -> bool:
        """Есть ли право admin на путь."""
        if self.role == "owner":
            return True
        if self.role == "auditor":
            return False
        return self._has(path, "admin")

    def has_any_write(self) -> bool:
        """Есть ли хотя бы одно право write/admin (для исключения inbox)."""
        if self.role == "owner":
            return True
        if self.role == "auditor":
            return False
        need = LEVELS["write"]
        return any(LEVELS[scope.level] >= need for scope in self.scopes)

    def as_dict(self) -> dict[str, Any]:
        """Представить принципала словарём (без токена и хеша)."""
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "scopes": [scope.as_dict() for scope in self.scopes],
            "expires_at": self.expires_at,
        }


def _normalize_scope(item: Any) -> Scope:
    if isinstance(item, Scope):
        prefix, level = item.prefix, item.level
    elif isinstance(item, dict):
        prefix, level = item.get("prefix"), item.get("level")
    elif isinstance(item, (tuple, list)) and len(item) == 2:
        prefix, level = item[0], item[1]
    elif isinstance(item, str) and ":" in item:
        prefix, level = item.rsplit(":", 1)
    else:
        raise ValidationError(
            f"Некорректный scope: {item!r}.",
            hint="Передайте (prefix, level) или {'prefix': ..., 'level': ...}.",
        )
    if not isinstance(prefix, str):
        raise ValidationError(
            "prefix в scope должен быть строкой.",
            hint="Например: ('docs', 'read') или ('', 'read') для всего хранилища.",
        )
    prefix = prefix.strip().replace("\\", "/").strip("/")
    normalized_level = str(level).strip().lower() if level is not None else ""
    if normalized_level not in LEVELS:
        raise ValidationError(
            f"Некорректный уровень scope «{level}».",
            hint="Уровень должен быть read, write или admin.",
        )
    return Scope(prefix=prefix, level=normalized_level)


def _as_scope_list(scopes: Iterable[Any] | Any | None) -> list[Any]:
    """Привести scopes к списку элементов, не путая одиночную пару со списком."""
    if scopes is None:
        return []
    if isinstance(scopes, (Scope, dict, str)):
        return [scopes]
    if isinstance(scopes, (tuple, list)):
        if (
            len(scopes) == 2
            and isinstance(scopes[0], str)
            and isinstance(scopes[1], str)
            and scopes[1].strip().lower() in LEVELS
        ):
            return [scopes]
        return list(scopes)
    return [scopes]


class Access:
    """Хранилище токенов и проверка прав по областям."""

    def __init__(self, path: str | Path | Any) -> None:
        self._lock = threading.RLock()
        self._closed = False
        if isinstance(path, (str, Path)):
            target = Path(path)
        elif hasattr(path, "root"):
            target = Path(path.root) / "access.sqlite3"
        else:
            target = Path(path)
        if target.suffix in (".sqlite3", ".sqlite", ".db"):
            db_path = target
        else:
            target.mkdir(parents=True, exist_ok=True)
            db_path = target / "access.sqlite3"
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

    @classmethod
    def for_store(cls, store: Any) -> "Access":
        """Создать хранилище токенов рядом с Store."""
        return cls(Path(store.root) / "access.sqlite3")

    def __enter__(self) -> "Access":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Закрыть соединение (повторный вызов безопасен)."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValidationError(
                "Хранилище токенов закрыто.",
                hint="Создайте новый Access(path) до конца работы.",
            )

    # ------------------------------------------------------------------
    # Выдача, проверка, отзыв
    # ------------------------------------------------------------------
    def create_token(
        self,
        name: str,
        role: str = "agent",
        scopes: Iterable[Any] | Any | None = None,
        ttl: int | float | timedelta | None = None,
    ) -> IssuedToken:
        """Создать токен и вернуть его один раз вместе с метаданными."""
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(
                "Имя токена обязательно.",
                hint="Передайте непустое имя, например agent-1.",
            )
        name = name.strip()
        if len(name) > 200:
            raise ValidationError(
                "Слишком длинное имя токена.",
                hint="Сократите имя до 200 символов.",
            )
        if role not in ROLES:
            raise ValidationError(
                f"Неизвестная роль «{role}».",
                hint="Роль должна быть owner, human, agent или auditor.",
            )
        normalized = tuple(_normalize_scope(item) for item in _as_scope_list(scopes))
        now = _utc_now()
        expires_at = None
        if ttl is not None:
            if isinstance(ttl, timedelta):
                delta = ttl
            elif isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
                raise ValidationError(
                    "ttl должен быть числом секунд, timedelta или None.",
                    hint="Например: ttl=timedelta(days=1) или ttl=3600.",
                )
            else:
                delta = timedelta(seconds=float(ttl))
            expires_at = _format_ts(now + delta)
        token = "ab_" + secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        with self._lock:
            self._ensure_open()
            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO tokens
                        (name, role, scopes_json, token_hash, created_at, expires_at, revoked_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        name,
                        role,
                        json.dumps([scope.as_dict() for scope in normalized]),
                        token_hash,
                        _format_ts(now),
                        expires_at,
                    ),
                )
                token_id = int(cursor.lastrowid)
        record = TokenRecord(
            id=token_id,
            name=name,
            role=role,
            scopes=normalized,
            created_at=_format_ts(now),
            expires_at=expires_at,
            revoked_at=None,
        )
        return IssuedToken(token, record)

    def _row_to_record(self, row: sqlite3.Row) -> TokenRecord:
        try:
            raw_scopes = json.loads(row["scopes_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            raw_scopes = []
        scopes = []
        for item in raw_scopes if isinstance(raw_scopes, list) else []:
            try:
                scopes.append(_normalize_scope(item))
            except ValidationError:
                continue
        return TokenRecord(
            id=row["id"],
            name=row["name"],
            role=row["role"],
            scopes=tuple(scopes),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )

    def verify(self, token: str) -> Principal:
        """Проверить токен и вернуть принципала или отказ PermissionDenied."""
        if not isinstance(token, str) or not token.strip():
            raise PermissionDenied(
                "authenticate",
                None,
                "valid token",
                actor=None,
                reason="токен не передан",
            )
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT * FROM tokens WHERE token_hash=?", (_hash_token(token.strip()),)
            ).fetchone()
        if row is None:
            raise PermissionDenied(
                "authenticate",
                None,
                "valid token",
                actor=None,
                reason="неизвестный токен",
            )
        record = self._row_to_record(row)
        if record.revoked_at is not None:
            raise PermissionDenied(
                "authenticate",
                None,
                "valid token",
                actor=record.name,
                reason="токен отозван",
            )
        if record.expires_at is not None:
            try:
                expires = datetime.fromisoformat(record.expires_at)
            except ValueError:
                expires = None
            if expires is not None:
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= _utc_now():
                    raise PermissionDenied(
                        "authenticate",
                        None,
                        "valid token",
                        actor=record.name,
                        reason="срок действия токена истёк",
                    )
        return Principal(
            id=record.id,
            name=record.name,
            role=record.role,
            scopes=record.scopes,
            expires_at=record.expires_at,
        )

    def revoke(self, token: str | int | TokenRecord) -> bool:
        """Отозвать токен по значению, id или записи. True, если найден."""
        if isinstance(token, TokenRecord):
            return self.revoke_by_id(token.id)
        if isinstance(token, int) and not isinstance(token, bool):
            return self.revoke_by_id(token)
        if not isinstance(token, str) or not token.strip():
            return False
        with self._lock:
            self._ensure_open()
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE tokens SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                    (_format_ts(_utc_now()), _hash_token(token.strip())),
                )
                if cursor.rowcount:
                    return True
                found = self._conn.execute(
                    "SELECT 1 FROM tokens WHERE token_hash=?",
                    (_hash_token(token.strip()),),
                ).fetchone()
        return found is not None

    def revoke_by_id(self, token_id: int) -> bool:
        """Отозвать токен по внутреннему id."""
        if not isinstance(token_id, int) or isinstance(token_id, bool):
            raise ValidationError(
                "token_id должен быть целым числом.",
                hint="Возьмите id из list_tokens().",
            )
        with self._lock:
            self._ensure_open()
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (_format_ts(_utc_now()), token_id),
                )
                return bool(cursor.rowcount)

    def revoke_name(self, name: str) -> int:
        """Отозвать все активные токены с указанным именем. Число отозванных."""
        if not isinstance(name, str) or not name.strip():
            return 0
        with self._lock:
            self._ensure_open()
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE tokens SET revoked_at=? WHERE name=? AND revoked_at IS NULL",
                    (_format_ts(_utc_now()), name.strip()),
                )
                return int(cursor.rowcount)

    def list_tokens(self) -> list[TokenRecord]:
        """Все токены без секретов, новые сверху."""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT * FROM tokens ORDER BY id DESC"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    # ------------------------------------------------------------------
    # Политика путей
    # ------------------------------------------------------------------
    def require_read(
        self, principal: Principal, path: str, *, action: str = "read"
    ) -> None:
        """Проверить право чтения пути."""
        if principal.role == "owner":
            return
        if principal.role == "auditor":
            raise PermissionDenied(
                action,
                path,
                "audit",
                actor=principal.name,
                reason="роль auditor читает только журнал аудита и статистику",
            )
        if not principal.can_read(path):
            raise PermissionDenied(
                action, path, "read", actor=principal.name
            )

    def require_write(
        self,
        principal: Principal,
        path: str,
        *,
        action: str = "write",
        creating: bool = False,
    ) -> None:
        """Проверить право записи с учётом защищённых путей и inbox."""
        if principal.role == "owner":
            return
        if principal.role == "auditor":
            raise PermissionDenied(
                action,
                path,
                "write",
                actor=principal.name,
                reason="роль auditor не может изменять документы",
            )
        if is_inbox_path(path):
            if not creating:
                raise PermissionDenied(
                    action,
                    path,
                    "owner",
                    actor=principal.name,
                    reason=(
                        "в owner/inbox разрешено только создавать новый документ, "
                        "редактировать чужой нельзя"
                    ),
                )
            if not (principal.can_write(path) or principal.has_any_write()):
                raise PermissionDenied(
                    action, path, "write", actor=principal.name
                )
            return
        if is_protected(path):
            raise PermissionDenied(
                action,
                path,
                "owner",
                actor=principal.name,
                reason="путь защищён: писать может только owner",
            )
        if not principal.can_write(path):
            raise PermissionDenied(
                action, path, "write", actor=principal.name
            )

    def require_journal(
        self, principal: Principal, *, action: str = "audit"
    ) -> None:
        """Проверить доступ к журналу аудита и статистике."""
        if principal.role in ("owner", "auditor"):
            return
        raise PermissionDenied(
            action,
            None,
            "audit",
            actor=principal.name,
            reason="журнал аудита и статистика доступны только owner и auditor",
        )


# Удобные псевдонимы.
Access.tokens = Access.list_tokens
