"""Доска задач и координация роя поверх общего SQLite-хранилища.

Модуль работает рядом с ядром Store и использует то же соединение с базой:
схема задач создаётся в brain.sqlite3, документы handoff кладутся в Store.
В модуле нет сети и вызовов LLM, все операции детерминированы.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from typing import Any, Callable, Iterable, Sequence

from .errors import ArtekBrainError, DeletedError, NotFoundError, ValidationError
from .store import Store

TODO = "todo"
CLAIMED = "claimed"
IN_PROGRESS = "in_progress"
REVIEW = "review"
REWORK = "rework"
DONE = "done"
BLOCKED = "blocked"
CANCELLED = "cancelled"

STATUSES = (
    TODO,
    CLAIMED,
    IN_PROGRESS,
    REVIEW,
    REWORK,
    DONE,
    BLOCKED,
    CANCELLED,
)
"""Допустимые статусы задачи."""

OPEN_STATUSES = (TODO, CLAIMED, IN_PROGRESS, REVIEW, REWORK, BLOCKED)
"""Статусы, которые считаются открытой работой."""

ACTIVE_STATUSES = (CLAIMED, IN_PROGRESS)
"""Статусы, удерживающие аренду и область."""

REVIEWER_ROLES = frozenset({"reviewer", "owner", "admin"})
"""Роли, которым разрешена приёмка и возврат на доработку."""

_ALLOWED_ROLES = frozenset({"reviewer", "owner", "admin", "agent", "human", "auditor"})
_TASK_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d{3,}$")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*$")
_HANDOFF_TASK_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    depends_on TEXT NOT NULL DEFAULT '[]',
    area TEXT NOT NULL DEFAULT '[]',
    author TEXT NOT NULL,
    assignee TEXT,
    revision INTEGER NOT NULL,
    claim_token TEXT,
    claim_expires_at TEXT,
    claimed_at TEXT,
    evidence TEXT,
    evidence_mismatch INTEGER NOT NULL DEFAULT 0,
    evidence_report TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
CREATE INDEX IF NOT EXISTS idx_tasks_claim_token ON tasks(claim_token);

CREATE TABLE IF NOT EXISTS task_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    ts TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0,
    read_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_messages_recipient
    ON task_messages(recipient, read, id);

CREATE TABLE IF NOT EXISTS task_agents (
    name TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL,
    activity TEXT
);

CREATE TABLE IF NOT EXISTS task_roles (
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (actor, role)
);

CREATE TABLE IF NOT EXISTS task_handoffs (
    task_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    author TEXT NOT NULL,
    version INTEGER,
    ts TEXT NOT NULL
);
"""


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
class Task:
    """Задача на доске."""

    id: str
    title: str
    body: str
    status: str
    priority: int
    depends_on: list[str]
    area: list[str]
    author: str
    assignee: str | None
    revision: int
    claim_token: str | None
    claim_expires_at: str | None
    evidence: str | None
    note: str | None
    created_at: str
    updated_at: str
    claimed_at: str | None = None
    evidence_mismatch: bool = False
    evidence_report: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Представить задачу словарём."""
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "status": self.status,
            "priority": self.priority,
            "depends_on": list(self.depends_on),
            "area": list(self.area),
            "author": self.author,
            "assignee": self.assignee,
            "revision": self.revision,
            "claim_token": self.claim_token,
            "claim_expires_at": self.claim_expires_at,
            "claimed_at": self.claimed_at,
            "evidence": self.evidence,
            "evidence_mismatch": self.evidence_mismatch,
            "evidence_report": self.evidence_report,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class Claim:
    """Результат аренды задачи: задача, токен и срок."""

    task: Task
    token: str
    expires_at: str

    @property
    def claim_token(self) -> str:
        """Псевдоним токена для удобства."""
        return self.token

    @property
    def id(self) -> str:
        """Идентификатор задачи."""
        return self.task.id

    def __getattr__(self, name: str) -> Any:
        return getattr(self.task, name)


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
    """Присутствие агента в рое."""

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

    def __str__(self) -> str:
        return self.author

    def __eq__(self, other: object) -> bool:
        if isinstance(other, WhoKnows):
            return (
                self.path,
                self.author,
                self.version,
                self.ts,
                self.reason,
            ) == (
                other.path,
                other.author,
                other.version,
                other.ts,
                other.reason,
            )
        if isinstance(other, str):
            return self.author == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.path, self.author, self.version, self.ts, self.reason))

    def as_dict(self) -> dict[str, Any]:
        """Представить запись словарём."""
        return {
            "path": self.path,
            "author": self.author,
            "version": self.version,
            "ts": self.ts,
            "reason": self.reason,
        }


class TaskError(ArtekBrainError):
    """Базовая ошибка доски задач."""


class TaskNotFoundError(NotFoundError, TaskError):
    """Задача не найдена."""

    def __init__(self, task_id: str) -> None:
        super().__init__(
            f"Задача «{task_id}» не найдена.",
            hint="Проверьте id или получите список задач через list().",
        )


class TaskRevisionConflictError(TaskError):
    """Передан устаревший revision задачи."""

    def __init__(self, task: Task, expected: int) -> None:
        self.task = task
        self.current = task
        self.expected = expected
        super().__init__(
            (
                f"Конфликт revision задачи «{task.id}»: передана {expected}, "
                f"текущая {task.revision} (status={task.status}, "
                f"assignee={task.assignee or '—'})."
            ),
            hint="Прочитайте задачу через show() и передайте актуальный revision.",
        )


class AreaConflictError(TaskError):
    """Область задачи пересекается с уже занятой областью."""

    def __init__(
        self,
        task: Task,
        busy: Task,
        overlap: tuple[str, str],
    ) -> None:
        self.task = task
        self.busy_task = busy
        self.overlap = overlap
        super().__init__(
            (
                f"Область задачи «{task.id}» ({overlap[0]}) пересекается с "
                f"занятой задачей «{busy.id}» агента {busy.assignee or '—'} "
                f"({overlap[1]}, status={busy.status})."
            ),
            hint="Дождитесь освобождения области или возьмите задачу вне пересечения.",
        )


class DependencyError(TaskError):
    """Зависимости задачи ещё не выполнены."""

    def __init__(self, task: Task, pending: Sequence[str]) -> None:
        self.task = task
        self.pending = list(pending)
        super().__init__(
            f"Задачу «{task.id}» нельзя взять: не завершены зависимости {', '.join(pending)}.",
            hint="Дождитесь статуса done у зависимостей или измените depends_on.",
        )


class DependencyCycleError(DependencyError):
    """Создание задачи замкнуло цикл зависимостей."""

    def __init__(self, task_id: str, cycle: Sequence[str]) -> None:
        self.task_id = task_id
        self.cycle = list(cycle)
        ArtekBrainError.__init__(
            self,
            f"Цикл зависимостей: {' -> '.join(cycle)}.",
            hint="Уберите одну из зависимостей, чтобы граф остался ациклическим.",
        )


class ClaimLimitError(TaskError):
    """У агента превышено число параллельных аренд."""

    def __init__(self, agent: str, limit: int, active: int) -> None:
        self.agent = agent
        self.limit = limit
        self.active = active
        super().__init__(
            f"У агента {agent} уже {active} активных аренд из {limit}.",
            hint="Освободите задачу через release() или завершите работу.",
        )


class LeaseError(TaskError):
    """Аренда задачи недействительна или истекла."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint or "Возьмите задачу заново через claim().")


