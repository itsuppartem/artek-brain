"""Координация агентов поверх общего SQLite-хранилища.

Письма (inbox), присутствие агентов, «кто последним менял путь» и короткий
бриф. Доски задач нет (решение владельца 12.11.2026): ориентир для агентов —
заметки в памяти, прежде всего projects/pilot/backlog.md.
В модуле нет сети и вызовов LLM, все операции детерминированы.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

from .errors import ArtekBrainError, ValidationError
from .store import Store

BACKLOG_PATH = "projects/pilot/backlog.md"
"""Заметка, которая заменяет доску задач."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    ts TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0,
    read_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient
    ON messages(recipient, read, id);

CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL,
    activity TEXT
);
"""

# Старые таблицы доски задач: письма и присутствие переносятся, остальное
# удаляется при первом открытии хранилища новой версией.
_LEGACY_RENAMES = (("task_messages", "messages"), ("task_agents", "agents"))
_LEGACY_DROPS = ("tasks", "task_roles", "task_handoffs", "runs")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_ts(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True)
class Message:
    """Письмо во входящих агента."""

    id: int
    sender: str
    recipient: str
    subject: str
    body: str
    ts: str
    read: bool

    def as_dict(self) -> dict[str, Any]:
        """Представить письмо словарём."""
        return {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "subject": self.subject,
            "body": self.body,
            "ts": self.ts,
            "read": self.read,
        }


@dataclass(frozen=True)
class Presence:
    """Присутствие агента."""

    name: str
    last_seen: str
    activity: str | None
    online: bool

    def as_dict(self) -> dict[str, Any]:
        """Представить присутствие словарём."""
        return {
            "name": self.name,
            "last_seen": self.last_seen,
            "activity": self.activity,
            "online": self.online,
        }


@dataclass(frozen=True, eq=False)
class WhoKnows:
    """Последний автор правки пути.

    Сравнивается и с другим WhoKnows, и со строкой имени автора.
    """

    path: str
    author: str
    version: int
    ts: str
    reason: str | None

    def _key(self) -> tuple[Any, ...]:
        return (self.path, self.author, self.version, self.ts, self.reason)

    def __str__(self) -> str:
        return self.author

    def __eq__(self, other: object) -> bool:
        if isinstance(other, WhoKnows):
            return self._key() == other._key()
        if isinstance(other, str):
            return self.author == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._key())

    def as_dict(self) -> dict[str, Any]:
        """Представить запись словарём."""
        return {
            "path": self.path,
            "author": self.author,
            "version": self.version,
            "ts": self.ts,
            "reason": self.reason,
        }


class CoordError(ArtekBrainError):
    """Ошибка координации (письма, присутствие)."""


class Coordination:
    """Письма, присутствие агентов, who_knows и бриф."""

    def __init__(
        self,
        store: Store,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(store, Store):
            raise ValidationError(
                "store должен быть экземпляром Store.",
                hint="Создайте Store(path) и передайте его в Coordination.",
            )
        self._store = store
        self._clock = clock or _utc_now
        self._store._ensure_open()
        with self._store._conn:
            self._migrate_legacy()
            self._store._conn.executescript(_SCHEMA)

    def _migrate_legacy(self) -> None:
        """Перенести письма и присутствие из таблиц доски задач, удалить остальное."""
        conn = self._store._conn
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for old, new in _LEGACY_RENAMES:
            if old in existing and new not in existing:
                conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
            elif old in existing:
                conn.execute(f"DROP TABLE {old}")
        for name in _LEGACY_DROPS:
            if name in existing:
                conn.execute(f"DROP TABLE {name}")
        conn.execute("DROP INDEX IF EXISTS idx_task_messages_recipient")

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------
    @property
    def _conn(self) -> Any:
        return self._store._conn

    @property
    def _lock(self) -> Any:
        return self._store._lock

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _audit(self, actor: str | None, action: str, detail: dict[str, Any]) -> None:
        try:
            self._store._audit_ok(actor, action, None, detail)
        except (ArtekBrainError, ValueError):
            pass

    @staticmethod
    def _require_name(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(
                f"{field} обязателен.",
                hint=f"Передайте непустую строку для {field}.",
            )
        if len(value) > 200:
            raise ValidationError(
                f"Слишком длинный {field}.",
                hint=f"Сократите {field} до 200 символов.",
            )
        return value.strip()

    @staticmethod
    def _require_text(value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(
                f"{field} должен быть строкой.",
                hint=f"Передайте текст для {field}.",
            )
        return value

    @staticmethod
    def _row_to_message(row: Any) -> Message:
        return Message(
            id=row["id"],
            sender=row["sender"],
            recipient=row["recipient"],
            subject=row["subject"],
            body=row["body"],
            ts=row["ts"],
            read=bool(row["read"]),
        )

    # ------------------------------------------------------------------
    # Присутствие
    # ------------------------------------------------------------------
    def presence(self, name: str, activity: str = "") -> Presence:
        """Отметить агента онлайн и записать, что он делает."""
        agent_name = self._require_name(name, "name")
        text = self._require_text(activity, "activity")
        now_iso = _format_ts(self._now())
        with self._lock:
            self._store._ensure_open()
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO agents(name, last_seen, activity)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        last_seen=excluded.last_seen,
                        activity=excluded.activity
                    """,
                    (agent_name, now_iso, text or None),
                )
        return Presence(
            name=agent_name, last_seen=now_iso, activity=text or None, online=True
        )

    def agent_heartbeat(self, name: str, activity: str = "") -> Presence:
        """Псевдоним presence() для heartbeat по имени."""
        return self.presence(name, activity)

    def agents_online(self, *, ttl_seconds: int = 300) -> list[Presence]:
        """Список агентов, замеченных за последние ttl_seconds."""
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds < 1:
            raise ValidationError(
                "ttl_seconds должен быть целым числом больше 0.",
                hint="Передайте ttl_seconds=300 или другое положительное число.",
            )
        with self._lock:
            self._store._ensure_open()
            rows = self._conn.execute("SELECT * FROM agents ORDER BY name ASC").fetchall()
        threshold = self._now() - timedelta(seconds=ttl_seconds)
        return [
            Presence(
                name=row["name"],
                last_seen=row["last_seen"],
                activity=row["activity"],
                online=True,
            )
            for row in rows
            if _parse_ts(row["last_seen"]) >= threshold
        ]

    # ------------------------------------------------------------------
    # Письма
    # ------------------------------------------------------------------
    def send(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        sender: str = "anonymous",
    ) -> Message:
        """Отправить письмо агенту to (по умолчанию непрочитанное)."""
        recipient = self._require_name(to, "to")
        clean_sender = self._require_name(sender, "sender")
        clean_subject = self._require_name(subject, "subject")
        clean_body = self._require_text(body, "body")
        now_iso = _format_ts(self._now())
        with self._lock:
            self._store._ensure_open()
            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO messages
                        (sender, recipient, subject, body, ts, read, read_at)
                    VALUES (?, ?, ?, ?, ?, 0, NULL)
                    """,
                    (clean_sender, recipient, clean_subject, clean_body, now_iso),
                )
                message_id = int(cursor.lastrowid or 0)
            self._audit(
                clean_sender, "message_send", {"to": recipient, "subject": clean_subject}
            )
        return Message(
            id=message_id,
            sender=clean_sender,
            recipient=recipient,
            subject=clean_subject,
            body=clean_body,
            ts=now_iso,
            read=False,
        )

    def inbox(self, name: str, unread: bool = False) -> list[Message]:
        """Входящие агента, новые сверху; unread=True только непрочитанные."""
        agent_name = self._require_name(name, "name")
        where = "recipient=? AND read=0" if unread else "recipient=?"
        with self._lock:
            self._store._ensure_open()
            rows = self._conn.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY ts DESC, id DESC",
                (agent_name,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def mark_read(self, message_id: int | Sequence[int], name: str) -> int:
        """Отметить письма прочитанными; возвращает число изменённых."""
        identifier, reader = message_id, name
        if isinstance(identifier, str) and isinstance(reader, int):
            identifier, reader = reader, identifier
        reader_name = self._require_name(reader, "name")
        if isinstance(identifier, int) and not isinstance(identifier, bool):
            ids = [identifier]
        elif isinstance(identifier, Iterable) and not isinstance(identifier, (str, bytes)):
            ids = [int(item) for item in identifier]
        else:
            raise ValidationError(
                "message_id должен быть целым числом или списком целых чисел.",
                hint="Возьмите id из inbox(name).",
            )
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            self._store._ensure_open()
            rows = self._conn.execute(
                f"SELECT id, recipient FROM messages WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
            found = {row["id"]: row["recipient"] for row in rows}
            foreign = [item for item in ids if found.get(item) not in (None, reader_name)]
            if foreign:
                raise CoordError(
                    "Нельзя отметить чужие письма: "
                    + ", ".join(str(item) for item in foreign)
                    + ".",
                    hint="Отмечайте только письма из своего inbox.",
                )
            now_iso = _format_ts(self._now())
            with self._conn:
                self._conn.execute(
                    f"""
                    UPDATE messages SET read=1, read_at=?
                    WHERE recipient=? AND read=0 AND id IN ({placeholders})
                    """,
                    (now_iso, reader_name, *ids),
                )
                changed = self._conn.execute("SELECT changes()").fetchone()[0]
        return int(changed)

    # ------------------------------------------------------------------
    # Кто знает путь и бриф
    # ------------------------------------------------------------------
    def who_knows(self, path: str) -> WhoKnows | None:
        """Кто последним менял путь (точный путь или вложенные документы)."""
        if not isinstance(path, str) or not path.strip():
            raise ValidationError(
                "path обязателен.",
                hint="Передайте путь, например projects/pilot/README.md.",
            )
        clean = self._store._normalize_path(path)
        with self._lock:
            self._store._ensure_open()
            row = self._conn.execute(
                "SELECT * FROM versions WHERE path=? ORDER BY ts DESC, id DESC LIMIT 1",
                (clean,),
            ).fetchone()
            if row is None:
                escaped = (
                    clean.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                row = self._conn.execute(
                    """
                    SELECT * FROM versions
                    WHERE path LIKE ? ESCAPE '\\'
                    ORDER BY ts DESC, id DESC
                    LIMIT 1
                    """,
                    (escaped + "/%",),
                ).fetchone()
        if row is None:
            return None
        return WhoKnows(
            path=row["path"],
            author=row["author"],
            version=int(row["n"]),
            ts=row["ts"],
            reason=row["reason"],
        )

    def brief(self, agent_name: str, *, limit: int = 2000) -> str:
        """Короткий детерминированный бриф для нового агента.

        Где ориентир, кто сейчас работает, последние правки памяти и
        непрочитанный inbox. Длина не превышает limit символов.
        """
        name = self._require_name(agent_name, "agent_name")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 200:
            raise ValidationError(
                "limit должен быть целым числом не меньше 200.",
                hint="По умолчанию бриф ограничен 2000 символами.",
            )
        online = self.agents_online(ttl_seconds=900)
        with self._lock:
            self._store._ensure_open()
            change_rows = self._conn.execute(
                """
                SELECT path, author, n, ts, reason FROM versions
                ORDER BY ts DESC, id DESC
                LIMIT 8
                """
            ).fetchall()
            unread_rows = self._conn.execute(
                """
                SELECT * FROM messages
                WHERE recipient=? AND read=0
                ORDER BY ts DESC, id DESC
                """,
                (name,),
            ).fetchall()

        def clip(value: str | None, width: int) -> str:
            text = value or ""
            return text if len(text) <= width else text[: width - 1] + "…"

        lines: list[str] = [
            f"# Бриф для {name}",
            f"Ориентир: {BACKLOG_PATH} и projects/pilot/README.md "
            "(доски задач нет).",
            f"Сейчас работают: {len(online)}",
        ]
        for item in online[:6]:
            lines.append(f"- {item.name}: {clip(item.activity, 60) or '—'}")
        lines.append("Последние правки памяти:")
        for row in change_rows:
            lines.append(
                f"- {row['path']} v{row['n']} — {row['author']}: {clip(row['reason'], 50)}"
            )
        lines.append(f"Inbox: {len(unread_rows)} непрочитанных")
        for row in unread_rows[:3]:
            lines.append(f"- «{clip(row['subject'], 50)}» от {row['sender']}")
        if len(unread_rows) > 3:
            lines.append(f"- … ещё {len(unread_rows) - 3} писем")
        text = "\n".join(lines)
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return text
