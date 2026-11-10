"""Очередь ревью знаний: причины, версии, решения владельца/человека.

Таблица создаётся в том же SQLite-хранилище, что и документы. В модуле нет
сети и LLM. Действия approve/reject/defer разрешены только owner/human.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .errors import NotFoundError, PermissionDenied, ValidationError
from .knowledge import Document, iter_documents, now_utc, parse_date

OPEN = "open"
APPROVED = "approved"
REJECTED = "rejected"
DEFERRED = "deferred"

STATUSES = (OPEN, APPROVED, REJECTED, DEFERRED)
"""Допустимые статусы записи очереди."""

DRIFT = "drift"
STATUS_REVIEW = "status_review"
OVERDUE_REVIEW = "overdue_review"
EVIDENCE_MISMATCH = "evidence_mismatch"

REASONS = (DRIFT, STATUS_REVIEW, OVERDUE_REVIEW, EVIDENCE_MISMATCH)
"""Причины попадания документа в очередь."""

_REVIEW_ROLES = ("owner", "human")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    version INTEGER,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    actor TEXT,
    note TEXT,
    defer_until TEXT,
    UNIQUE(path, reason, version)
);

CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status);
CREATE INDEX IF NOT EXISTS idx_review_queue_path ON review_queue(path);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_queue_unique
    ON review_queue(path, reason, COALESCE(version, -1));
"""


def _format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class QueueItem:
    """Запись очереди ревью с причиной и ссылкой на версию."""

    id: int
    path: str
    version: int | None
    reason: str
    detail: str
    status: str
    created_at: str
    updated_at: str
    actor: str | None = None
    note: str | None = None
    defer_until: str | None = None

    @property
    def ref(self) -> str:
        """Ссылка на версию документа."""
        return f"{self.path}@{self.version}" if self.version else self.path

    def as_dict(self) -> dict[str, Any]:
        """Представить запись словарём."""
        return {
            "id": self.id,
            "path": self.path,
            "version": self.version,
            "ref": self.ref,
            "reason": self.reason,
            "detail": self.detail,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "actor": self.actor,
            "note": self.note,
            "defer_until": self.defer_until,
        }