class ReviewPermissionError(TaskError):
    """Нет права принять или вернуть задачу."""

    def __init__(self, task: Task, actor: str) -> None:
        self.task = task
        self.actor = actor
        super().__init__(
            (
                f"Агент {actor} не может принять/вернуть задачу «{task.id}»: "
                f"нужна роль reviewer/owner и нельзя принимать работу, которую делал сам."
            ),
            hint="Попросите reviewer или owner выполнить действие.",
        )


class ActionError(TaskError):
    """Действие недопустимо в текущем статусе задачи."""

    def __init__(self, task: Task, action: str, allowed: Sequence[str]) -> None:
        self.task = task
        self.action = action
        self.allowed = list(allowed)
        super().__init__(
            (
                f"Действие {action} невозможно для задачи «{task.id}» "
                f"в статусе {task.status}."
            ),
            hint="Допустимые статусы: " + ", ".join(allowed) + ".",
        )


def _path_overlap(left: str, right: str) -> bool:
    left = left.strip("/")
    right = right.strip("/")
    if left == right:
        return True
    return left.startswith(right + "/") or right.startswith(left + "/")


def _areas_overlap(left: Sequence[str], right: Sequence[str]) -> tuple[str, str] | None:
    for first in left:
        for second in right:
            if _path_overlap(first, second):
                return first, second
    return None


