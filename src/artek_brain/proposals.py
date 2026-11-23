"""Предложения агентов и утверждённое знание.

Агент пишет только в ``proposals/<agent>/...``. В ``approved/`` (и другие
защищённые области) документ попадает только через ``approve`` от имени
owner/human-ревьюера, причём автор предложения не может утвердить его сам.
Каждое утверждение записывается вместе с хешем версии.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .upkeep import load_settings as load_upkeep_settings
from .access import Access, Principal, is_hidden, is_protected
from .errors import NotFoundError, PermissionDenied, ValidationError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    agent TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER,
    sha256 TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    approved_version INTEGER,
    approved_sha256 TEXT,
    rejected_by TEXT,
    rejected_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_agent ON proposals(agent);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);

CREATE TABLE IF NOT EXISTS proposal_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_path TEXT NOT NULL,
    proposal_version INTEGER NOT NULL,
    proposal_sha256 TEXT NOT NULL,
    target_path TEXT NOT NULL,
    target_version INTEGER NOT NULL,
    target_sha256 TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_target ON proposal_approvals(target_path);
"""

STATUSES = ("pending", "approved", "rejected")
_APPROVER_ROLES = ("owner", "human")
_APPROVABLE_PREFIXES = ("approved", "rules", "scoring", "owner")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _first_segment(path: str) -> str:
    return path.strip("/").split("/", 1)[0].casefold()