class ReviewQueue:
    """Очередь документов, требующих проверки человеком."""

    def __init__(self, store: Any, access: Any | None = None) -> None:
        if hasattr(store, "store") and hasattr(store, "access"):
            access = access or store.access
            store = store.store
        self.store = store
        self.access = access
        with self.store._lock:
            self.store._ensure_open()
            with self.store._conn:
                self.store._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------
    def _row_to_item(self, row: Any) -> QueueItem:
        return QueueItem(
            id=int(row["id"]),
            path=row["path"],
            version=int(row["version"]) if row["version"] is not None else None,
            reason=row["reason"],
            detail=row["detail"] or "",
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            actor=row["actor"],
            note=row["note"],
            defer_until=row["defer_until"],
        )

    def _principal(self, token: str) -> Any:
        if self.access is None:
            raise ValidationError(
                "Очередь ревью создана без Access: проверка прав недоступна.",
                hint="Передайте Access при создании ReviewQueue.",
            )
        return self.access.verify(token)

    def _load(self, item_id: int) -> QueueItem:
        if not isinstance(item_id, int) or isinstance(item_id, bool):
            raise ValidationError(
                "item_id должен быть целым числом.",
                hint="Возьмите id из queue.list().",
            )
        with self.store._lock:
            self.store._ensure_open()
            row = self.store._conn.execute(
                "SELECT * FROM review_queue WHERE id=?", (item_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(
                f"Запись очереди ревью #{item_id} не найдена.",
                hint="Посмотрите актуальные id через queue.list().",
            )
        return self._row_to_item(row)

    def _require_reviewer(self, token: str, action: str, item: QueueItem) -> Any:
        principal = self._principal(token)
        if principal.role not in _REVIEW_ROLES:
            raise PermissionDenied(
                f"review_{action}",
                item.path,
                "owner или human",
                actor=principal.name,
                reason="решение по очереди ревью принимает только owner/human",
            )
        return principal

    # ------------------------------------------------------------------
    # Наполнение и просмотр
    # ------------------------------------------------------------------
    def add(
        self,
        path: str,
        reason: str,
        *,
        version: int | None = None,
        detail: str = "",
        actor: str = "system",
    ) -> QueueItem:
        """Добавить запись; повтор той же версии и причины игнорируется."""
        if not isinstance(path, str) or not path.strip():
            raise ValidationError(
                "path обязателен.",
                hint="Передайте логический путь документа.",
            )
        if reason not in REASONS:
            raise ValidationError(
                f"Неизвестная причина ревью «{reason}».",
                hint="Допустимо: " + ", ".join(REASONS) + ".",
            )
        item, _ = self._add(path.strip(), reason, version, detail, actor)
        return item

    def _add(
        self,
        path: str,
        reason: str,
        version: int | None,
        detail: str,
        actor: str,
    ) -> tuple[QueueItem, bool]:
        moment = _format_ts(now_utc())
        with self.store._lock:
            self.store._ensure_open()
            with self.store._conn:
                cursor = self.store._conn.execute(
                    """
                    INSERT OR IGNORE INTO review_queue
                        (path, version, reason, detail, status, created_at,
                         updated_at, actor, note, defer_until)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (path, version, reason, detail, OPEN, moment, moment, actor),
                )
                inserted = bool(cursor.rowcount)
                row = self.store._conn.execute(
                    """
                    SELECT * FROM review_queue
                    WHERE path=? AND reason=? AND version IS ?
                    """,
                    (path, reason, version),
                ).fetchone()
        return self._row_to_item(row), inserted

    def sync(
        self,
        brain: Any,
        token: str,
        *,
        prefix: str = "",
    ) -> list[QueueItem]:
        """Добавить документы со статусом review и просроченным review_by."""
        added: list[QueueItem] = []
        for doc in iter_documents(brain, token, prefix):
            if doc.status == "review":
                item, inserted = self._add(
                    doc.path,
                    STATUS_REVIEW,
                    doc.version,
                    "документ ждёт проверки (status=review)",
                    "system",
                )
                if inserted:
                    added.append(item)
            overdue = self._is_overdue(doc)
            if overdue:
                item, inserted = self._add(
                    doc.path,
                    OVERDUE_REVIEW,
                    doc.version,
                    f"review_by={doc.data.get('review_by')} уже прошёл",
                    "system",
                )
                if inserted:
                    added.append(item)
        return added

    @staticmethod
    def _is_overdue(doc: Document) -> bool:
        moment = parse_date(doc.data.get("review_by"))
        return moment is not None and moment < now_utc()

    def list(
        self,
        token: str | None = None,
        *,
        status: str | None = OPEN,
        path: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[QueueItem]:
        """Список записей очереди с фильтром по правам токена."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValidationError(
                "limit должен быть целым числом от 1 до 1000.",
                hint="Передайте limit=100.",
            )
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValidationError(
                "offset должен быть целым числом >= 0.",
                hint="Передайте offset=0.",
            )
        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            if status not in STATUSES:
                raise ValidationError(
                    f"Неизвестный статус очереди «{status}».",
                    hint="Допустимо: " + ", ".join(STATUSES) + " или all.",
                )
            clauses.append("status=?")
            params.append(status)
        if path:
            clauses.append("path=?")
            params.append(path)
        sql = "SELECT * FROM review_queue"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.store._lock:
            self.store._ensure_open()
            rows = self.store._conn.execute(sql, params).fetchall()
        items = [self._row_to_item(row) for row in rows]
        if token is None or self.access is None:
            return items
        principal = self._principal(token)
        if principal.role == "owner":
            return items
        return [item for item in items if principal.can_read(item.path)]

    def count_open(self) -> int:
        """Число открытых записей очереди (для метрик)."""
        with self.store._lock:
            self.store._ensure_open()
            row = self.store._conn.execute(
                "SELECT COUNT(*) AS c FROM review_queue WHERE status=?", (OPEN,)
            ).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------
    # Решения
    # ------------------------------------------------------------------
    def approve(self, token: str, item_id: int, note: str = "") -> QueueItem:
        """Одобрить запись очереди (owner/human)."""
        return self._decide(token, item_id, APPROVED, "approve", note, None)

    def reject(self, token: str, item_id: int, note: str = "") -> QueueItem:
        """Отклонить запись очереди (owner/human)."""
        return self._decide(token, item_id, REJECTED, "reject", note, None)

    def defer(
        self,
        token: str,
        item_id: int,
        note: str = "",
        until: str | None = None,
    ) -> QueueItem:
        """Отложить запись очереди (owner/human)."""
        return self._decide(token, item_id, DEFERRED, "defer", note, until)

    def _decide(
        self,
        token: str,
        item_id: int,
        status: str,
        action: str,
        note: str,
        until: str | None,
    ) -> QueueItem:
        item = self._load(item_id)
        principal = self._require_reviewer(token, action, item)
        if until is not None:
            parsed = parse_date(until)
            if parsed is None:
                raise ValidationError(
                    f"Некорректная дата defer until={until!r}.",
                    hint="Передайте дату ISO, например 2026-12-31.",
                )
        moment = _format_ts(now_utc())
        with self.store._lock:
            self.store._ensure_open()
            with self.store._conn:
                self.store._conn.execute(
                    """
                    UPDATE review_queue
                    SET status=?, updated_at=?, actor=?, note=?, defer_until=?
                    WHERE id=?
                    """,
                    (status, moment, principal.name, note, until, item_id),
                )
        return self._load(item_id)
