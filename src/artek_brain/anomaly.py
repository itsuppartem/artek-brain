"""Детерминированные детекторы аномалий по аудиту и версиям.

Пороги настраиваются. Инциденты пишутся в очередь ревью
``owner/inbox/incidents/...`` и при ``auto_suspend`` приостанавливают токен
актора. Снимает приостановку только owner.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from .access import Access, is_hidden, is_protected
from .errors import ArtekBrainError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS anomaly_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    actor TEXT,
    severity TEXT NOT NULL,
    summary TEXT NOT NULL,
    window_start TEXT,
    window_end TEXT,
    detail_json TEXT,
    dedupe_key TEXT NOT NULL UNIQUE,
    path TEXT,
    suspended INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_actor ON anomaly_incidents(actor);
CREATE INDEX IF NOT EXISTS idx_incidents_kind ON anomaly_incidents(kind);
"""

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class AnomalyConfig:
    """Пороги детекторов и режим авто-приостановки."""

    window_minutes: int = 10
    version_spike: int = 20
    mass_edit_docs: int = 10
    rollback_count: int = 3
    protected_attempts: int = 3
    hidden_reads: int = 1
    size_growth_factor: float = 5.0
    size_growth_min_bytes: int = 100_000
    auto_suspend: bool = False
    incident_prefix: str = "owner/inbox/incidents"

    def as_dict(self) -> dict[str, Any]:
        """Представить настройки словарём."""
        return {
            "window_minutes": self.window_minutes,
            "version_spike": self.version_spike,
            "mass_edit_docs": self.mass_edit_docs,
            "rollback_count": self.rollback_count,
            "protected_attempts": self.protected_attempts,
            "hidden_reads": self.hidden_reads,
            "size_growth_factor": self.size_growth_factor,
            "size_growth_min_bytes": self.size_growth_min_bytes,
            "auto_suspend": self.auto_suspend,
            "incident_prefix": self.incident_prefix,
        }


@dataclass(frozen=True)
class Incident:
    """Найденная аномалия."""

    id: int
    kind: str
    actor: str | None
    severity: str
    summary: str
    window_start: str
    window_end: str
    detail: dict[str, Any]
    path: str | None
    suspended: bool
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        """Представить инцидент словарём."""
        return {
            "id": self.id,
            "kind": self.kind,
            "actor": self.actor,
            "severity": self.severity,
            "summary": self.summary,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "detail": self.detail,
            "path": self.path,
            "suspended": self.suspended,
            "created_at": self.created_at,
        }