@dataclass(frozen=True)
class Proposal:
    """Запись предложения и его утверждения."""

    path: str
    agent: str
    target: str
    status: str
    version: int | None
    sha256: str | None
    reason: str | None
    created_at: str
    updated_at: str
    approved_by: str | None = None
    approved_at: str | None = None
    approved_version: int | None = None
    approved_sha256: str | None = None
    rejected_by: str | None = None
    rejected_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Представить предложение словарём."""
        return {
            "path": self.path,
            "agent": self.agent,
            "target": self.target,
            "status": self.status,
            "version": self.version,
            "sha256": self.sha256,
            "reason": self.reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "approved_version": self.approved_version,
            "approved_sha256": self.approved_sha256,
            "rejected_by": self.rejected_by,
            "rejected_at": self.rejected_at,
        }


@dataclass(frozen=True)
class Approval:
    """Факт утверждения: кто, что и на каком хеше утвердил."""

    proposal_path: str
    proposal_version: int
    proposal_sha256: str
    target_path: str
    target_version: int
    target_sha256: str
    approved_by: str
    ts: str

    def as_dict(self) -> dict[str, Any]:
        """Представить утверждение словарём."""
        return {
            "proposal_path": self.proposal_path,
            "proposal_version": self.proposal_version,
            "proposal_sha256": self.proposal_sha256,
            "target_path": self.target_path,
            "target_version": self.target_version,
            "target_sha256": self.target_sha256,
            "approved_by": self.approved_by,
            "ts": self.ts,
        }


class ProposalStore:
    """Предложения и утверждение версий поверх Store и Access."""

    def __init__(self, store: Any, access: Access) -> None:
        self.store = store
        self.access = access
        self._lock = threading.RLock()
        with self.store._lock:
            self.store._ensure_open()
            with self.store._conn:
                self.store._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return _utc_now()

    def _verify(self, token: str, action: str, path: str | None) -> Principal:
        try:
            return self.access.verify(token)
        except PermissionDenied as exc:
            self.store._log_failure(action, path, exc.actor, exc, None, None)
            raise

    def _deny(
        self,
        action: str,
        path: str | None,
        principal: Principal,
        required: str,
        reason: str,
    ) -> None:
        exc = PermissionDenied(
            action, path, required, actor=principal.name, reason=reason
        )
        self.store._log_failure(action, path, principal.name, exc, None, None)
        raise exc

    @staticmethod
    def _safe_agent(name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(
                "Имя агента обязательно.",
                hint="Передайте непустое имя агента.",
            )
        value = name.strip()
        if "/" in value or "\\" in value or value in (".", "..") or len(value) > 200:
            raise ValidationError(
                f"Некорректное имя агента {name!r}.",
                hint="Имя агента не должно содержать / и .. (до 200 символов).",
            )
        return value

    def _proposal_path(self, agent: str, path: str) -> str:
        agent = self._safe_agent(agent)
        normalized = self.store._normalize_path(path)
        prefix = f"proposals/{agent}/"
        if normalized.startswith("proposals/"):
            if not normalized.startswith(prefix):
                raise PermissionDenied(
                    "proposal_submit",
                    normalized,
                    "write",
                    actor=agent,
                    reason="агент пишет только в proposals/<своё имя>/...",
                )
            return normalized
        return prefix + normalized

    @staticmethod
    def _target_for(agent: str, proposal_path: str, target: str | None) -> str:
        if target is not None:
            return proposal_path  # значение заменит вызывающий после нормализации
        prefix = f"proposals/{agent}/"
        relative = (
            proposal_path[len(prefix) :]
            if proposal_path.startswith(prefix)
            else proposal_path
        )
        return f"approved/{relative}"

    def _row_to_proposal(self, row: sqlite3.Row) -> Proposal:
        return Proposal(
            path=row["path"],
            agent=row["agent"],
            target=row["target"],
            status=row["status"],
            version=row["version"],
            sha256=row["sha256"],
            reason=row["reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
            approved_version=row["approved_version"],
            approved_sha256=row["approved_sha256"],
            rejected_by=row["rejected_by"],
            rejected_at=row["rejected_at"],
        )

    def _find(self, path: str) -> sqlite3.Row | None:
        with self.store._lock:
            self.store._ensure_open()
            return self.store._conn.execute(
                "SELECT * FROM proposals WHERE path=?", (path,)
            ).fetchone()

    def _require_proposal(self, path: str) -> sqlite3.Row:
        row = self._find(path)
        if row is None:
            raise NotFoundError(
                f"Предложение «{path}» не найдено.",
                hint="Посмотрите список через brain proposal list.",
            )
        return row

    # ------------------------------------------------------------------
    # Предложения
    # ------------------------------------------------------------------
    def submit(
        self,
        token: str,
        path: str,
        content: str,
        reason: str,
        *,
        base_version: int | None = None,
        target: str | None = None,
    ) -> Proposal:
        """Создать или обновить предложение агента (только в своём каталоге)."""
        principal = self._verify(token, "proposal_submit", path)
        if principal.role == "auditor":
            self._deny(
                "proposal_submit", path, principal, "write",
                "роль auditor не может предлагать изменения",
            )
        if not principal.has_any_write():
            self._deny(
                "proposal_submit", path, principal, "write",
                "у токена нет права записи",
            )
        agent = self._safe_agent(principal.name)
        proposal_path = self._proposal_path(agent, path)
        if target is not None:
            target_path = self.store._normalize_path(target)
            if is_hidden(target_path):
                self._deny(
                    "proposal_submit", target_path, principal, "owner",
                    "скрытые проверки нельзя утверждать из предложений",
                )
            if not self._approvable(target_path):
                raise ValidationError(
                    f"Цель утверждения «{target_path}» не в защищённой области.",
                    hint="Укажите документ решений или путь в approved/, rules/, scoring/, owner/.",
                )
        else:
            target_path = self._target_for(agent, proposal_path, None)
            target_path = self.store._normalize_path(target_path)

        result = self.store.write(
            proposal_path,
            content,
            agent,
            reason,
            base_version=base_version,
        )
        now = _format_ts(self._now())
        with self._lock:
            self.store._ensure_open()
            with self.store._conn:
                existing = self.store._conn.execute(
                    "SELECT * FROM proposals WHERE path=?", (proposal_path,)
                ).fetchone()
                if existing is None:
                    self.store._conn.execute(
                        """
                        INSERT INTO proposals
                            (path, agent, target, status, version, sha256, reason,
                             created_at, updated_at)
                        VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                        """,
                        (
                            proposal_path,
                            agent,
                            target_path,
                            result.version,
                            result.sha256,
                            reason,
                            now,
                            now,
                        ),
                    )
                else:
                    self.store._conn.execute(
                        """
                        UPDATE proposals
                        SET target=?, status='pending', version=?, sha256=?,
                            reason=?, updated_at=?, approved_by=NULL,
                            approved_at=NULL, approved_version=NULL,
                            approved_sha256=NULL, rejected_by=NULL, rejected_at=NULL
                        WHERE path=?
                        """,
                        (
                            target_path,
                            result.version,
                            result.sha256,
                            reason,
                            now,
                            proposal_path,
                        ),
                    )
                self.store._audit_ok(
                    agent,
                    "proposal_submit",
                    proposal_path,
                    {
                        "version": result.version,
                        "sha256": result.sha256,
                        "target": target_path,
                    },
                )
        return self._row_to_proposal(self._require_proposal(proposal_path))

    def list(self, token: str, *, status: str | None = None) -> list[Proposal]:
        """Список предложений: агент видит только свои, ревьюер — все."""
        principal = self._verify(token, "proposal_list", None)
        clauses: list[str] = []
        params: list[Any] = []
        if principal.role == "agent":
            clauses.append("agent=?")
            params.append(principal.name)
        if status is not None:
            if status not in STATUSES:
                raise ValidationError(
                    f"Неизвестный статус предложения «{status}».",
                    hint="Допустимо: pending, approved, rejected.",
                )
            clauses.append("status=?")
            params.append(status)
        sql = "SELECT * FROM proposals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id DESC"
        with self.store._lock:
            self.store._ensure_open()
            rows = self.store._conn.execute(sql, params).fetchall()
        return [self._row_to_proposal(row) for row in rows]

    def show(self, token: str, path: str) -> Proposal:
        """Показать одно предложение с учётом прав."""
        principal = self._verify(token, "proposal_show", path)
        agent = self._safe_agent(principal.name)
        normalized = self.store._normalize_path(path)
        if not normalized.startswith("proposals/"):
            normalized = f"proposals/{agent}/{normalized}"
        row = self._require_proposal(normalized)
        if principal.role == "agent" and row["agent"] != principal.name:
            self._deny(
                "proposal_show", normalized, principal, "read",
                "агент видит только свои предложения",
            )
        return self._row_to_proposal(row)

    # ------------------------------------------------------------------
    # Утверждение и отклонение
    # ------------------------------------------------------------------
    def approve(
        self,
        token: str,
        path: str,
        *,
        target: str | None = None,
        reason: str = "утверждено ревьюером",
    ) -> dict[str, Any]:
        """Утвердить версию предложения и перенести её в защищённую область."""
        principal = self._verify(token, "proposal_approve", path)
        if principal.role not in _APPROVER_ROLES:
            self._deny(
                "proposal_approve", path, principal, "owner",
                "утверждать может только owner или human-ревьюер",
            )
        normalized = self.store._normalize_path(path)
        if not normalized.startswith("proposals/"):
            normalized = f"proposals/{principal.name}/{normalized}"
        row = self._require_proposal(normalized)
        proposal = self._row_to_proposal(row)
        if proposal.agent == principal.name:
            self._deny(
                "proposal_approve", normalized, principal, "owner",
                "автор предложения не может утвердить его сам",
            )
        current = self.store.read(normalized, actor=principal.name)
        info = self.store.history(normalized, limit=1)[0]
        if info.author == principal.name:
            self._deny(
                "proposal_approve", normalized, principal, "owner",
                "автор версии не может утвердить её сам",
            )
        target_path = (
            self.store._normalize_path(target) if target is not None else proposal.target
        )
        if is_hidden(target_path):
            self._deny(
                "proposal_approve", target_path, principal, "owner",
                "скрытые проверки нельзя утверждать из предложений",
            )
        if not self._approvable(target_path):
            raise ValidationError(
                f"Цель утверждения «{target_path}» не в защищённой области.",
                hint="Укажите документ решений или путь в approved/, rules/, scoring/, owner/.",
            )

        current_target = None
        try:
            current_target = self.store.read(target_path, actor=principal.name)
        except NotFoundError:
            current_target = None
        base_version = current_target.version if current_target is not None else None
        written = self.store.write(
            target_path,
            current.content,
            principal.name,
            reason,
            base_version=base_version,
        )
        now = _format_ts(self._now())
        approval = Approval(
            proposal_path=normalized,
            proposal_version=current.version,
            proposal_sha256=info.sha256,
            target_path=target_path,
            target_version=written.version,
            target_sha256=written.sha256,
            approved_by=principal.name,
            ts=now,
        )
        with self._lock:
            self.store._ensure_open()
            with self.store._conn:
                self.store._conn.execute(
                    """
                    UPDATE proposals
                    SET status='approved', approved_by=?, approved_at=?,
                        approved_version=?, approved_sha256=?, updated_at=?
                    WHERE path=?
                    """,
                    (
                        principal.name,
                        now,
                        current.version,
                        approval.proposal_sha256,
                        now,
                        normalized,
                    ),
                )
                self.store._conn.execute(
                    """
                    INSERT INTO proposal_approvals
                        (proposal_path, proposal_version, proposal_sha256,
                         target_path, target_version, target_sha256, approved_by, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval.proposal_path,
                        approval.proposal_version,
                        approval.proposal_sha256,
                        approval.target_path,
                        approval.target_version,
                        approval.target_sha256,
                        approval.approved_by,
                        approval.ts,
                    ),
                )
                self.store._audit_ok(
                    principal.name,
                    "proposal_approve",
                    normalized,
                    approval.as_dict(),
                )
        return {"proposal": self._row_to_proposal(self._require_proposal(normalized)).as_dict(), "approval": approval.as_dict()}

    def reject(
        self,
        token: str,
        path: str,
        *,
        reason: str = "",
    ) -> Proposal:
        """Отклонить предложение (owner или human-ревьюер)."""
        principal = self._verify(token, "proposal_reject", path)
        if principal.role not in _APPROVER_ROLES:
            self._deny(
                "proposal_reject", path, principal, "owner",
                "отклонять может только owner или human-ревьюер",
            )
        normalized = self.store._normalize_path(path)
        if not normalized.startswith("proposals/"):
            normalized = f"proposals/{principal.name}/{normalized}"
        self._require_proposal(normalized)
        now = _format_ts(self._now())
        with self._lock:
            self.store._ensure_open()
            with self.store._conn:
                self.store._conn.execute(
                    """
                    UPDATE proposals
                    SET status='rejected', rejected_by=?, rejected_at=?, updated_at=?
                    WHERE path=?
                    """,
                    (principal.name, now, now, normalized),
                )
                self.store._audit_ok(
                    principal.name,
                    "proposal_reject",
                    normalized,
                    {"reason": reason},
                )
        return self._row_to_proposal(self._require_proposal(normalized))

    def _approvable(self, target_path: str) -> bool:
        """Цель утверждения: защищённая область или документ решений."""
        if load_upkeep_settings(self.store.root).is_decision(target_path):
            return True
        return is_protected(target_path) and _first_segment(target_path) in _APPROVABLE_PREFIXES

    # ------------------------------------------------------------------
    # Защита утверждённого знания
    # ------------------------------------------------------------------
    def guard_write(
        self, principal: Principal, path: str, content: str | None = None
    ) -> None:
        """Не дать тихо переписать approved/: правка только через утверждение."""
        if not is_protected(path) or _first_segment(path) != "approved":
            return
        if principal.role == "agent":
            self._deny(
                "write", path, principal, "owner",
                "утверждённое знание меняет только утверждение предложения",
            )
        if content is None:
            return
        with self.store._lock:
            self.store._ensure_open()
            exists = self.store._conn.execute(
                "SELECT 1 FROM versions WHERE path=? LIMIT 1", (path,)
            ).fetchone()
        if exists is None:
            return
        digest = _sha256(content)
        with self.store._lock:
            self.store._ensure_open()
            row = self.store._conn.execute(
                """
                SELECT 1 FROM proposal_approvals
                WHERE target_path=? AND target_sha256=?
                ORDER BY id DESC LIMIT 1
                """,
                (path, digest),
            ).fetchone()
        if row is None:
            self._deny(
                "write", path, principal, "owner",
                "правка approved/ допускается только через утверждение предложения",
            )

    def verify_approvals(self, token: str) -> list[dict[str, Any]]:
        """Найти утверждённые документы, изменённые после утверждения."""
        principal = self._verify(token, "proposal_verify", None)
        if principal.role not in _APPROVER_ROLES and principal.role != "auditor":
            self._deny(
                "proposal_verify", None, principal, "owner",
                "проверка утверждений доступна owner, human и auditor",
            )
        with self.store._lock:
            self.store._ensure_open()
            rows = self.store._conn.execute(
                """
                SELECT a.* FROM proposal_approvals a
                JOIN (
                    SELECT target_path, MAX(id) AS id
                    FROM proposal_approvals GROUP BY target_path
                ) last ON last.id=a.id
                ORDER BY a.target_path
                """
            ).fetchall()
        drift: list[dict[str, Any]] = []
        for row in rows:
            try:
                info = self.store.history(row["target_path"], limit=1)[0]
            except Exception:
                continue
            if info.sha256 != row["target_sha256"]:
                drift.append(
                    {
                        "target_path": row["target_path"],
                        "approved_by": row["approved_by"],
                        "approved_version": row["target_version"],
                        "approved_sha256": row["target_sha256"],
                        "current_version": info.n,
                        "current_sha256": info.sha256,
                        "current_author": info.author,
                    }
                )
        return drift