class TaskBoard:
    """Доска задач: claim с арендой, области, зависимости, inbox и handoff."""

    def __init__(
        self,
        store: Store,
        *,
        max_claims: int = 2,
        clock: Callable[[], datetime] | None = None,
        reviewers: Iterable[str] = (),
        owners: Iterable[str] = (),
        evidence_checker: Any = None,
        review_queue: Any = None,
    ) -> None:
        if not isinstance(max_claims, int) or isinstance(max_claims, bool) or max_claims < 1:
            raise ValidationError(
                "max_claims должен быть целым числом больше 0.",
                hint="Передайте max_claims=2 или другое положительное число.",
            )
        if not isinstance(store, Store):
            raise ValidationError(
                "store должен быть экземпляром Store.",
                hint="Создайте Store(path) и передайте его в TaskBoard.",
            )
        self._store = store
        self._max_claims = int(max_claims)
        self._clock = clock or _utc_now
        self._evidence_checker = evidence_checker
        self._review_queue = review_queue
        self._store._ensure_open()
        with self._store._conn:
            self._store._conn.executescript(_SCHEMA)
            self._ensure_columns()
        for name in reviewers:
            self.grant_role(name, "reviewer")
        for name in owners:
            self.grant_role(name, "owner")

    def _ensure_columns(self) -> None:
        """Добавить новые колонки в старую базу задач без потери данных."""
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "claimed_at" not in columns:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN claimed_at TEXT")
        if "evidence_mismatch" not in columns:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN evidence_mismatch INTEGER NOT NULL DEFAULT 0"
            )
        if "evidence_report" not in columns:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN evidence_report TEXT")

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------
    @property
    def _conn(self) -> Any:
        return self._store._conn

    @property
    def _lock(self) -> Any:
        return self._store._lock

    @property
    def max_claims(self) -> int:
        """Максимум параллельных аренд на одного агента."""
        return self._max_claims

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _audit(
        self,
        actor: str | None,
        action: str,
        task_id: str | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._store._audit_ok(actor, action, f"task/{task_id}" if task_id else None, detail or {})
        except (ArtekBrainError, ValueError):
            pass

    def _row_to_task(self, row: Any) -> Task:
        report = None
        raw_report = row["evidence_report"] if "evidence_report" in row.keys() else None
        if raw_report:
            try:
                parsed = json.loads(raw_report)
                report = parsed if isinstance(parsed, dict) else None
            except (TypeError, json.JSONDecodeError):
                report = None
        return Task(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            status=row["status"],
            priority=int(row["priority"]),
            depends_on=list(json.loads(row["depends_on"] or "[]")),
            area=list(json.loads(row["area"] or "[]")),
            author=row["author"],
            assignee=row["assignee"],
            revision=int(row["revision"]),
            claim_token=row["claim_token"],
            claim_expires_at=row["claim_expires_at"],
            evidence=row["evidence"],
            note=row["note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            claimed_at=(
                row["claimed_at"] if "claimed_at" in row.keys() else None
            ),
            evidence_mismatch=bool(
                row["evidence_mismatch"]
                if "evidence_mismatch" in row.keys()
                else 0
            ),
            evidence_report=report,
        )

    def _find_locked(self, task_id: str) -> Task:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        return self._row_to_task(row)

    def _release_expired_locked(self) -> list[str]:
        now_iso = _format_ts(self._now())
        rows = self._conn.execute(
            """
            SELECT * FROM tasks
            WHERE status IN (?, ?) AND claim_expires_at IS NOT NULL
              AND claim_expires_at<=?
            """,
            (CLAIMED, IN_PROGRESS, now_iso),
        ).fetchall()
        if not rows:
            return []
        released: list[str] = []
        with self._conn:
            for row in rows:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, assignee=NULL, claim_token=NULL,
                        claim_expires_at=NULL, revision=revision+1, updated_at=?
                    WHERE id=?
                    """,
                    (TODO, now_iso, row["id"]),
                )
                released.append(row["id"])
                self._audit(
                    row["assignee"],
                    "task_expired",
                    row["id"],
                    {"previous_assignee": row["assignee"], "status": row["status"]},
                )
        return released

    def _require_name(self, value: Any, field: str) -> str:
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

    def _require_text(self, value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(
                f"{field} должен быть строкой.",
                hint=f"Передайте текст для {field}.",
            )
        return value

    def _normalize_area(self, area: Any) -> list[str]:
        if isinstance(area, str) or not isinstance(area, Iterable):
            raise ValidationError(
                "area должен быть списком префиксов путей.",
                hint='Пример: area=["src/artek_brain", "tests"].',
            )
        result: list[str] = []
        for item in area:
            if not isinstance(item, str) or not item.strip():
                raise ValidationError(
                    "Элемент area должен быть непустой строкой.",
                    hint="Передайте путь или префикс пути, например src/artek_brain.",
                )
            value = item.strip().replace("\\", "/")
            while value.startswith("./"):
                value = value[2:]
            value = value.strip("/")
            if not value or ".." in value.split("/"):
                raise ValidationError(
                    f"Недопустимый префикс области {item!r}.",
                    hint="Область — относительный путь без .. и пустых сегментов.",
                )
            if value not in result:
                result.append(value)
        if not result:
            raise ValidationError(
                "area не может быть пустым.",
                hint="Укажите хотя бы один префикс пути, который занимает задача.",
            )
        return result

    def _normalize_deps(self, depends_on: Any) -> list[str]:
        if depends_on is None:
            return []
        if isinstance(depends_on, str) or not isinstance(depends_on, Iterable):
            raise ValidationError(
                "depends_on должен быть списком id задач.",
                hint='Пример: depends_on=["CORE-001"].',
            )
        result: list[str] = []
        for item in depends_on:
            if not isinstance(item, str) or not _TASK_ID_RE.match(item.strip()):
                raise ValidationError(
                    f"Некорректный id зависимости {item!r}.",
                    hint="id задачи выглядит как AREA-NNN, например CORE-001.",
                )
            value = item.strip()
            if value not in result:
                result.append(value)
        return result

    def _normalize_key(self, key: Any) -> str:
        if not isinstance(key, str) or not key.strip():
            raise ValidationError(
                "key должен быть непустой строкой.",
                hint="Ключ задачи — латинские буквы и цифры, например CORE.",
            )
        value = re.sub(r"[^A-Za-z0-9]", "", key).upper()
        if not value or value[0].isdigit() or not _KEY_RE.match(value):
            raise ValidationError(
                f"Некорректный key {key!r}.",
                hint="Ключ начинается с буквы и содержит A-Z, 0-9, например CORE.",
            )
        return value

    def _derive_key(self, area: Sequence[str]) -> str:
        first = area[0].strip("/")
        segment = first.split("/")[0]
        if "." in segment and "/" not in first:
            segment = segment.split(".")[0]
        return self._normalize_key(segment or "TASK")

    def _next_id_locked(self, key: str) -> str:
        rows = self._conn.execute(
            "SELECT id FROM tasks WHERE id LIKE ?", (f"{key}-%",)
        ).fetchall()
        maximum = 0
        for row in rows:
            match = re.fullmatch(re.escape(key) + r"-(\d+)", row["id"])
            if match:
                maximum = max(maximum, int(match.group(1)))
        return f"{key}-{maximum + 1:03d}"

    def _check_cycle_locked(self, task_id: str, depends_on: Sequence[str]) -> None:
        graph: dict[str, list[str]] = {}
        for row in self._conn.execute("SELECT id, depends_on FROM tasks").fetchall():
            graph[row["id"]] = list(json.loads(row["depends_on"] or "[]"))
        graph[task_id] = list(depends_on)
        state: dict[str, int] = {}
        trail: list[str] = []

        def visit(node: str) -> None:
            current = state.get(node, 0)
            if current == 1:
                start = trail.index(node) if node in trail else 0
                raise DependencyCycleError(task_id, trail[start:] + [node])
            if current == 2:
                return
            state[node] = 1
            trail.append(node)
            for dependency in graph.get(node, []):
                visit(dependency)
            trail.pop()
            state[node] = 2

        visit(task_id)

    def _check_revision(self, task: Task, revision: Any) -> None:
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise ValidationError(
                "revision должен быть целым числом.",
                hint="Возьмите revision из show()/list() и передайте его.",
            )
        if revision != task.revision:
            raise TaskRevisionConflictError(task, revision)

    def _require_action(self, task: Task, action: str, allowed: Sequence[str]) -> None:
        if task.status not in allowed:
            raise ActionError(task, action, allowed)

    def _roles_locked(self, actor: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT role FROM task_roles WHERE actor=?", (actor,)
        ).fetchall()
        return {row["role"] for row in rows}

    def _require_reviewer(self, task: Task, actor: str) -> None:
        # Проверять нельзя собственную работу: исполнитель не принимает себя.
        # Автор (постановщик) принимать может: он ставит задачу, исполняет другой.
        if actor == task.assignee:
            raise ReviewPermissionError(task, actor)
        if not (self._roles_locked(actor) & REVIEWER_ROLES):
            raise ReviewPermissionError(task, actor)

    # ------------------------------------------------------------------
    # Роли
    # ------------------------------------------------------------------
    def grant_role(self, actor: str, role: str) -> None:
        """Выдать роль участнику (reviewer, owner, admin, agent, human, auditor)."""
        name = self._require_name(actor, "actor")
        if role not in _ALLOWED_ROLES:
            raise ValidationError(
                f"Неизвестная роль {role!r}.",
                hint="Допустимые роли: " + ", ".join(sorted(_ALLOWED_ROLES)) + ".",
            )
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            with self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO task_roles(actor, role) VALUES (?, ?)",
                    (name, role),
                )
            self._audit(name, "task_role_grant", None, {"role": role})

    def revoke_role(self, actor: str, role: str) -> None:
        """Забрать роль у участника."""
        name = self._require_name(actor, "actor")
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            with self._conn:
                self._conn.execute(
                    "DELETE FROM task_roles WHERE actor=? AND role=?", (name, role)
                )
            self._audit(name, "task_role_revoke", None, {"role": role})

    def roles(self, actor: str) -> set[str]:
        """Роли участника."""
        name = self._require_name(actor, "actor")
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            return self._roles_locked(name)

    # ------------------------------------------------------------------
    # Задачи
    # ------------------------------------------------------------------
    def create(
        self,
        title: str,
        body: str,
        *,
        area: Iterable[str],
        author: str,
        priority: int = 2,
        depends_on: Iterable[str] | None = None,
        key: str | None = None,
    ) -> Task:
        """Создать задачу в статусе todo.

        id формируется как AREA-NNN; AREA берётся из key или первого
        префикса area. depends_on может ссылаться на будущие задачи,
        но цикл зависимостей запрещён.
        """
        clean_title = self._require_name(title, "title")
        clean_body = self._require_text(body, "body")
        clean_author = self._require_name(author, "author")
        clean_area = self._normalize_area(area)
        deps = self._normalize_deps(depends_on)
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
            raise ValidationError(
                "priority должен быть целым числом >= 0.",
                hint="Меньше число — выше приоритет, по умолчанию 2.",
            )
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            task_key = self._normalize_key(key) if key is not None else self._derive_key(clean_area)
            task_id = self._next_id_locked(task_key)
            if task_id in deps:
                raise DependencyCycleError(task_id, [task_id, task_id])
            self._check_cycle_locked(task_id, deps)
            now_iso = _format_ts(self._now())
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO tasks
                        (id, title, body, status, priority, depends_on, area,
                         author, assignee, revision, claim_token,
                         claim_expires_at, evidence, note, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, NULL,
                            NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        task_id,
                        clean_title,
                        clean_body,
                        TODO,
                        int(priority),
                        json.dumps(deps, ensure_ascii=False),
                        json.dumps(clean_area, ensure_ascii=False),
                        clean_author,
                        now_iso,
                        now_iso,
                    ),
                )
                self._audit(
                    clean_author,
                    "task_create",
                    task_id,
                    {"title": clean_title, "area": clean_area, "depends_on": deps},
                )
            return self._find_locked(task_id)

    def list(
        self,
        *,
        status: str | Iterable[str] | None = None,
        assignee: str | None = None,
        area: str | None = None,
        priority: int | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Task]:
        """Список задач с фильтрами; порядок priority, затем id."""
        wanted: set[str] | None = None
        if status is not None:
            if isinstance(status, str):
                wanted = {status}
            elif isinstance(status, Iterable):
                wanted = set(status)
            else:
                raise ValidationError(
                    "status должен быть строкой или списком статусов.",
                    hint="Например: status=['todo', 'claimed'].",
                )
            unknown = wanted - set(STATUSES)
            if unknown:
                raise ValidationError(
                    "Неизвестные статусы: " + ", ".join(sorted(unknown)) + ".",
                    hint="Допустимые статусы: " + ", ".join(STATUSES) + ".",
                )
        if assignee is not None:
            assignee = self._require_name(assignee, "assignee")
        if priority is not None and (
            not isinstance(priority, int) or isinstance(priority, bool)
        ):
            raise ValidationError(
                "priority должен быть целым числом.",
                hint="Передайте priority или None.",
            )
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise ValidationError(
                "limit должен быть положительным целым числом.",
                hint="Передайте limit=None или число больше 0.",
            )
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValidationError(
                "offset должен быть целым числом >= 0.",
                hint="Передайте offset=0 для первой страницы.",
            )
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY priority ASC, id ASC"
            ).fetchall()
            tasks = [self._row_to_task(row) for row in rows]
        result: list[Task] = []
        for task in tasks:
            if wanted is not None and task.status not in wanted:
                continue
            if assignee is not None and task.assignee != assignee:
                continue
            if priority is not None and task.priority != priority:
                continue
            if area is not None and _areas_overlap(task.area, [area]) is None:
                continue
            result.append(task)
        if offset:
            result = result[offset:]
        if limit is not None:
            result = result[:limit]
        return result

    def show(self, task_id: str) -> Task:
        """Показать задачу по id."""
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            return self._find_locked(task_id)

    def claim(
        self,
        task_id: str,
        agent: str,
        *,
        revision: int,
        minutes: int = 30,
    ) -> Claim:
        """Взять задачу в аренду на minutes минут, выдав claim_token."""
        agent_name = self._require_name(agent, "agent")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
            raise ValidationError(
                "minutes должен быть целым числом больше 0.",
                hint="Передайте minutes=30 или другое положительное число.",
            )
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            task = self._find_locked(task_id)
            self._check_revision(task, revision)
            self._require_action(task, "claim", (TODO, REWORK))

            active = self._conn.execute(
                """
                SELECT COUNT(*) FROM tasks
                WHERE assignee=? AND status IN (?, ?)
                """,
                (agent_name, CLAIMED, IN_PROGRESS),
            ).fetchone()[0]
            if int(active) >= self._max_claims:
                raise ClaimLimitError(agent_name, self._max_claims, int(active))

            pending: list[str] = []
            if task.depends_on:
                placeholders = ",".join("?" for _ in task.depends_on)
                rows = self._conn.execute(
                    f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",
                    tuple(task.depends_on),
                ).fetchall()
                statuses = {row["id"]: row["status"] for row in rows}
                for dependency in task.depends_on:
                    if statuses.get(dependency) != DONE:
                        pending.append(
                            f"{dependency} ({statuses.get(dependency, 'missing')})"
                        )
            if pending:
                raise DependencyError(task, pending)

            busy_rows = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?) AND id<>?
                """,
                (CLAIMED, IN_PROGRESS, task.id),
            ).fetchall()
            for row in busy_rows:
                busy = self._row_to_task(row)
                overlap = _areas_overlap(task.area, busy.area)
                if overlap is not None:
                    raise AreaConflictError(task, busy, overlap)

            token = token_hex(16)
            expires = _format_ts(self._now() + timedelta(minutes=minutes))
            now_iso = _format_ts(self._now())
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, assignee=?, claim_token=?, claim_expires_at=?,
                        claimed_at=?, revision=revision+1, updated_at=?
                    WHERE id=?
                    """,
                    (CLAIMED, agent_name, token, expires, now_iso, now_iso, task.id),
                )
                self._audit(
                    agent_name,
                    "task_claim",
                    task.id,
                    {"minutes": minutes, "revision": revision},
                )
            updated = self._find_locked(task.id)
            return Claim(task=updated, token=token, expires_at=expires)

    def start(
        self,
        task_id: str,
        agent: str,
        *,
        revision: int,
        token: str | None = None,
    ) -> Task:
        """Перевести взятую задачу в in_progress."""
        agent_name = self._require_name(agent, "agent")
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            task = self._find_locked(task_id)
            self._check_revision(task, revision)
            self._require_action(task, "start", (CLAIMED,))
            if task.assignee != agent_name:
                raise LeaseError(
                    f"Задача «{task.id}» взята другим агентом: {task.assignee}.",
                    hint="Взять её может только текущий арендатор.",
                )
            if token is not None and token != task.claim_token:
                raise LeaseError(
                    f"Неверный claim_token для задачи «{task.id}».",
                    hint="Используйте токен, выданный в claim().",
                )
            now_iso = _format_ts(self._now())
            with self._conn:
                self._conn.execute(
                    "UPDATE tasks SET status=?, revision=revision+1, updated_at=? WHERE id=?",
                    (IN_PROGRESS, now_iso, task.id),
                )
                self._audit(agent_name, "task_start", task.id, {})
            return self._find_locked(task.id)

    def heartbeat(
        self,
        token: str,
        *,
        minutes: int = 30,
        revision: int | None = None,
    ) -> Claim:
        """Продлить аренду по claim_token (revision можно проверить дополнительно)."""
        clean_token = self._require_name(token, "token")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
            raise ValidationError(
                "minutes должен быть целым числом больше 0.",
                hint="Передайте minutes=30 или другое положительное число.",
            )
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE claim_token=?", (clean_token,)
            ).fetchone()
            if row is None:
                raise LeaseError(
                    "Аренда по этому токену не найдена или уже истекла.",
                    hint="Возьмите задачу заново через claim().",
                )
            task = self._row_to_task(row)
            if revision is not None:
                self._check_revision(task, revision)
            self._require_action(task, "heartbeat", ACTIVE_STATUSES)
            expires = _format_ts(self._now() + timedelta(minutes=minutes))
            now_iso = _format_ts(self._now())
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET claim_expires_at=?, revision=revision+1, updated_at=?
                    WHERE id=?
                    """,
                    (expires, now_iso, task.id),
                )
                self._audit(
                    task.assignee,
                    "task_heartbeat",
                    task.id,
                    {"minutes": minutes},
                )
            updated = self._find_locked(task.id)
            return Claim(task=updated, token=clean_token, expires_at=expires)

    def release(
        self,
        task_id: str,
        agent: str | None = None,
        *,
        revision: int | None = None,
        token: str | None = None,
    ) -> Task:
        """Досрочно освободить аренду, вернув задачу в todo.

        Можно указать агента и revision либо передать только claim_token:
        токен сам подтверждает право на аренду.
        """
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            task = self._find_locked(task_id)
            if token is None and revision is None and agent == task.claim_token:
                token, agent = agent, None
            if agent is None and token is None:
                raise ValidationError(
                    "release требует agent или token.",
                    hint="Передайте агента с revision или claim_token.",
                )
            if token is not None and token != task.claim_token:
                raise LeaseError(
                    f"Неверный claim_token для задачи «{task.id}».",
                    hint="Используйте токен, выданный в claim().",
                )
            agent_name = (
                self._require_name(agent, "agent")
                if agent is not None
                else (task.assignee or "")
            )
            if not agent_name:
                raise LeaseError(
                    f"У задачи «{task.id}» нет активного арендатора.",
                    hint="Возможно, аренда уже истекла; вызовите show().",
                )
            if revision is None:
                if token is None:
                    raise ValidationError(
                        "revision обязателен для release.",
                        hint="Возьмите revision из show() и повторите вызов.",
                    )
                revision = task.revision
            self._check_revision(task, revision)
            self._require_action(task, "release", ACTIVE_STATUSES)
            if task.assignee != agent_name:
                raise LeaseError(
                    f"Задача «{task.id}» взята агентом {task.assignee}, а не {agent_name}.",
                    hint="Освободить задачу может только текущий арендатор.",
                )
            now_iso = _format_ts(self._now())
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, assignee=NULL, claim_token=NULL,
                        claim_expires_at=NULL, revision=revision+1, updated_at=?
                    WHERE id=?
                    """,
                    (TODO, now_iso, task.id),
                )
                self._audit(agent_name, "task_release", task.id, {})
            return self._find_locked(task.id)

    def submit(
        self,
        task_id: str,
        agent: str,
        *,
        evidence: Any,
        revision: int,
        token: str | None = None,
        body: str | None = None,
    ) -> Task:
        """Отправить задачу в review с проверяемыми доказательствами."""
        agent_name = self._require_name(agent, "agent")
        if isinstance(evidence, str):
            clean_evidence = self._require_text(evidence, "evidence").strip()
            if not clean_evidence:
                raise ValidationError(
                    "evidence обязателен при submit.",
                    hint="Приложите ссылки, хеши или вывод тестов, подтверждающие работу.",
                )
            evidence_value: Any = clean_evidence
            stored_evidence = clean_evidence
        else:
            if self._evidence_checker is None:
                raise ValidationError(
                    "evidence должен быть строкой или структурированным списком.",
                    hint="Для структурированных доказательств нужен EvidenceChecker.",
                )
            if not evidence:
                raise ValidationError(
                    "evidence обязателен при submit.",
                    hint="Приложите doc_version, test_run, file_hash или audit_ref.",
                )
            evidence_value = evidence
            stored_evidence = json.dumps(evidence, ensure_ascii=False, default=str)
        if body is not None:
            body = self._require_text(body, "body")
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            task = self._find_locked(task_id)
            self._check_revision(task, revision)
            self._require_action(task, "submit", ACTIVE_STATUSES)
            if task.assignee != agent_name:
                raise LeaseError(
                    f"Задача «{task.id}» взята агентом {task.assignee}, а не {agent_name}.",
                    hint="Отправить на ревью может только текущий арендатор.",
                )
            if token is not None and token != task.claim_token:
                raise LeaseError(
                    f"Неверный claim_token для задачи «{task.id}».",
                    hint="Используйте токен, выданный в claim().",
                )
            mismatch = False
            report_dict: dict[str, Any] | None = None
            if self._evidence_checker is not None:
                from .evidence import EvidenceError

                report = self._evidence_checker.validate(
                    agent_name, task, evidence_value, now=self._now()
                )
                if not report.ok:
                    reasons = "; ".join(
                        str(item.get("reason") or "отклонено")
                        for item in report.rejected
                    )
                    if report.critical and not report.accepted:
                        message = (
                            "Доказательства не подтверждают работу "
                            "для критичной области."
                        )
                    elif report.critical and report.weak_only:
                        message = (
                            "Свободный текст без проверяемой ссылки "
                            "не принимается для критичной области."
                        )
                    else:
                        message = "Доказательства не подтверждают работу по задаче."
                    if reasons:
                        message = f"{message} Причины: {reasons}."
                    raise EvidenceError(message, report)
                mismatch = report.has_mismatch
                report_dict = report.as_dict()
            now_iso = _format_ts(self._now())
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, evidence=?, evidence_mismatch=?, evidence_report=?,
                        body=COALESCE(?, body),
                        claim_token=NULL, claim_expires_at=NULL,
                        revision=revision+1, updated_at=?
                    WHERE id=?
                    """,
                    (
                        REVIEW,
                        stored_evidence,
                        1 if mismatch else 0,
                        (
                            json.dumps(report_dict, ensure_ascii=False)
                            if report_dict is not None
                            else None
                        ),
                        body,
                        now_iso,
                        task.id,
                    ),
                )
                self._audit(
                    agent_name,
                    "task_submit",
                    task.id,
                    {
                        "evidence": stored_evidence,
                        "revision": revision,
                        "evidence_mismatch": mismatch,
                        "evidence_report": report_dict,
                    },
                )
            if mismatch and self._review_queue is not None:
                self._review_queue.add(
                    f"task/{task.id}",
                    "evidence_mismatch",
                    detail=(
                        f"задача {task.id}: заявленные действия не найдены "
                        "в аудите актора"
                    ),
                    actor=agent_name,
                )
            return self._find_locked(task.id)

    def accept(
        self,
        task_id: str,
        actor: str,
        *,
        revision: int,
        note: str = "",
    ) -> Task:
        """Принять задачу из review в done (reviewer/owner, не исполнитель)."""
        actor_name = self._require_name(actor, "actor")
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            task = self._find_locked(task_id)
            self._check_revision(task, revision)
            self._require_action(task, "accept", (REVIEW,))
            self._require_reviewer(task, actor_name)
            now_iso = _format_ts(self._now())
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, note=?, revision=revision+1, updated_at=?
                    WHERE id=?
                    """,
                    (DONE, note or None, now_iso, task.id),
                )
                self._audit(actor_name, "task_accept", task.id, {"note": note})
            return self._find_locked(task.id)

    def rework(
        self,
        task_id: str,
        actor: str,
        *,
        revision: int,
        reason: str = "",
        note: str | None = None,
    ) -> Task:
        """Вернуть задачу из review на доработку (reviewer/owner, не исполнитель)."""
        actor_name = self._require_name(actor, "actor")
        text = note if note is not None else reason
        text = self._require_text(text, "reason")
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            task = self._find_locked(task_id)
            self._check_revision(task, revision)
            self._require_action(task, "rework", (REVIEW,))
            self._require_reviewer(task, actor_name)
            now_iso = _format_ts(self._now())
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, assignee=NULL, claim_token=NULL,
                        claim_expires_at=NULL, note=?, revision=revision+1,
                        updated_at=?
                    WHERE id=?
                    """,
                    (REWORK, text or None, now_iso, task.id),
                )
                self._audit(actor_name, "task_rework", task.id, {"reason": text})
            return self._find_locked(task.id)

    def block(
        self,
        task_id: str,
        actor: str,
        *,
        revision: int,
        reason: str = "",
    ) -> Task:
        """Заблокировать задачу с причиной."""
        actor_name = self._require_name(actor, "actor")
        text = self._require_text(reason, "reason")
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            task = self._find_locked(task_id)
            self._check_revision(task, revision)
            self._require_action(task, "block", (TODO, CLAIMED, IN_PROGRESS, REWORK))
            now_iso = _format_ts(self._now())
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, assignee=NULL, claim_token=NULL,
                        claim_expires_at=NULL, note=?, revision=revision+1,
                        updated_at=?
                    WHERE id=?
                    """,
                    (BLOCKED, text or None, now_iso, task.id),
                )
                self._audit(actor_name, "task_block", task.id, {"reason": text})
            return self._find_locked(task.id)

    def cancel(
        self,
        task_id: str,
        actor: str,
        *,
        revision: int,
        reason: str = "",
    ) -> Task:
        """Отменить задачу."""
        actor_name = self._require_name(actor, "actor")
        text = self._require_text(reason, "reason")
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            task = self._find_locked(task_id)
            self._check_revision(task, revision)
            self._require_action(
                task,
                "cancel",
                (TODO, CLAIMED, IN_PROGRESS, REVIEW, REWORK, BLOCKED),
            )
            now_iso = _format_ts(self._now())
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, assignee=NULL, claim_token=NULL,
                        claim_expires_at=NULL, note=?, revision=revision+1,
                        updated_at=?
                    WHERE id=?
                    """,
                    (CANCELLED, text or None, now_iso, task.id),
                )
                self._audit(actor_name, "task_cancel", task.id, {"reason": text})
            return self._find_locked(task.id)

    # ------------------------------------------------------------------
    # Presence
    # ------------------------------------------------------------------
    def presence(self, name: str, activity: str = "") -> Presence:
        """Отметить агента онлайн и записать, что он делает."""
        agent_name = self._require_name(name, "name")
        text = self._require_text(activity, "activity")
        now_iso = _format_ts(self._now())
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO task_agents(name, last_seen, activity)
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
            self._release_expired_locked()
            rows = self._conn.execute(
                "SELECT * FROM task_agents ORDER BY name ASC"
            ).fetchall()
        threshold = self._now() - timedelta(seconds=ttl_seconds)
        result: list[Presence] = []
        for row in rows:
            last_seen = _parse_ts(row["last_seen"])
            if last_seen >= threshold:
                result.append(
                    Presence(
                        name=row["name"],
                        last_seen=row["last_seen"],
                        activity=row["activity"],
                        online=True,
                    )
                )
        return result

    # ------------------------------------------------------------------
    # Inbox и handoff
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
            self._release_expired_locked()
            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO task_messages
                        (sender, recipient, subject, body, ts, read, read_at)
                    VALUES (?, ?, ?, ?, ?, 0, NULL)
                    """,
                    (clean_sender, recipient, clean_subject, clean_body, now_iso),
                )
                message_id = int(cursor.lastrowid or 0)
            self._audit(
                clean_sender,
                "task_message_send",
                None,
                {"to": recipient, "subject": clean_subject},
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
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            if unread:
                rows = self._conn.execute(
                    """
                    SELECT * FROM task_messages
                    WHERE recipient=? AND read=0
                    ORDER BY ts DESC, id DESC
                    """,
                    (agent_name,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM task_messages
                    WHERE recipient=?
                    ORDER BY ts DESC, id DESC
                    """,
                    (agent_name,),
                ).fetchall()
        return [
            Message(
                id=row["id"],
                sender=row["sender"],
                recipient=row["recipient"],
                subject=row["subject"],
                body=row["body"],
                ts=row["ts"],
                read=bool(row["read"]),
            )
            for row in rows
        ]

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
            self._release_expired_locked()
            rows = self._conn.execute(
                f"SELECT id, recipient FROM task_messages WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
            found = {row["id"]: row["recipient"] for row in rows}
            foreign = [item for item in ids if found.get(item) not in (None, reader_name)]
            if foreign:
                raise TaskError(
                    "Нельзя отметить чужие письма: "
                    + ", ".join(str(item) for item in foreign)
                    + ".",
                    hint="Отмечайте только письма из своего inbox.",
                )
            now_iso = _format_ts(self._now())
            with self._conn:
                self._conn.execute(
                    f"""
                    UPDATE task_messages SET read=1, read_at=?
                    WHERE recipient=? AND read=0 AND id IN ({placeholders})
                    """,
                    (now_iso, reader_name, *ids),
                )
                changed = self._conn.execute("SELECT changes()").fetchone()[0]
        return int(changed)

    def handoff(
        self,
        task: str,
        done: str,
        left: str,
        important: str | bool = "",
        *,
        author: str = "anonymous",
    ) -> Any:
        """Сохранить handoff как документ handoffs/<task>.md в Store."""
        if not isinstance(task, str) or not _HANDOFF_TASK_RE.match(task):
            raise ValidationError(
                f"Некорректный id задачи для handoff: {task!r}.",
                hint="Используйте буквы, цифры, точку, дефис или подчёркивание.",
            )
        clean_author = self._require_name(author, "author")
        done_text = self._require_text(done, "done")
        left_text = self._require_text(left, "left")
        if isinstance(important, bool):
            important_text = "да" if important else "нет"
        elif isinstance(important, str):
            important_text = important
        else:
            raise ValidationError(
                "important должен быть строкой или bool.",
                hint="Передайте текст важного или True/False.",
            )
        now_iso = _format_ts(self._now())
        path = f"handoffs/{task}.md"
        content = (
            f"# Handoff: {task}\n\n"
            f"- Задача: {task}\n"
            f"- Автор: {clean_author}\n"
            f"- Время: {now_iso}\n"
            f"- Важное: {important_text}\n\n"
            f"## Сделано\n\n{done_text}\n\n"
            f"## Осталось\n\n{left_text}\n"
        )
        base_version: int | None
        try:
            base_version = self._store.read(path, actor=clean_author).version
        except DeletedError:
            self._store.restore(path, clean_author, f"handoff по задаче {task}")
            base_version = self._store.read(path, actor=clean_author).version
        except NotFoundError:
            base_version = None
        result = self._store.write(
            path,
            content,
            clean_author,
            f"handoff по задаче {task}",
            base_version=base_version,
        )
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO task_handoffs(task_id, path, author, version, ts)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        path=excluded.path,
                        author=excluded.author,
                        version=excluded.version,
                        ts=excluded.ts
                    """,
                    (task, path, clean_author, result.version, now_iso),
                )
        return result

    # ------------------------------------------------------------------
    # Кто знает путь и бриф
    # ------------------------------------------------------------------
    def who_knows(self, path: str) -> WhoKnows | None:
        """Кто последним менял путь (точный путь или вложенные документы)."""
        if not isinstance(path, str) or not path.strip():
            raise ValidationError(
                "path обязателен.",
                hint="Передайте путь, например src/artek_brain/tasks.py.",
            )
        clean = self._store._normalize_path(path)
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            row = self._conn.execute(
                """
                SELECT * FROM versions
                WHERE path=?
                ORDER BY ts DESC, id DESC
                LIMIT 1
                """,
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

        Содержит открытые задачи, занятые области, последние handoff и
        непрочитанный inbox. Длина не превышает limit символов.
        """
        name = self._require_name(agent_name, "agent_name")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 200:
            raise ValidationError(
                "limit должен быть целым числом не меньше 200.",
                hint="По умолчанию бриф ограничен 2000 символами.",
            )
        with self._lock:
            self._store._ensure_open()
            self._release_expired_locked()
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY priority ASC, id ASC"
            ).fetchall()
            tasks = [self._row_to_task(row) for row in rows]
            handoff_rows = self._conn.execute(
                "SELECT * FROM task_handoffs ORDER BY ts DESC, task_id ASC LIMIT 3"
            ).fetchall()
            unread_rows = self._conn.execute(
                """
                SELECT * FROM task_messages
                WHERE recipient=? AND read=0
                ORDER BY ts DESC, id DESC
                """,
                (name,),
            ).fetchall()

        def clip(value: str, width: int) -> str:
            return value if len(value) <= width else value[: width - 1] + "…"

        lines: list[str] = [f"# Бриф для {name}"]
        open_tasks = [task for task in tasks if task.status in OPEN_STATUSES]
        lines.append(f"Открытые задачи: {len(open_tasks)}")
        shown_open = open_tasks[:10]
        for task in shown_open:
            assignee = task.assignee or "свободна"
            lines.append(
                f"- {task.id} [{task.status}] p{task.priority} "
                f"{clip(task.title, 45)} ({assignee})"
            )
        hidden = len(open_tasks) - len(shown_open)
        if hidden > 0:
            lines.append(f"- … ещё {hidden} задач не показано")

        busy = [task for task in tasks if task.status in ACTIVE_STATUSES]
        lines.append(f"Занятые области: {len(busy)}")
        for task in busy[:6]:
            lines.append(
                f"- {clip(', '.join(task.area), 50)} — "
                f"{task.assignee or '—'} ({task.id})"
            )
        if len(busy) > 6:
            lines.append(f"- … ещё {len(busy) - 6} занятых задач")

        lines.append(f"Последние handoff: {len(handoff_rows)}")
        for row in handoff_rows:
            lines.append(
                f"- {row['task_id']} от {row['author']} в {row['ts']} "
                f"({row['path']} v{row['version']})"
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
