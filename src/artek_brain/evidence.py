"""Проверка доказательств при отправке задачи на ревью.

Доказательство должно ссылаться на проверяемый факт: версию документа в Store,
системный прогон тестов, хеш файла или запись аудита. Свободный текст не
принимается для критичных областей. Заявленное действие сверяется с аудитом.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .access import is_protected
from .errors import ArtekBrainError, ValidationError

TYPES = ("doc_version", "test_run", "file_hash", "audit_ref", "text")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_READ_CLAIM_RE = re.compile(
    r"(?:прочитал[аи]?|read)\s+([\w./-]+)", re.IGNORECASE
)
_RUN_CLAIM_RE = re.compile(
    r"(?:запустил[аи]?|запуск|ran|run)\s+([^\n,;]+)", re.IGNORECASE
)
_DEFAULT_CRITICAL = ("rules", "scoring", "hidden", "approved", "owner")


class EvidenceError(ArtekBrainError):
    """Доказательства не подтверждают заявленное."""

    def __init__(self, message: str, report: "EvidenceReport | None" = None) -> None:
        self.report = report
        super().__init__(
            message,
            hint=(
                "Приложите doc_version (path+version+sha256), test_run (id прогона), "
                "file_hash (путь+sha256) или audit_ref (id записи аудита)."
            ),
        )


@dataclass(frozen=True)
class EvidenceReport:
    """Итог проверки доказательств."""

    accepted: tuple[dict[str, Any], ...] = ()
    rejected: tuple[dict[str, Any], ...] = ()
    mismatches: tuple[dict[str, Any], ...] = ()
    critical: bool = False
    weak_only: bool = False

    @property
    def ok(self) -> bool:
        """Можно ли принять доказательства."""
        if self.rejected:
            return False
        if self.critical and self.weak_only:
            return False
        return bool(self.accepted)

    @property
    def has_mismatch(self) -> bool:
        """Есть ли расхождение заявленного и аудита."""
        return bool(self.mismatches)

    def as_dict(self) -> dict[str, Any]:
        """Представить отчёт словарём."""
        return {
            "accepted": list(self.accepted),
            "rejected": list(self.rejected),
            "mismatches": list(self.mismatches),
            "critical": self.critical,
            "weak_only": self.weak_only,
            "ok": self.ok,
            "has_mismatch": self.has_mismatch,
        }


def parse_evidence(value: Any) -> list[dict[str, Any]]:
    """Разобрать строку/JSON/список в список элементов доказательств."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text[:1] in "[{":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return [{"type": "text", "text": text}]
            return parse_evidence(parsed)
        return [{"type": "text", "text": text}]
    if isinstance(value, dict):
        if isinstance(value.get("items"), list):
            return parse_evidence(value["items"])
        if "type" in value or "kind" in value:
            item = dict(value)
            item["type"] = str(item.get("type") or item.get("kind")).strip().lower()
            return [item]
        if isinstance(value.get("evidence"), (str, list, dict)):
            return parse_evidence(value["evidence"])
        return [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]
    if isinstance(value, (list, tuple)):
        result: list[dict[str, Any]] = []
        for entry in value:
            result.extend(parse_evidence(entry))
        return result
    return [{"type": "text", "text": str(value)}]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class EvidenceChecker:
    """Сверяет доказательства с Store, реестром прогонов, файлами и аудитом."""

    def __init__(
        self,
        store: Any,
        *,
        runs: Any = None,
        repo_root: str | Path | None = None,
        critical_prefixes: Sequence[str] | None = None,
    ) -> None:
        self.store = store
        self.runs = runs
        self.repo_root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
        self.critical_prefixes = tuple(
            item.strip().strip("/")
            for item in (critical_prefixes or _DEFAULT_CRITICAL)
            if item.strip()
        )

    # ------------------------------------------------------------------
    # Критичность области
    # ------------------------------------------------------------------
    def is_critical(self, task: Any) -> bool:
        """Критична ли область задачи (правила, scoring, hidden, approved)."""
        areas = getattr(task, "area", None) or []
        for area in areas:
            cleaned = str(area).strip().strip("/")
            for prefix in self.critical_prefixes:
                if not prefix:
                    return True
                if cleaned == prefix or cleaned.startswith(prefix + "/"):
                    return True
                if prefix == cleaned or prefix.startswith(cleaned + "/"):
                    return True
        return False

    # ------------------------------------------------------------------
    # Проверка
    # ------------------------------------------------------------------
    def validate(
        self,
        actor: str,
        task: Any,
        evidence: Any,
        *,
        now: datetime | None = None,
    ) -> EvidenceReport:
        """Проверить доказательства и вернуть отчёт без исключения."""
        items = parse_evidence(evidence)
        critical = self.is_critical(task)
        window_start = (
            _parse_ts(getattr(task, "claimed_at", None))
            or _parse_ts(getattr(task, "created_at", None))
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        window_end = now or datetime.now(timezone.utc)
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        mismatches: list[dict[str, Any]] = []

        for index, item in enumerate(items):
            kind = str(item.get("type") or item.get("kind") or "").strip().lower()
            if kind not in TYPES:
                rejected.append(
                    {
                        "index": index,
                        "type": kind or None,
                        "reason": f"неизвестный тип доказательства «{kind}»",
                    }
                )
                continue
            if kind == "text":
                text = str(item.get("text") or "").strip()
                if not text:
                    rejected.append(
                        {"index": index, "type": kind, "reason": "пустой текст"}
                    )
                    continue
                if critical:
                    rejected.append(
                        {
                            "index": index,
                            "type": kind,
                            "reason": (
                                "свободный текст без проверяемой ссылки "
                                "не принимается для критичной области"
                            ),
                        }
                    )
                    continue
                accepted.append(
                    {"index": index, "type": kind, "text": text, "weak": True}
                )
                self._text_claims(
                    actor, text, window_start, window_end, mismatches, index
                )
                continue

            issue, fact = self._validate_typed(item, kind, actor)
            if issue is not None:
                rejected.append({"index": index, "type": kind, **issue})
                continue
            accepted.append({"index": index, "type": kind, **fact})
            self._check_claim(
                actor, item, fact, window_start, window_end, mismatches, index
            )

        weak_only = bool(accepted) and all(item.get("weak") for item in accepted)
        return EvidenceReport(
            accepted=tuple(accepted),
            rejected=tuple(rejected),
            mismatches=tuple(mismatches),
            critical=critical,
            weak_only=weak_only,
        )

    def _validate_typed(
        self, item: dict[str, Any], kind: str, actor: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        if kind == "doc_version":
            return self._doc_version(item)
        if kind == "test_run":
            return self._test_run(item, actor)
        if kind == "file_hash":
            return self._file_hash(item)
        if kind == "audit_ref":
            return self._audit_ref(item, actor)
        return {"reason": "неизвестный тип"}, {}

    def _doc_version(
        self, item: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        path = item.get("path")
        version = item.get("version")
        sha = item.get("sha256")
        try:
            path = self.store._normalize_path(path)
        except ValidationError as exc:
            return {"reason": str(exc)}, {}
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            return {"reason": "version должна быть целым числом >= 1"}, {}
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            return {"reason": "sha256 должен быть 64 hex-символа"}, {}
        with self.store._lock:
            self.store._ensure_open()
            row = self.store._conn.execute(
                "SELECT path, n, sha256, author FROM versions WHERE path=? AND n=?",
                (path, version),
            ).fetchone()
        if row is None:
            return {
                "reason": f"версия {version} документа «{path}» не существует"
            }, {}
        if row["sha256"] != sha:
            return {
                "reason": (
                    f"sha256 не совпадает с версией {version} документа «{path}»"
                )
            }, {}
        return None, {
            "path": path,
            "version": int(row["n"]),
            "sha256": row["sha256"],
            "author": row["author"],
        }

    def _test_run(
        self, item: dict[str, Any], actor: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        run_id = item.get("run_id")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
            return {"reason": "run_id должен быть целым числом >= 1"}, {}
        if self.runs is None:
            return {"reason": "реестр прогонов недоступен"}, {}
        run = self.runs.get(run_id)
        if run is None:
            return {"reason": f"прогон #{run_id} не найден"}, {}
        if run.actor != actor:
            return {
                "reason": f"прогон #{run_id} сделан другим актором «{run.actor}»"
            }, {}
        require_pass = item.get("require_pass", True)
        if require_pass and run.status != "pass":
            return {
                "reason": f"прогон #{run_id} не прошёл (статус {run.status})"
            }, {}
        return None, {
            "run_id": run.id,
            "status": run.status,
            "pass_rate": run.pass_rate,
            "code_sha": run.code_sha,
            "hidden_pass_rate": run.hidden_pass_rate,
        }

    def _file_hash(
        self, item: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        raw_path = item.get("path")
        sha = item.get("sha256")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return {"reason": "path обязателен"}, {}
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            return {"reason": "sha256 должен быть 64 hex-символа"}, {}
        relative = raw_path.strip().replace("\\", "/")
        if relative.startswith("/") or ".." in relative.split("/"):
            return {"reason": "path должен быть относительным и без .."}, {}
        target = (self.repo_root / relative).resolve()
        try:
            target.relative_to(self.repo_root)
        except ValueError:
            return {"reason": "path выходит за пределы репозитория"}, {}
        if not target.is_file():
            return {"reason": f"файл «{relative}» не найден"}, {}
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != sha:
            return {
                "reason": f"sha256 файла «{relative}» не совпадает"
            }, {}
        return None, {"path": relative, "sha256": digest}

    def _audit_ref(
        self, item: dict[str, Any], actor: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        audit_id = item.get("audit_id")
        if not isinstance(audit_id, int) or isinstance(audit_id, bool) or audit_id < 1:
            return {"reason": "audit_id должен быть целым числом >= 1"}, {}
        with self.store._lock:
            self.store._ensure_open()
            row = self.store._conn.execute(
                "SELECT * FROM audit WHERE id=?", (audit_id,)
            ).fetchone()
        if row is None:
            return {"reason": f"запись аудита #{audit_id} не найдена"}, {}
        if row["actor"] != actor:
            return {
                "reason": f"запись аудита #{audit_id} сделана другим актором"
            }, {}
        return None, {
            "audit_id": int(row["id"]),
            "action": row["action"],
            "path": row["path"],
            "ts": row["ts"],
        }

    # ------------------------------------------------------------------
    # Заявленное и факт
    # ------------------------------------------------------------------
    def _audit_exists(
        self,
        actor: str,
        action: str,
        path: str | None,
        start: datetime,
        end: datetime,
    ) -> bool:
        clauses = ["actor=?", "action=?", "ts>=?", "ts<=?"]
        params: list[Any] = [
            actor,
            action,
            start.astimezone(timezone.utc).isoformat(timespec="microseconds"),
            end.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        ]
        if path is not None:
            clauses.append("path=?")
            params.append(path)
        sql = "SELECT 1 FROM audit WHERE " + " AND ".join(clauses) + " LIMIT 1"
        with self.store._lock:
            self.store._ensure_open()
            return self.store._conn.execute(sql, params).fetchone() is not None

    def _check_claim(
        self,
        actor: str,
        item: dict[str, Any],
        fact: dict[str, Any],
        start: datetime,
        end: datetime,
        mismatches: list[dict[str, Any]],
        index: int,
    ) -> None:
        claim = item.get("claim")
        claims = item.get("claims")
        raw_claims: list[Any] = []
        if isinstance(claim, dict):
            raw_claims.append(claim)
        if isinstance(claims, list):
            raw_claims.extend(claims)
        for entry in raw_claims:
            if not isinstance(entry, dict):
                continue
            action = str(entry.get("action") or "").strip().lower()
            path = entry.get("path")
            if action in ("read", "write", "search", "diff", "history"):
                try:
                    normalized = (
                        self.store._normalize_path(path) if path is not None else None
                    )
                except ValidationError:
                    normalized = None
                if not self._audit_exists(actor, action, normalized, start, end):
                    mismatches.append(
                        {
                            "index": index,
                            "claim": action,
                            "path": path,
                            "reason": "в аудите нет такого действия актора за время задачи",
                        }
                    )
            elif action in ("run", "test_run"):
                run_id = entry.get("run_id") or fact.get("run_id")
                found = False
                if self.runs is not None:
                    if isinstance(run_id, int):
                        run = self.runs.get(run_id)
                        found = run is not None and run.actor == actor
                    else:
                        found = bool(self.runs.list(actor=actor, limit=100))
                if not found:
                    mismatches.append(
                        {
                            "index": index,
                            "claim": action,
                            "run_id": run_id,
                            "reason": "в реестре нет такого прогона актора",
                        }
                    )

    def _text_claims(
        self,
        actor: str,
        text: str,
        start: datetime,
        end: datetime,
        mismatches: list[dict[str, Any]],
        index: int,
    ) -> None:
        for match in _READ_CLAIM_RE.finditer(text):
            path = match.group(1).strip().rstrip(".,)")
            try:
                normalized = self.store._normalize_path(path)
            except ValidationError:
                continue
            if not self._audit_exists(actor, "read", normalized, start, end):
                mismatches.append(
                    {
                        "index": index,
                        "claim": "read",
                        "path": normalized,
                        "reason": "в аудите нет чтения этого пути актором",
                    }
                )
        for match in _RUN_CLAIM_RE.finditer(text):
            command = match.group(1).strip()
            if not command:
                continue
            found = False
            if self.runs is not None:
                for run in self.runs.list(actor=actor, limit=100):
                    if command.lower() in run.command.lower():
                        found = True
                        break
            if not found:
                mismatches.append(
                    {
                        "index": index,
                        "claim": "run",
                        "command": command,
                        "reason": "в реестре нет такого прогона актора",
                    }
                )