class AnomalyDetector:
    """Сканер аудита и версий с записью инцидентов в очередь ревью."""

    def __init__(
        self,
        store: Any,
        *,
        access: Access | None = None,
        board: Any = None,
        config: AnomalyConfig | Mapping[str, Any] | None = None,
        clock: Any = None,
    ) -> None:
        self.store = store
        self.access = access
        self.board = board
        self._clock = clock or _utc_now
        self._lock = threading.RLock()
        if isinstance(config, AnomalyConfig):
            self.config = config
        elif isinstance(config, Mapping):
            self.config = AnomalyConfig(
                **{key: value for key, value in config.items() if hasattr(AnomalyConfig, key)}
            )
        else:
            self.config = AnomalyConfig()
        with self.store._lock:
            self.store._ensure_open()
            with self.store._conn:
                self.store._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Сканирование
    # ------------------------------------------------------------------
    def scan(
        self,
        *,
        now: datetime | None = None,
        auto_suspend: bool | None = None,
    ) -> list[Incident]:
        """Найти аномалии, записать новые инциденты и, если включено, заморозить токен."""
        moment = now or self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
        start = moment - timedelta(minutes=self.config.window_minutes)
        start_iso = _format_ts(start)
        end_iso = _format_ts(moment)
        found: list[dict[str, Any]] = []
        found.extend(self._version_spike(start_iso, end_iso))
        found.extend(self._mass_edits(start_iso, end_iso))
        found.extend(self._repeated_rollbacks(start_iso, end_iso))
        found.extend(self._protected_attempts(start_iso, end_iso))
        found.extend(self._hidden_reads(start_iso, end_iso))
        found.extend(self._size_growth(start_iso, end_iso))

        suspend_flag = self.config.auto_suspend if auto_suspend is None else auto_suspend
        incidents: list[Incident] = []
        for item in found:
            incident = self._record(item, start_iso, end_iso, suspend_flag)
            if incident is not None:
                incidents.append(incident)
        return incidents

    # ------------------------------------------------------------------
    # Детекторы
    # ------------------------------------------------------------------
    def _version_spike(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT author, COUNT(*) AS c FROM versions
            WHERE ts>=? AND ts<=? GROUP BY author
            HAVING COUNT(*)>=? ORDER BY c DESC, author
            """,
            (start, end, self.config.version_spike),
        )
        return [
            {
                "kind": "version_spike",
                "actor": row["author"],
                "severity": "high",
                "summary": (
                    f"всплеск версий: {row['c']} за {self.config.window_minutes} мин"
                ),
                "detail": {"versions": int(row["c"])},
            }
            for row in rows
        ]

    def _mass_edits(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT author, COUNT(DISTINCT path) AS docs FROM versions
            WHERE ts>=? AND ts<=? GROUP BY author
            HAVING COUNT(DISTINCT path)>=? ORDER BY docs DESC, author
            """,
            (start, end, self.config.mass_edit_docs),
        )
        return [
            {
                "kind": "mass_edits",
                "actor": row["author"],
                "severity": "high",
                "summary": (
                    f"массовые правки: {row['docs']} документов за "
                    f"{self.config.window_minutes} мин"
                ),
                "detail": {"documents": int(row["docs"])},
            }
            for row in rows
        ]

    def _repeated_rollbacks(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT actor, COUNT(*) AS c FROM audit
            WHERE action IN ('rollback_version','rollback_author','rollback_batch')
              AND ts>=? AND ts<=? AND detail_json LIKE '%"ok": true%'
            GROUP BY actor HAVING COUNT(*)>=? ORDER BY c DESC, actor
            """,
            (start, end, self.config.rollback_count),
        )
        return [
            {
                "kind": "repeated_rollbacks",
                "actor": row["actor"],
                "severity": "medium",
                "summary": f"повторные откаты: {row['c']} за окно",
                "detail": {"rollbacks": int(row["c"])},
            }
            for row in rows
        ]

    def _protected_attempts(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT actor, path FROM audit
            WHERE action IN
                ('write','delete','restore','rollback_version',
                 'rollback_author','rollback_batch','proposal_submit')
              AND ts>=? AND ts<=? AND detail_json LIKE '%"ok": false%'
            """,
            (start, end),
        )
        counts: dict[str, list[str]] = {}
        for row in rows:
            path = row["path"]
            if not path or not is_protected(path):
                continue
            counts.setdefault(row["actor"], []).append(path)
        return [
            {
                "kind": "protected_write_attempts",
                "actor": actor,
                "severity": "high",
                "summary": (
                    f"попытки писать в защищённые пути: {len(paths)}"
                ),
                "detail": {"attempts": len(paths), "paths": sorted(set(paths))[:20]},
            }
            for actor, paths in counts.items()
            if len(paths) >= self.config.protected_attempts
        ]

    def _hidden_reads(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT actor, path, action FROM audit
            WHERE ts>=? AND ts<=?
            """,
            (start, end),
        )
        counts: dict[str, list[str]] = {}
        for row in rows:
            path = row["path"]
            if not path or not is_hidden(path):
                continue
            counts.setdefault(row["actor"], []).append(f"{row['action']}:{path}")
        return [
            {
                "kind": "hidden_reads",
                "actor": actor,
                "severity": "critical",
                "summary": f"обращения к hidden/: {len(items)}",
                "detail": {"attempts": len(items), "paths": sorted(set(items))[:20]},
            }
            for actor, items in counts.items()
            if len(items) >= self.config.hidden_reads
        ]

    def _size_growth(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT path, n, size, author FROM versions
            WHERE ts>=? AND ts<=? ORDER BY path, n
            """,
            (start, end),
        )
        incidents: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if row["path"] in seen:
                continue
            previous = self._query(
                "SELECT size FROM versions WHERE path=? AND n<? ORDER BY n DESC LIMIT 1",
                (row["path"], row["n"]),
            )
            if not previous:
                continue
            old_size = int(previous[0]["size"] or 0)
            new_size = int(row["size"] or 0)
            if old_size <= 0:
                continue
            if (
                new_size >= self.config.size_growth_min_bytes
                and new_size / old_size >= self.config.size_growth_factor
            ):
                seen.add(row["path"])
                incidents.append(
                    {
                        "kind": "size_growth",
                        "actor": row["author"],
                        "severity": "medium",
                        "summary": (
                            f"резкий рост размера «{row['path']}»: "
                            f"{old_size} → {new_size} байт"
                        ),
                        "detail": {
                            "path": row["path"],
                            "from_size": old_size,
                            "to_size": new_size,
                        },
                    }
                )
        return incidents

    # ------------------------------------------------------------------
    # Запись инцидентов
    # ------------------------------------------------------------------
    def _query(self, sql: str, params: Iterable[Any]) -> list[sqlite3.Row]:
        with self.store._lock:
            self.store._ensure_open()
            return self.store._conn.execute(sql, tuple(params)).fetchall()

    def _record(
        self,
        item: dict[str, Any],
        start_iso: str,
        end_iso: str,
        auto_suspend: bool,
    ) -> Incident | None:
        actor = item.get("actor")
        dedupe_key = f"{item['kind']}:{actor}:{start_iso}"
        detail_json = json.dumps(item.get("detail") or {}, ensure_ascii=False)
        now_iso = _format_ts(self._now())
        with self._lock:
            self.store._ensure_open()
            with self.store._conn:
                existing = self.store._conn.execute(
                    "SELECT * FROM anomaly_incidents WHERE dedupe_key=?",
                    (dedupe_key,),
                ).fetchone()
                if existing is not None:
                    return None
                cursor = self.store._conn.execute(
                    """
                    INSERT INTO anomaly_incidents
                        (kind, actor, severity, summary, window_start, window_end,
                         detail_json, dedupe_key, path, suspended, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
                    """,
                    (
                        item["kind"],
                        actor,
                        item.get("severity", "medium"),
                        item["summary"],
                        start_iso,
                        end_iso,
                        detail_json,
                        dedupe_key,
                        now_iso,
                    ),
                )
                incident_id = int(cursor.lastrowid)
        path = self._write_incident_file(
            incident_id, item, start_iso, end_iso
        )
        suspended = False
        if auto_suspend and actor and self.access is not None:
            suspended = self._suspend_actor(actor)
        with self._lock:
            with self.store._conn:
                self.store._conn.execute(
                    "UPDATE anomaly_incidents SET path=?, suspended=? WHERE id=?",
                    (path, 1 if suspended else 0, incident_id),
                )
        self.store._audit_ok(
            "system",
            "anomaly_incident",
            path,
            {
                "incident_id": incident_id,
                "kind": item["kind"],
                "actor": actor,
                "severity": item.get("severity", "medium"),
                "suspended": suspended,
            },
        )
        return Incident(
            id=incident_id,
            kind=item["kind"],
            actor=actor,
            severity=item.get("severity", "medium"),
            summary=item["summary"],
            window_start=start_iso,
            window_end=end_iso,
            detail=item.get("detail") or {},
            path=path,
            suspended=suspended,
            created_at=now_iso,
        )

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _write_incident_file(
        self,
        incident_id: int,
        item: dict[str, Any],
        start_iso: str,
        end_iso: str,
    ) -> str:
        prefix = self.config.incident_prefix.strip().strip("/")
        stamp = _format_ts(self._now()).replace(":", "").replace("-", "").replace("+", "")
        actor = _SAFE_NAME_RE.sub("_", str(item.get("actor") or "unknown"))[:60]
        kind = _SAFE_NAME_RE.sub("_", str(item["kind"]))[:60]
        path = f"{prefix}/{stamp}-{incident_id}-{kind}-{actor}.md"
        content = (
            f"# Инцидент #{incident_id}: {item['kind']}\n\n"
            f"- актор: {item.get('actor')}\n"
            f"- важность: {item.get('severity', 'medium')}\n"
            f"- окно: {start_iso} .. {end_iso}\n"
            f"- сводка: {item['summary']}\n\n"
            "```json\n"
            + json.dumps(item.get("detail") or {}, ensure_ascii=False, indent=2)
            + "\n```\n"
        )
        try:
            self.store.write(
                path,
                content,
                "system",
                f"аномалия {item['kind']} у {item.get('actor')}",
            )
        except ArtekBrainError:
            return path
        if self.board is not None:
            try:
                self.board.send(
                    "owner",
                    f"Инцидент: {item['kind']}",
                    content,
                    sender="system",
                )
            except Exception:
                pass
        return path

    def _suspend_actor(self, actor: str) -> bool:
        if self.access is None:
            return False
        tokens = [
            token
            for token in self.access.list_tokens()
            if token.name == actor and not token.revoked and not token.suspended
        ]
        if not tokens or any(token.role == "owner" for token in tokens):
            return False
        changed = self.access.suspend_name(actor)
        return changed > 0

    # ------------------------------------------------------------------
    # Чтение инцидентов
    # ------------------------------------------------------------------
    def incidents(
        self,
        *,
        actor: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[Incident]:
        """Последние инциденты с фильтрами."""
        clauses: list[str] = []
        params: list[Any] = []
        if actor is not None:
            clauses.append("actor=?")
            params.append(actor)
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        sql = "SELECT * FROM anomaly_incidents"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self._query(sql, params)
        result: list[Incident] = []
        for row in rows:
            try:
                detail = json.loads(row["detail_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                detail = {}
            result.append(
                Incident(
                    id=int(row["id"]),
                    kind=row["kind"],
                    actor=row["actor"],
                    severity=row["severity"],
                    summary=row["summary"],
                    window_start=row["window_start"] or "",
                    window_end=row["window_end"] or "",
                    detail=detail if isinstance(detail, dict) else {},
                    path=row["path"],
                    suspended=bool(row["suspended"]),
                    created_at=row["created_at"],
                )
            )
        return result
