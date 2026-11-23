"""Гарантия обновления знаний: решения, зависимости, расхождения и задачи.

Всё здесь детерминировано, без LLM и сети:

* документ зависит от решения, если в его тексте есть ссылка с якорем вида
  ``docs/decisions.md#раздел``. Изменился раздел — зависимый документ получает
  задачу. Ссылка без якоря считается навигацией и задачу не создаёт;
* поиск расхождений без ссылок сравнивает утверждения из документов-решений с
  остальными по общим редким словам и ищет противоположную позицию («держим» и
  «выкатить») или другое число при той же формулировке. Это эвристика: она не
  понимает смысл, синонимы и перефразирование;
* задача не исчезает, пока документ не обновят или агент явно не подтвердит
  актуальность с причиной. Долго открытая задача уходит владельцу в inbox.

Задачи ничего не блокируют: запись и выкатка работают как обычно.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import NotFoundError, ValidationError

SETTINGS_FILE = "settings.json"

DEFAULT_DECISION_PATHS = ("decisions.md", "decisions/", "docs/decisions.md", "docs/decisions/")
DEFAULT_RULE_SOURCES = ("backlog.md", "status.md")
DEFAULT_EXCLUDE = ("notes/", "archive/", "proposals/", "owner/inbox/")
DEFAULT_ESCALATE_DAYS = 3

STALE = "stale"
DECISION_CHANGED = "decision_changed"
RULE_CONFLICT = "rule_conflict"

MIN_CONFIRM_REASON = 10
SYNC_INTERVAL_SECONDS = 300
MAX_CONFLICTS_PER_DOC = 5


def _match(path: str, key: str) -> bool:
    if key.endswith("/"):
        return path.startswith(key)
    return path == key or fnmatchcase(path, key)


@dataclass(frozen=True)
class UpkeepSettings:
    """Настройки решений и задач на обновление."""

    decision_paths: tuple[str, ...] = DEFAULT_DECISION_PATHS
    require_owner: bool = True
    rule_sources: tuple[str, ...] = DEFAULT_RULE_SOURCES
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE
    escalate_days: int = DEFAULT_ESCALATE_DAYS
    owner: str = "owner"

    def is_decision(self, path: str) -> bool:
        """Документ-решение по настройке decisions.paths."""
        return any(_match(path, key) for key in self.decision_paths)

    def is_rule_source(self, path: str) -> bool:
        """Источник правил для поиска расхождений."""
        return self.is_decision(path) or any(_match(path, key) for key in self.rule_sources)

    def is_excluded(self, path: str) -> bool:
        """Журнальные документы: предупреждение есть, постоянной задачи нет."""
        return any(_match(path, key) for key in self.exclude)


def _strings(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return default


def load_settings(root: str | Path) -> UpkeepSettings:
    """Прочитать блоки decisions и upkeep; ошибки дают значения по умолчанию."""
    try:
        raw = json.loads((Path(root) / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    decisions = raw.get("decisions") if isinstance(raw.get("decisions"), dict) else {}
    upkeep = raw.get("upkeep") if isinstance(raw.get("upkeep"), dict) else {}
    try:
        days = max(1, int(upkeep.get("escalate_days", DEFAULT_ESCALATE_DAYS)))
    except (TypeError, ValueError):
        days = DEFAULT_ESCALATE_DAYS
    required = decisions.get("require_owner", True)
    return UpkeepSettings(
        decision_paths=_strings(decisions.get("paths"), DEFAULT_DECISION_PATHS),
        # Отключает только явный false: опечатка не должна снимать защиту.
        require_owner=required is not False,
        rule_sources=_strings(upkeep.get("rule_sources"), DEFAULT_RULE_SOURCES),
        exclude=_strings(upkeep.get("exclude"), DEFAULT_EXCLUDE),
        escalate_days=days,
        owner=str(upkeep.get("owner") or "owner"),
    )


# ----------------------------------------------------------------------
# Разделы и ссылки
# ----------------------------------------------------------------------
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LINK_RE = re.compile(r"((?:\.{0,2}/)*[\w.\-/]*[\w\-]\.md)#([^\s)\]`'\"<>,;]+)")


def slugify(title: str) -> str:
    """Якорь заголовка в стиле GitHub, с кириллицей."""
    text = re.sub(r"[`*_~\[\]()]", "", title).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text).strip("-")


def split_sections(text: str) -> dict[str, tuple[str, str]]:
    """Разделы документа: якорь -> (заголовок, хеш тела)."""
    found: list[tuple[str, list[str]]] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
        heading = None if fenced else _HEADING_RE.match(line)
        if heading:
            found.append((heading.group(2).strip(), []))
        elif found:
            found[-1][1].append(line.rstrip())
    result: dict[str, tuple[str, str]] = {}
    seen: Counter[str] = Counter()
    for title, body in found:
        slug = slugify(title)
        if not slug:
            continue
        seen[slug] += 1
        key = slug if seen[slug] == 1 else f"{slug}-{seen[slug] - 1}"
        digest = hashlib.sha256("\n".join(body).strip().encode("utf-8")).hexdigest()
        result[key] = (title, digest)
    return result


def _resolve_doc(source: str, target: str, decisions: set[str]) -> str | None:
    clean = target.lstrip("./") if target.startswith("./") else target
    if clean in decisions:
        return clean
    parts: list[str] = list(PurePosixPath(source).parent.parts)
    for part in PurePosixPath(target).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    relative = "/".join(parts)
    if relative in decisions:
        return relative
    name = PurePosixPath(target).name
    same = [item for item in decisions if PurePosixPath(item).name == name]
    return same[0] if len(same) == 1 else None


def _resolve_anchor(anchor: str, slugs: set[str]) -> str | None:
    wanted = slugify(anchor.replace("%20", " "))
    if wanted in slugs:
        return wanted
    prefixed = [slug for slug in slugs if slug.startswith(wanted + "-")]
    return prefixed[0] if len(prefixed) == 1 else None


def decision_links(source: str, text: str, decisions: set[str]) -> set[tuple[str, str]]:
    """Зависимости документа: пары (документ-решение, якорь из ссылки)."""
    links: set[tuple[str, str]] = set()
    for target, anchor in _LINK_RE.findall(text):
        doc = _resolve_doc(source, target, decisions)
        if doc and doc != source:
            links.add((doc, anchor))
    return links


# ----------------------------------------------------------------------
# Детерминированный поиск расхождений в тексте
# ----------------------------------------------------------------------
_CANCELLED_RE = re.compile(
    r"отмен[её]н|устарел|больше не действ|заменено|superseded|deprecated|obsolete|~~",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[^\W_]+(?:[.\-][^\W_]+)*", re.UNICODE)
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+")
_GROUPED_NUMBER_RE = re.compile(r"(?<=\d)[  ](?=\d{3}\b)")
_PLAIN_NUMBER_RE = re.compile(r"^\d+$")
_DATE_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}|(?:0[1-9]|[12]\d|3[01]|[1-9])\.(?:0[1-9]|1[0-2])(?:\.\d{2,4})?)$"
)

_HOLD = (
    "держ", "отлож", "заморож", "приостанов", "запрещ", "запрет", "нельзя", "копим",
    "hold", "freez", "postpon", "defer", "forbid", "never", "block",
)
_GO_RE = re.compile(
    r"^(?:выкат(?:ить|ывать|ываем|ывай|им|и|ите)|выклад(?:ывать|ываем|ывай)|выложить"
    r"|деплоить|задеплоить|деплоим|катим|катить|релизим|разреш[её]н\w*|разрешить|можно"
    r"|deploy|ship|rollout|publish|allow|allowed)$"
)
_NEGATIONS = {"не", "нет", "not", "no", "dont", "don't"}
_STOP = {
    "и", "в", "во", "на", "с", "со", "по", "к", "ко", "у", "о", "об", "от", "до", "из", "за",
    "для", "при", "про", "над", "под", "это", "этот", "эта", "эти", "тот", "та", "те", "как",
    "что", "чтобы", "если", "или", "а", "но", "же", "ли", "бы", "уже", "ещё", "еще", "все",
    "всё", "его", "её", "их", "мы", "вы", "они", "он", "она", "оно", "так", "там", "тут",
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "be", "with",
    "this", "that", "it", "at", "by", "from", "as", "шаг", "step", "сейчас", "потом",
}


def _stem(token: str) -> str:
    if token[-1:].isdigit() or len(token) <= 4:
        return token
    if len(token) == 5:
        return token[:4]
    return token[: max(4, len(token) - 2)]


@dataclass(frozen=True)
class Statement:
    """Одно утверждение документа: тема, позиция и числа."""

    path: str
    line: int
    text: str
    topic: frozenset[str]
    stance: str | None
    numbers: tuple[int, ...]


def _stance(tokens: list[str]) -> str | None:
    hold = go = False
    for index, token in enumerate(tokens):
        negated = index > 0 and tokens[index - 1] in _NEGATIONS
        if token.startswith(_HOLD):
            if negated:
                go = True
            else:
                hold = True
        elif _GO_RE.match(token):
            if negated:
                hold = True
            else:
                go = True
    if hold:
        return "hold"
    return "go" if go else None


def extract_statements(path: str, text: str) -> list[Statement]:
    """Разобрать документ на утверждения; отменённые и код пропускаются."""
    result: list[Statement] = []
    fenced = False
    for number, raw in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(raw):
            fenced = not fenced
            continue
        line = raw.strip()
        if fenced or not line or line.startswith("#") or _CANCELLED_RE.search(line):
            continue
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line).replace("**", "").replace("`", "")
        for sentence in _SENTENCE_RE.split(_GROUPED_NUMBER_RE.sub("", line)):
            tokens = [item.lower() for item in _TOKEN_RE.findall(sentence)]
            if not 3 <= len(tokens) <= 45:
                continue
            stance = _stance(tokens)
            numbers = tuple(
                int(item) for item in tokens if _PLAIN_NUMBER_RE.match(item) and len(item) <= 12
            )
            topic = frozenset(
                _stem(item)
                for item in tokens
                if item not in _STOP
                and item not in _NEGATIONS
                and not item.startswith(_HOLD)
                and not _GO_RE.match(item)
                and not _PLAIN_NUMBER_RE.match(item)
                and not _DATE_RE.match(item)
                and (len(item) >= 3 or any(char.isdigit() for char in item))
            )
            if len(topic) >= 2:
                result.append(Statement(path, number, sentence.strip(), topic, stance, numbers))
    return result


def _is_specific(term: str) -> bool:
    return any(char.isdigit() for char in term) or len(term) >= 5


def find_rule_conflicts(
    documents: dict[str, str], settings: UpkeepSettings
) -> list[dict[str, Any]]:
    """Найти вероятные расхождения правил в документах без ссылок между ними."""
    parsed = {path: extract_statements(path, text) for path, text in documents.items()}
    frequency: Counter[str] = Counter()
    for items in parsed.values():
        frequency.update({term for item in items for term in item.topic})
    limit = max(3, int(len(parsed) * 0.2))
    common = {term for term, count in frequency.items() if count > limit}

    index: dict[str, list[Statement]] = defaultdict(list)
    for path, items in parsed.items():
        for item in items:
            for term in item.topic:
                index[term].append(item)

    found: dict[tuple[str, str], dict[str, Any]] = {}
    for path, items in parsed.items():
        if not settings.is_rule_source(path):
            continue
        for rule in items:
            if rule.stance is None and len(rule.numbers) != 1:
                continue
            overlap: Counter[Statement] = Counter()
            for term in rule.topic:
                for other in index.get(term, ()):
                    if other.path != rule.path:
                        overlap[other] += 1
            for other in overlap:
                if settings.is_decision(other.path) and not settings.is_decision(rule.path):
                    continue
                shared = rule.topic & other.topic
                rare = shared - common
                ratio = len(shared) / max(1, min(len(rule.topic), len(other.topic)))
                kind = None
                if (
                    rule.stance
                    and other.stance
                    and rule.stance != other.stance
                    and (
                        (len(shared) >= 2 and ratio >= 0.5
                         and any(_is_specific(term) for term in rare))
                        or (len(shared) >= 3 and ratio >= 0.75)
                    )
                ):
                    kind = "stance"
                elif (
                    settings.is_decision(rule.path)
                    and len(rule.numbers) == 1
                    and len(other.numbers) == 1
                    and rule.numbers != other.numbers
                    and rule.stance == other.stance
                    and len(shared) >= 3
                    and ratio >= 0.75
                    and rare
                ):
                    kind = "value"
                if kind is None:
                    continue
                digest = hashlib.sha256(
                    f"{rule.path}\0{rule.text}\0{other.text}".encode()
                ).hexdigest()[:24]
                found[(other.path, digest)] = {
                    "path": other.path,
                    "line": other.line,
                    "text": other.text,
                    "rule_path": rule.path,
                    "rule_line": rule.line,
                    "rule_text": rule.text,
                    "kind": kind,
                    "fingerprint": digest,
                    "shared": sorted(shared),
                }
    per_doc: Counter[str] = Counter()
    result: list[dict[str, Any]] = []
    for item in sorted(
        found.values(), key=lambda row: (row["path"], -len(row["shared"]), row["line"])
    ):
        if per_doc[item["path"]] >= MAX_CONFLICTS_PER_DOC:
            continue
        per_doc[item["path"]] += 1
        result.append(item)
    return result


# ----------------------------------------------------------------------
# Задачи
# ----------------------------------------------------------------------
def _format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


class Upkeep:
    """Задачи на обновление: создание, закрытие, подтверждение, эскалация."""

    def __init__(self, store: Any, coord: Any | None = None) -> None:
        self.store = store
        self.coord = coord

    @property
    def settings(self) -> UpkeepSettings:
        """Настройки читаются заново: файл могут поменять на ходу."""
        return load_settings(self.store.root)

    # ------------------------------------------------------------------
    # Состояние
    # ------------------------------------------------------------------
    def _state(self, key: str) -> str | None:
        row = self.store._conn.execute(
            "SELECT value FROM upkeep_state WHERE key=?", (key, )
        ).fetchone()
        return row["value"] if row else None

    def _set_state(self, key: str, value: str) -> None:
        self.store._conn.execute(
            "INSERT INTO upkeep_state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _current_docs(self) -> dict[str, Any]:
        rows = self.store._conn.execute(
            """
            SELECT d.path AS path, d.current_version AS version, v.ts AS ts, v.sha256 AS sha256
            FROM documents d JOIN versions v ON v.path=d.path AND v.n=d.current_version
            WHERE d.deleted=0
            """
        ).fetchall()
        return {row["path"]: row for row in rows}

    # ------------------------------------------------------------------
    # Синхронизация
    # ------------------------------------------------------------------
    def sync(
        self,
        *,
        force: bool = False,
        now: datetime | None = None,
        max_age: float = SYNC_INTERVAL_SECONDS,
    ) -> dict[str, Any]:
        """Привести задачи к текущему состоянию памяти. Идемпотентно."""
        from .aging import freshness

        moment = now or datetime.now(timezone.utc)
        settings = self.settings
        with self.store._lock:
            self.store._ensure_open()
            marker = self.store._conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS id FROM versions"
            ).fetchone()["id"]
            reviews = self.store._conn.execute(
                "SELECT COUNT(*) AS n FROM doc_reviews"
            ).fetchone()["n"]
            signature = f"{marker}:{reviews}:{self._settings_key(settings)}"
            changed = self._state("signature") != signature
            last = float(self._state("synced_at") or 0)
            if not force and not changed and time.time() - last < max_age:
                return {"skipped": True}
            docs = self._current_docs()
            texts: dict[str, str] = {}
            if force or changed:
                for path, row in docs.items():
                    try:
                        texts[path] = self.store._read_blob(row["sha256"])
                    except Exception:  # noqa: BLE001 - один битый блоб не останавливает проверку
                        continue
        overdue = {
            item["path"]: item
            for item in freshness(self.store, now=moment, with_notes=False)
            if item["overdue"]
        }
        conflicts = None
        if force or changed:
            considered = {
                path: text
                for path, text in texts.items()
                if not settings.is_excluded(path)
            }
            conflicts = find_rule_conflicts(considered, settings)
        created = closed = 0
        now_text = _format_ts(moment)
        with self.store._lock, self.store._conn:
            if force or changed:
                created += self._sync_decisions(docs, texts, settings, now_text)
                for item in conflicts or []:
                    row = docs.get(item["path"])
                    if row is None:
                        continue
                    created += self._open(
                        item["path"], RULE_CONFLICT, item["fingerprint"],
                        (
                            f"Возможное расхождение с {item['rule_path']}:{item['rule_line']}: "
                            f"«{_clip(item['rule_text'], 90)}» и строка {item['line']}: "
                            f"«{_clip(item['text'], 90)}»"
                        ),
                        int(row["version"]), now_text,
                    )
            for path, item in overdue.items():
                row = docs.get(path)
                if row is None or settings.is_excluded(path):
                    continue
                created += self._open(
                    path, STALE, f"v{row['version']}:{item.get('reviewed_at') or ''}",
                    f"Срок свежести прошёл {item['overdue_days']} дн. назад",
                    int(row["version"]), now_text,
                )
            closed = self._auto_close(docs, overdue, conflicts, now_text)
            escalated = self._escalate(settings, moment, now_text)
            self._set_state("signature", signature)
            self._set_state("synced_at", str(time.time()))
        return {"skipped": False, "created": created, "closed": closed, "escalated": escalated}

    @staticmethod
    def _settings_key(settings: UpkeepSettings) -> str:
        raw = repr((settings.decision_paths, settings.rule_sources, settings.exclude))
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def _open(
        self, path: str, kind: str, fingerprint: str, detail: str, version: int, now: str
    ) -> int:
        cursor = self.store._conn.execute(
            """
            INSERT OR IGNORE INTO upkeep_tasks
                (path, kind, fingerprint, detail, doc_version, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (path, kind, fingerprint, detail, version, now, now),
        )
        return int(cursor.rowcount > 0)

    def _sync_decisions(
        self, docs: dict[str, Any], texts: dict[str, str], settings: UpkeepSettings, now: str
    ) -> int:
        decisions = {path for path in docs if settings.is_decision(path)}
        created = 0
        changed: dict[str, dict[str, str]] = {}
        known: dict[str, set[str]] = {}
        for path in sorted(decisions):
            if path not in texts:
                continue
            rows = self.store._conn.execute(
                "SELECT slug, hash FROM decision_sections WHERE path=?", (path,)
            ).fetchall()
            old = {row["slug"]: row["hash"] for row in rows}
            current = {slug: item[1] for slug, item in split_sections(texts[path]).items()}
            known[path] = set(old) | set(current)
            if old:
                diff = {
                    slug: current.get(slug, "deleted")
                    for slug in set(old) | set(current)
                    if slug in old and old.get(slug) != current.get(slug)
                }
                if diff:
                    changed[path] = diff
            self.store._conn.execute("DELETE FROM decision_sections WHERE path=?", (path,))
            self.store._conn.executemany(
                "INSERT INTO decision_sections(path, slug, hash, version) VALUES (?, ?, ?, ?)",
                [(path, slug, digest, int(docs[path]["version"])) for slug, digest in current.items()],
            )
        if not changed:
            return 0
        for source, text in texts.items():
            if source in decisions and not settings.is_decision(source):
                continue
            for doc, anchor in decision_links(source, text, decisions):
                if doc not in changed:
                    continue
                slug = _resolve_anchor(anchor, known.get(doc, set()))
                if slug is None or slug not in changed[doc]:
                    continue
                source_ts = _parse_ts(docs[source]["ts"])
                decision_ts = _parse_ts(docs[doc]["ts"])
                if source_ts and decision_ts and source_ts > decision_ts:
                    continue
                digest = changed[doc][slug]
                state = "раздел удалён" if digest == "deleted" else "раздел изменён"
                created += self._open(
                    source, DECISION_CHANGED, f"{doc}#{slug}@{digest[:16]}",
                    f"Решение изменилось: {doc}#{slug} (v{docs[doc]['version']}, {state})",
                    int(docs[source]["version"]), now,
                )
        return created

    def _auto_close(
        self,
        docs: dict[str, Any],
        overdue: dict[str, Any],
        conflicts: list[dict[str, Any]] | None,
        now: str,
    ) -> int:
        rows = self.store._conn.execute(
            "SELECT * FROM upkeep_tasks WHERE status='open'"
        ).fetchall()
        active = (
            {(item["path"], item["fingerprint"]) for item in conflicts}
            if conflicts is not None
            else None
        )
        closed = 0
        for row in rows:
            current = docs.get(row["path"])
            resolution = None
            if current is None:
                resolution = "deleted"
            elif row["kind"] in (STALE, DECISION_CHANGED) and int(current["version"]) > int(
                row["doc_version"]
            ):
                resolution = "updated"
            elif row["kind"] == STALE and row["path"] not in overdue:
                resolution = "no_longer_overdue"
            elif (
                row["kind"] == RULE_CONFLICT
                and active is not None
                and (row["path"], row["fingerprint"]) not in active
            ):
                resolution = "no_longer_found"
            if resolution:
                self.store._conn.execute(
                    "UPDATE upkeep_tasks SET status='resolved', resolution=?, resolved_by='system',"
                    " updated_at=? WHERE id=?",
                    (resolution, now, row["id"]),
                )
                closed += 1
        return closed

    def _escalate(self, settings: UpkeepSettings, moment: datetime, now: str) -> int:
        cutoff = _format_ts(moment - timedelta(days=settings.escalate_days))
        rows = self.store._conn.execute(
            "SELECT * FROM upkeep_tasks WHERE status='open' AND escalated_at IS NULL "
            "AND created_at<=? ORDER BY created_at, id",
            (cutoff,),
        ).fetchall()
        if not rows:
            return 0
        lines = [
            f"- #{row['id']} {row['path']} ({row['kind']}): {_clip(row['detail'], 140)}"
            for row in rows[:30]
        ]
        if len(rows) > 30:
            lines.append(f"- … ещё {len(rows) - 30}")
        lines.append(
            "Обновите документ или подтвердите актуальность: "
            "brain upkeep confirm <путь> --reason \"…\"."
        )
        self.store._conn.execute(
            "INSERT INTO messages (sender, recipient, subject, body, ts, read, read_at) "
            "VALUES ('artek-brain', ?, ?, ?, ?, 0, NULL)",
            (
                settings.owner,
                f"Задачи на обновление без реакции {settings.escalate_days}+ дн.: {len(rows)}",
                "\n".join(lines),
                now,
            ),
        )
        self.store._conn.executemany(
            "UPDATE upkeep_tasks SET escalated_at=? WHERE id=?",
            [(now, row["id"]) for row in rows],
        )
        self.store._audit_ok(
            "artek-brain", "upkeep_escalate", None,
            {"tasks": [int(row["id"]) for row in rows[:100]], "to": settings.owner},
        )
        return len(rows)

    # ------------------------------------------------------------------
    # Чтение задач и предупреждения
    # ------------------------------------------------------------------
    @staticmethod
    def _task(row: Any) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "path": row["path"],
            "kind": row["kind"],
            "detail": row["detail"],
            "status": row["status"],
            "doc_version": int(row["doc_version"]),
            "created_at": row["created_at"],
            "escalated": bool(row["escalated_at"]),
            "resolution": row["resolution"],
            "resolved_by": row["resolved_by"],
            "note": row["note"],
        }

    def tasks(
        self, *, path: str | None = None, status: str = "open", limit: int = 100
    ) -> list[dict[str, Any]]:
        """Список задач: open, resolved, confirmed или all."""
        where = []
        params: list[Any] = []
        if status != "all":
            where.append("status=?")
            params.append(status)
        if path:
            where.append("path=?")
            params.append(path)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self.store._lock:
            self.store._ensure_open()
            rows = self.store._conn.execute(
                f"SELECT * FROM upkeep_tasks {clause} ORDER BY created_at, id LIMIT ?",  # noqa: S608
                (*params, max(1, int(limit))),
            ).fetchall()
        return [self._task(row) for row in rows]

    def warning_for(self, path: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Предупреждение для ответа на чтение; None, если всё в порядке."""
        from .aging import freshness_for

        item = freshness_for(self.store, [path], now=now).get(path)
        open_tasks = self.tasks(path=path, limit=3)
        overdue = bool(item and item["overdue"])
        if not overdue and not open_tasks:
            return None
        parts = []
        if overdue:
            parts.append(
                f"Документ устарел: правка {item['age_days']:.0f} дн. назад, "
                f"срок {item['ttl_days']} дн."
            )
        parts.extend(_clip(task["detail"], 180) for task in open_tasks)
        return {
            "status": "stale" if overdue else "needs_review",
            "warning": " ".join(parts) + " Сверь факты, прежде чем опираться.",
            "age_days": item["age_days"] if item else None,
            "ttl_days": item["ttl_days"] if item else None,
            "tasks": [
                {"id": task["id"], "kind": task["kind"], "detail": _clip(task["detail"], 180)}
                for task in open_tasks
            ],
            "fix": (
                "Обнови документ через brain_write или подтверди: "
                f'brain_op(action="confirm_fresh", path="{path}", reason="что сверил").'
            ),
        }

    def warnings_for(self, paths: list[str]) -> dict[str, str]:
        """Короткие предупреждения для результатов поиска."""
        from .aging import freshness_for

        unique = sorted(set(paths))
        if not unique:
            return {}
        fresh = freshness_for(self.store, unique)
        marks = ",".join("?" for _ in unique)
        with self.store._lock:
            self.store._ensure_open()
            rows = self.store._conn.execute(
                f"SELECT path, COUNT(*) AS n FROM upkeep_tasks WHERE status='open' "  # noqa: S608
                f"AND path IN ({marks}) GROUP BY path",
                unique,
            ).fetchall()
        tasks = {row["path"]: int(row["n"]) for row in rows}
        result: dict[str, str] = {}
        for path in unique:
            item = fresh.get(path)
            parts = []
            if item and item["overdue"]:
                parts.append(f"устарел: {item['age_days']:.0f} дн., срок {item['ttl_days']}")
            if tasks.get(path):
                parts.append(f"открытых задач: {tasks[path]}")
            if parts:
                result[path] = "; ".join(parts)
        return result

    def brief_lines(self, limit: int = 5) -> list[str]:
        """Строки брифа про открытые задачи."""
        with self.store._lock:
            self.store._ensure_open()
            total = self.store._conn.execute(
                "SELECT COUNT(*) AS n FROM upkeep_tasks WHERE status='open'"
            ).fetchone()["n"]
            rows = self.store._conn.execute(
                """
                SELECT * FROM upkeep_tasks WHERE status='open'
                ORDER BY CASE kind WHEN 'decision_changed' THEN 0
                                   WHEN 'rule_conflict' THEN 1 ELSE 2 END, created_at, id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        if not total:
            return []
        lines = [f"Задачи на обновление памяти: {total}"]
        lines.extend(
            f"- #{row['id']} {row['path']}: {_clip(row['detail'], 110)}" for row in rows
        )
        if total > len(rows):
            lines.append(f"- … ещё {total - len(rows)}: brain_op(action=\"upkeep\")")
        lines.append(
            "Закрой: обнови документ или brain_op(action=\"confirm_fresh\", path=…, reason=…)."
        )
        return lines

    # ------------------------------------------------------------------
    # Подтверждение
    # ------------------------------------------------------------------
    def confirm(
        self,
        actor: str,
        path: str,
        reason: str,
        *,
        task_id: int | None = None,
        session: str | None = None,
        model: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Явно подтвердить актуальность документа с причиной."""
        clean = str(reason or "").strip()
        if len(clean) < MIN_CONFIRM_REASON:
            raise ValidationError(
                "Нужна причина подтверждения.",
                hint=f"Напишите, что сверили (не короче {MIN_CONFIRM_REASON} символов).",
            )
        moment = _format_ts(now or datetime.now(timezone.utc))
        with self.store._lock, self.store._conn:
            self.store._ensure_open()
            doc = self.store._get_doc_locked(path)
            if doc is None or doc["deleted"]:
                raise NotFoundError(f"Документ «{path}» не найден.")
            version = int(doc["current_version"])
            sql = "SELECT id FROM upkeep_tasks WHERE status='open' AND path=?"
            params: list[Any] = [path]
            if task_id is not None:
                sql += " AND id=?"
                params.append(int(task_id))
            ids = [int(row["id"]) for row in self.store._conn.execute(sql, params).fetchall()]
            if task_id is not None and not ids:
                raise NotFoundError(f"Открытой задачи #{task_id} у «{path}» нет.")
            self.store._conn.executemany(
                "UPDATE upkeep_tasks SET status='confirmed', resolution='confirmed', "
                "resolved_by=?, note=?, updated_at=? WHERE id=?",
                [(actor, clean, moment, item) for item in ids],
            )
            if task_id is None:
                self.store._conn.execute(
                    "INSERT INTO doc_reviews(path, version, ts, actor, reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (path, version, moment, actor, clean),
                )
            self.store._audit_ok(
                actor, "confirm_fresh", path,
                {"version": version, "tasks": ids, "reason": clean[:300]}, session, model,
            )
        return {"path": path, "version": version, "confirmed_tasks": ids, "reason": clean}
