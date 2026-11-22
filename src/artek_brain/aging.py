"""Старение знаний: срок свежести документа без ручного frontmatter.

Агенты почти никогда не ставят `review_by`, поэтому свежесть считается по
дате последней версии и сроку (TTL) для папки или файла. Сроки задаются в
`settings.json` хранилища:

    {"aging": {"ttl_days": {"backlog.md": 7, "docs/prod.md": 7, "docs/": 30},
               "default_days": 60},
     "brief": {"paths": ["README.md", "backlog.md"]}}

Ключ, оканчивающийся на «/», — папка; остальные — точный путь или шаблон
fnmatch. Выигрывает самый длинный подходящий ключ. Если у документа есть
`review_by` во frontmatter, он важнее срока: так считают и бриф, и
quality.report.

Модуль читает метаданные версий и журнал. Содержимое открывается только чтобы
достать `review_by`, без записи в журнал и с кэшем по sha256.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

SETTINGS_FILE = "settings.json"

DEFAULT_TTL_DAYS: dict[str, int] = {
    "status.md": 3,
    "backlog.md": 7,
    "README.md": 14,
    "docs/prod.md": 7,
    "docs/": 30,
    "owner/": 60,
    "notes/": 90,
}
DEFAULT_DAYS = 60
DEFAULT_BRIEF_PATHS = ("README.md", "backlog.md")


@dataclass(frozen=True)
class AgingSettings:
    """Сроки свежести и пути ориентиров для брифа."""

    ttl_days: dict[str, int]
    default_days: int
    brief_paths: tuple[str, ...]

    def ttl_for(self, path: str) -> int:
        """Срок свежести пути в днях: самый длинный подходящий ключ."""
        best_key = ""
        best = self.default_days
        for key, days in self.ttl_days.items():
            if key.endswith("/"):
                hit = path.startswith(key)
            else:
                hit = path == key or fnmatchcase(path, key)
            if hit and len(key) > len(best_key):
                best_key, best = key, int(days)
        return best


def _raw_settings(root: str | Path) -> dict[str, Any]:
    target = Path(root) / SETTINGS_FILE
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_aging(root: str | Path) -> AgingSettings:
    """Прочитать блоки aging и brief из settings.json; ошибки = значения по умолчанию."""
    raw = _raw_settings(root)
    aging = raw.get("aging") if isinstance(raw.get("aging"), dict) else {}
    ttl = dict(DEFAULT_TTL_DAYS)
    custom = aging.get("ttl_days")
    if isinstance(custom, dict):
        for key, value in custom.items():
            try:
                ttl[str(key)] = max(1, int(value))
            except (TypeError, ValueError):
                continue
    try:
        default_days = max(1, int(aging.get("default_days", DEFAULT_DAYS)))
    except (TypeError, ValueError):
        default_days = DEFAULT_DAYS
    brief = raw.get("brief") if isinstance(raw.get("brief"), dict) else {}
    paths = brief.get("paths")
    if isinstance(paths, list) and all(isinstance(item, str) for item in paths) and paths:
        brief_paths = tuple(paths)
    else:
        brief_paths = DEFAULT_BRIEF_PATHS
    return AgingSettings(ttl, default_days, brief_paths)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


_REVIEW_CACHE: dict[str, datetime | None] = {}


def _review_by(store: Any, sha256: str | None) -> datetime | None:
    """Срок review_by из frontmatter версии; кэш по sha256, журнал не пишется."""
    if not sha256:
        return None
    if sha256 in _REVIEW_CACHE:
        return _REVIEW_CACHE[sha256]
    value: datetime | None = None
    try:
        text = store._read_blob(sha256)
        if text.lstrip("\ufeff").startswith("---"):
            from . import frontmatter
            from .knowledge import parse_date

            raw = frontmatter.parse(text).data.get("review_by")
            value = parse_date(raw if isinstance(raw, str) else (str(raw) if raw else None))
    except Exception:  # noqa: BLE001 - битый frontmatter не должен ломать бриф
        value = None
    if len(_REVIEW_CACHE) > 10000:
        _REVIEW_CACHE.clear()
    _REVIEW_CACHE[sha256] = value
    return value


def freshness(store: Any, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Свежесть каждого живого документа по сроку.

    Для каждого пути: дата последней версии, возраст, срок, просрочка,
    сколько заметок (notes/) появилось после последней правки и сколько
    раз документ читали. Отсортировано: сначала просроченные и самые
    читаемые.
    """
    settings = load_aging(store.root)
    moment = now or datetime.now(timezone.utc)
    with store._lock:
        store._ensure_open()
        rows = store._conn.execute(
            """
            SELECT d.path AS path, d.current_version AS version, v.ts AS ts,
                   v.sha256 AS sha256
            FROM documents d
            LEFT JOIN versions v ON v.path=d.path AND v.n=d.current_version
            WHERE d.deleted=0
            """
        ).fetchall()
        note_rows = store._conn.execute(
            """
            SELECT v.ts AS ts FROM versions v
            JOIN documents d ON d.path=v.path
            WHERE d.deleted=0 AND v.path LIKE 'notes/%' AND v.n=1
            """
        ).fetchall()
        review = {row["path"]: _review_by(store, row["sha256"]) for row in rows}
        read_rows = store._conn.execute(
            "SELECT path, COUNT(*) AS n FROM audit "
            "WHERE action='read' AND path IS NOT NULL GROUP BY path"
        ).fetchall()
    note_times = sorted(t for t in (_parse(row["ts"]) for row in note_rows) if t)
    reads = {row["path"]: int(row["n"]) for row in read_rows}
    result: list[dict[str, Any]] = []
    for row in rows:
        path = row["path"]
        changed = _parse(row["ts"])
        if changed is None:
            continue
        ttl = settings.ttl_for(path)
        age = (moment - changed).total_seconds() / 86400
        review_by = review.get(path)
        if review_by is not None:
            # Явный срок во frontmatter важнее TTL папки.
            overdue = moment > review_by
            overdue_days = max(0.0, (moment - review_by).total_seconds() / 86400)
        else:
            overdue = age > ttl
            overdue_days = max(0.0, age - ttl)
        notes_after = (
            0
            if path.startswith("notes/")
            else sum(1 for item in note_times if item > changed)
        )
        result.append(
            {
                "path": path,
                "version": row["version"],
                "ts": row["ts"],
                "age_days": round(age, 1),
                "ttl_days": ttl,
                "overdue": overdue,
                "overdue_days": round(overdue_days, 1),
                "review_by": review_by.isoformat() if review_by else None,
                "notes_after": notes_after,
                "reads": reads.get(path, 0),
            }
        )
    result.sort(
        key=lambda item: (not item["overdue"], -item["reads"], -item["age_days"])
    )
    return result


def lagging_docs(
    items: list[dict[str, Any]], *, min_notes: int = 3
) -> list[dict[str, Any]]:
    """Документы-ориентиры, после которых накопились заметки, а сами они не менялись.

    Сигнал «docs отстали от notes»: выводы оседают в notes/, а справочный
    документ остаётся прежним.
    """
    return [
        item
        for item in items
        if not item["path"].startswith("notes/")
        and item["notes_after"] >= min_notes
        and item["age_days"] >= 1
    ]
