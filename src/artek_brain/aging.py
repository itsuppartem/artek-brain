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
достать `review_by` и упоминания в заметках, без записи в журнал и с кэшем по sha256.
"""

from __future__ import annotations

import json
import re
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
_NOTE_CACHE: dict[str, str] = {}


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


def _note_text(store: Any, sha256: str) -> str:
    """Текст заметки с кэшем по sha256 (журнал чтений не засоряется)."""
    if sha256 not in _NOTE_CACHE:
        if len(_NOTE_CACHE) > 5000:
            _NOTE_CACHE.clear()
        try:
            _NOTE_CACHE[sha256] = store._read_blob(sha256)
        except Exception:  # noqa: BLE001 - битая заметка не должна ломать бриф
            _NOTE_CACHE[sha256] = ""
    return _NOTE_CACHE[sha256]


def _mentions(text: str, path: str) -> bool:
    """Заметка упоминает документ полным путём или именем файла."""
    if path in text:
        return True
    name = path.rsplit("/", 1)[-1]
    if "." not in name or name.lower() == "readme.md":
        return False
    return re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text) is not None


def _reviewed(store: Any) -> dict[tuple[str, int], str]:
    rows = store._conn.execute(
        "SELECT path, version, MAX(ts) AS ts FROM doc_reviews GROUP BY path, version"
    ).fetchall()
    return {(row["path"], int(row["version"])): row["ts"] for row in rows}


def _item(
    row: Any,
    settings: AgingSettings,
    moment: datetime,
    review_by: datetime | None,
    reviewed_at: str | None,
) -> dict[str, Any] | None:
    written = _parse(row["ts"])
    if written is None:
        return None
    checked = _parse(reviewed_at)
    changed = max(written, checked) if checked else written
    ttl = settings.ttl_for(row["path"])
    age = (moment - changed).total_seconds() / 86400
    if review_by is not None and not (checked and checked >= review_by):
        # Явный срок во frontmatter важнее TTL папки.
        overdue = moment > review_by
        overdue_days = max(0.0, (moment - review_by).total_seconds() / 86400)
    else:
        overdue = age > ttl
        overdue_days = max(0.0, age - ttl)
    return {
        "path": row["path"],
        "version": row["version"],
        "ts": row["ts"],
        "reviewed_at": reviewed_at,
        "age_days": round(age, 1),
        "ttl_days": ttl,
        "overdue": overdue,
        "overdue_days": round(overdue_days, 1),
        "review_by": review_by.isoformat() if review_by else None,
        "_changed": changed,
    }


def freshness(
    store: Any, *, now: datetime | None = None, with_notes: bool = True
) -> list[dict[str, Any]]:
    """Свежесть каждого живого документа по сроку.

    Для каждого пути: дата последней версии или подтверждения, возраст, срок,
    просрочка, сколько заметок (notes/) с упоминанием документа появилось после
    правки и сколько раз его читали. Сначала просроченные и самые читаемые.
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
        reviewed = _reviewed(store)
        review = {row["path"]: _review_by(store, row["sha256"]) for row in rows}
        notes: list[tuple[datetime, str]] = []
        if with_notes:
            note_rows = store._conn.execute(
                """
                SELECT first.ts AS ts, cur.sha256 AS sha256 FROM documents d
                JOIN versions first ON first.path=d.path AND first.n=1
                JOIN versions cur ON cur.path=d.path AND cur.n=d.current_version
                WHERE d.deleted=0 AND d.path LIKE 'notes/%'
                """
            ).fetchall()
            for row in note_rows:
                moment_note = _parse(row["ts"])
                if moment_note:
                    notes.append((moment_note, _note_text(store, row["sha256"])))
        read_rows = store._conn.execute(
            "SELECT path, COUNT(*) AS n FROM audit "
            "WHERE action='read' AND path IS NOT NULL GROUP BY path"
        ).fetchall()
    reads = {row["path"]: int(row["n"]) for row in read_rows}
    result: list[dict[str, Any]] = []
    for row in rows:
        path = row["path"]
        item = _item(
            row, settings, moment, review.get(path), reviewed.get((path, int(row["version"] or 0)))
        )
        if item is None:
            continue
        changed = item.pop("_changed")
        item["notes_after"] = (
            0
            if path.startswith("notes/")
            else sum(1 for ts, text in notes if ts > changed and _mentions(text, path))
        )
        item["reads"] = reads.get(path, 0)
        result.append(item)
    result.sort(
        key=lambda item: (not item["overdue"], -item["reads"], -item["age_days"])
    )
    return result


def freshness_for(
    store: Any, paths: list[str], *, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Свежесть только указанных документов: дёшево для чтения и поиска."""
    if not paths:
        return {}
    settings = load_aging(store.root)
    moment = now or datetime.now(timezone.utc)
    marks = ",".join("?" for _ in paths)
    with store._lock:
        store._ensure_open()
        rows = store._conn.execute(
            f"""
            SELECT d.path AS path, d.current_version AS version, v.ts AS ts,
                   v.sha256 AS sha256
            FROM documents d
            JOIN versions v ON v.path=d.path AND v.n=d.current_version
            WHERE d.deleted=0 AND d.path IN ({marks})
            """,  # noqa: S608 - в запросе только плейсхолдеры
            list(paths),
        ).fetchall()
        reviewed = _reviewed(store)
        review = {row["path"]: _review_by(store, row["sha256"]) for row in rows}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = _item(
            row, settings, moment, review.get(row["path"]),
            reviewed.get((row["path"], int(row["version"]))),
        )
        if item is not None:
            item.pop("_changed")
            result[row["path"]] = item
    return result


def lagging_docs(
    items: list[dict[str, Any]], *, min_notes: int = 3
) -> list[dict[str, Any]]:
    """Документы-ориентиры, после которых накопились заметки, а сами они не менялись.

    Сигнал «docs отстали от notes»: заметки, которые упоминают документ, вышли
    позже его последней правки. Заметки о другом не считаются.
    """
    return [
        item
        for item in items
        if not item["path"].startswith("notes/")
        and item["notes_after"] >= min_notes
        and item["age_days"] >= 1
    ]
