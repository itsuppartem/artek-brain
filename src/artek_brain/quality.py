"""Качество знаний: дубли, противоречия, битые ссылки, схемы, метрики.

Проверки детерминированные, без LLM и сети. Документы читаются через фасад
Brain с проверкой прав токена.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from . import frontmatter
from .errors import ArtekBrainError, ValidationError
from .knowledge import (
    Document,
    as_list,
    in_prefix,
    iter_documents,
    jaccard,
    normalize_value,
    now_utc,
    parse_date,
    word_shingles,
)

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_TABLE_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_KEY_VALUE_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?([A-Za-zА-Яа-яЁё_][\w .-]{0,60}?)\s*[:=]\s*(.+?)\s*$"
)
_DEFAULT_THRESHOLD = 0.8
_DEFAULT_SHINGLE = 5
_DEFAULT_STRICT_PREFIXES = ("approved",)


@dataclass(frozen=True)
class DuplicatePair:
    """Пара похожих документов и их сходство."""

    path_a: str
    path_b: str
    score: float
    version_a: int
    version_b: int

    def as_dict(self) -> dict[str, Any]:
        """Представить пару словарём."""
        return {
            "path_a": self.path_a,
            "path_b": self.path_b,
            "score": round(self.score, 6),
            "version_a": self.version_a,
            "version_b": self.version_b,
        }


@dataclass(frozen=True)
class Contradiction:
    """Противоречие: ключ, два значения и места в документах."""

    kind: str
    key: str
    value_a: str
    value_b: str
    path_a: str
    path_b: str
    line_a: int | None = None
    line_b: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Представить противоречие словарём."""
        return {
            "kind": self.kind,
            "key": self.key,
            "value_a": self.value_a,
            "value_b": self.value_b,
            "path_a": self.path_a,
            "path_b": self.path_b,
            "line_a": self.line_a,
            "line_b": self.line_b,
        }


@dataclass(frozen=True)
class SchemaResult:
    """Результат проверки документа по схеме типа."""

    path: str
    doc_type: str | None
    required: tuple[str, ...]
    missing: tuple[str, ...]
    valid: bool

    def as_dict(self) -> dict[str, Any]:
        """Представить результат словарём."""
        return {
            "path": self.path,
            "type": self.doc_type,
            "required": list(self.required),
            "missing": list(self.missing),
            "valid": self.valid,
        }


def _approved(doc: Document) -> bool:
    return doc.status == "approved" or in_prefix(doc.path, "approved")


def _parse_facts(data: dict[str, Any]) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    for raw in as_list(data.get("facts")):
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            facts.append((key, value))
    return facts


def _find_line(text: str, key: str, value: str) -> int | None:
    needle_key = key.strip().lower()
    needle_value = value.strip().lower()
    for index, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        if needle_key in lowered and needle_value in lowered:
            return index
    return None


def _numbers_in(value: str) -> list[str]:
    result: list[str] = []
    for match in _NUMBER_RE.finditer(value):
        result.append(match.group(0).replace(",", "."))
    return result


def _table_pairs(doc: Document) -> list[tuple[str, str, int]]:
    """Пары ключ/значение из markdown-таблиц и строк key: value."""
    pairs: list[tuple[str, str, int]] = []
    lines = doc.text.splitlines()
    for offset, line in enumerate(lines[doc.body_start :]):
        number = doc.body_start + offset + 1
        table = _TABLE_RE.match(line)
        if table:
            cells = [cell.strip() for cell in table.group(1).split("|")]
            if len(cells) >= 2 and not set(cells[0]) <= {"-", ":", " "}:
                key, value = cells[0], cells[1]
                if key and _numbers_in(value):
                    pairs.append((key, value, number))
            continue
        match = _KEY_VALUE_RE.match(line)
        if match:
            key, value = match.group(1).strip(), match.group(2).strip()
            if len(key) >= 2 and _numbers_in(value):
                pairs.append((key, value, number))
    return pairs


class Quality:
    """Проверки качества знаний и метрики состояния памяти."""

    def __init__(
        self,
        brain: Any,
        *,
        token: str | None = None,
        threshold: float = _DEFAULT_THRESHOLD,
        shingle_size: int = _DEFAULT_SHINGLE,
        strict_prefixes: Iterable[str] = _DEFAULT_STRICT_PREFIXES,
    ) -> None:
        if not 0.0 <= float(threshold) <= 1.0:
            raise ValidationError(
                "threshold должен быть от 0 до 1.",
                hint="Например, threshold=0.8.",
            )
        if not isinstance(shingle_size, int) or isinstance(shingle_size, bool) or shingle_size < 1:
            raise ValidationError(
                "shingle_size должен быть целым числом >= 1.",
                hint="По умолчанию 5 слов.",
            )
        self.brain = brain
        self.token = token
        self.threshold = float(threshold)
        self.shingle_size = int(shingle_size)
        self.strict_prefixes = tuple(str(item).strip("/") for item in strict_prefixes)

    def _token(self, token: str | None) -> str:
        value = token or self.token
        if not value:
            raise ValidationError(
                "Не передан токен для проверки качества.",
                hint="Передайте token в Quality(...) или первым аргументом метода.",
            )
        return value

    # ------------------------------------------------------------------
    # Дубли
    # ------------------------------------------------------------------
    def duplicates(
        self,
        token: str | None = None,
        *,
        prefix: str = "",
        threshold: float | None = None,
        limit: int = 100,
    ) -> list[DuplicatePair]:
        """Пары документов с Jaccard по 5-граммам слов выше порога."""
        value = self.threshold if threshold is None else float(threshold)
        if not 0.0 <= value <= 1.0:
            raise ValidationError(
                "threshold должен быть от 0 до 1.",
                hint="Например, threshold=0.8.",
            )
        docs = iter_documents(self.brain, self._token(token), prefix)
        shingles = [
            (doc, word_shingles(doc.body, self.shingle_size)) for doc in docs
        ]
        pairs: list[DuplicatePair] = []
        for index, (left, left_set) in enumerate(shingles):
            for right, right_set in shingles[index + 1 :]:
                if not left_set or not right_set:
                    continue
                score = jaccard(left_set, right_set)
                if score >= value:
                    pairs.append(
                        DuplicatePair(
                            path_a=left.path,
                            path_b=right.path,
                            score=score,
                            version_a=left.version,
                            version_b=right.version,
                        )
                    )
        pairs.sort(key=lambda item: (-item.score, item.path_a, item.path_b))
        return pairs[: max(1, int(limit))]

    # ------------------------------------------------------------------
    # Противоречия
    # ------------------------------------------------------------------
    def contradictions(
        self,
        token: str | None = None,
        *,
        prefix: str = "",
        limit: int = 200,
    ) -> list[Contradiction]:
        """Найти явные (facts) и табличные числовые противоречия."""
        docs = iter_documents(self.brain, self._token(token), prefix)
        found: list[Contradiction] = []
        found.extend(self._facts_contradictions(docs))
        found.extend(self._table_contradictions(docs))
        found.sort(
            key=lambda item: (item.kind, item.key, item.path_a, item.path_b, item.line_a or 0)
        )
        return found[: max(1, int(limit))]

    def _facts_contradictions(self, docs: list[Document]) -> list[Contradiction]:
        groups: dict[str, list[tuple[Document, str]]] = {}
        for doc in docs:
            if not _approved(doc):
                continue
            for key, value in _parse_facts(doc.data):
                groups.setdefault(key.strip().lower(), []).append((doc, value))
        result: list[Contradiction] = []
        for key, entries in groups.items():
            for index, (left, left_value) in enumerate(entries):
                for right, right_value in entries[index + 1 :]:
                    if left.path == right.path:
                        continue
                    if normalize_value(left_value) == normalize_value(right_value):
                        continue
                    result.append(
                        Contradiction(
                            kind="facts",
                            key=key,
                            value_a=left_value,
                            value_b=right_value,
                            path_a=left.path,
                            path_b=right.path,
                            line_a=_find_line(left.text, key, left_value),
                            line_b=_find_line(right.text, key, right_value),
                        )
                    )
        return result

    def _table_contradictions(self, docs: list[Document]) -> list[Contradiction]:
        groups: dict[str, list[tuple[Document, str, int]]] = {}
        for doc in docs:
            for key, value, line in _table_pairs(doc):
                numbers = _numbers_in(value)
                if not numbers:
                    continue
                groups.setdefault(key.strip().lower(), []).append((doc, numbers[0], line))
        result: list[Contradiction] = []
        for key, entries in groups.items():
            if len({doc.path for doc, _, _ in entries}) < 2:
                continue
            for index, (left, left_value, left_line) in enumerate(entries):
                for right, right_value, right_line in entries[index + 1 :]:
                    if left.path == right.path:
                        continue
                    if normalize_value(left_value) == normalize_value(right_value):
                        continue
                    result.append(
                        Contradiction(
                            kind="table",
                            key=key,
                            value_a=left_value,
                            value_b=right_value,
                            path_a=left.path,
                            path_b=right.path,
                            line_a=left_line,
                            line_b=right_line,
                        )
                    )
        return result

    # ------------------------------------------------------------------
    # Битые ссылки
    # ------------------------------------------------------------------
    def broken_links(self, token: str | None = None, prefix: str = "") -> list[Any]:
        """Битые внутренние ссылки в доступных документах (через Nav)."""
        principal = self.brain.access.verify(self._token(token))
        links = self.brain.nav.broken_links(prefix)
        if principal.role == "owner":
            return links
        return [link for link in links if principal.can_read(link.source)]

    # ------------------------------------------------------------------
    # Схемы типов
    # ------------------------------------------------------------------
    def load_schemas(self, token: str | None = None) -> dict[str, tuple[str, ...]]:
        """Прочитать правила из rules/schemas.json или rules/schemas.md."""
        token = self._token(token)
        for path in ("rules/schemas.json", "rules/schemas.md"):
            try:
                read = self.brain.read(token, path)
            except ArtekBrainError:
                continue
            if path.endswith(".json"):
                parsed = self._parse_json_schemas(read.content, path)
            else:
                parsed = self._parse_markdown_schemas(read.content, path)
            if parsed:
                return parsed
        return {}

    @staticmethod
    def _parse_json_schemas(text: str, path: str) -> dict[str, tuple[str, ...]]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"Некорректный JSON схем в «{path}»: {exc.msg}.",
                hint="Ожидается объект {type: [поля]} или {type: {required: [...]}}.",
            ) from exc
        if isinstance(data, dict) and isinstance(data.get("types"), dict):
            data = data["types"]
        if not isinstance(data, dict):
            raise ValidationError(
                f"Схемы в «{path}» должны быть объектом.",
                hint="Например: {\"decision\": [\"owner\", \"status\"]}.",
            )
        result: dict[str, tuple[str, ...]] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                fields = value.get("required") or value.get("fields") or []
            else:
                fields = value
            if isinstance(fields, str):
                fields = [item.strip() for item in fields.split(",")]
            if isinstance(fields, list):
                result[str(key).strip().lower()] = tuple(
                    str(item).strip() for item in fields if str(item).strip()
                )
        return result

    @staticmethod
    def _parse_markdown_schemas(text: str, path: str) -> dict[str, tuple[str, ...]]:
        parsed = frontmatter.parse(text)
        result: dict[str, tuple[str, ...]] = {}
        for raw in parsed.body.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = re.sub(r"^[-*+]\s+", "", line)
            table = _TABLE_RE.match(line)
            if table:
                cells = [cell.strip() for cell in table.group(1).split("|")]
                if len(cells) >= 2 and cells[0] and not set(cells[0]) <= {"-", ":", " "}:
                    result[cells[0].lower()] = tuple(
                        item.strip() for item in cells[1].split(",") if item.strip()
                    )
                continue
            match = _KEY_VALUE_RE.match(line)
            if not match:
                continue
            key, value = match.group(1).strip().lower(), match.group(2).strip()
            fields = tuple(item.strip() for item in value.split(",") if item.strip())
            if key and fields:
                result[key] = fields
        return result

    def validate(
        self,
        token: str | None = None,
        doc: str | Document | dict[str, Any] | frontmatter.Frontmatter | None = None,
        *,
        content: str | None = None,
        schemas: dict[str, tuple[str, ...]] | None = None,
    ) -> SchemaResult:
        """Проверить документ по схеме его type."""
        if doc is None:
            doc = token
            token = None
        token = self._token(token)
        rules = self.load_schemas(token) if schemas is None else schemas
        path: str
        if isinstance(doc, Document):
            path = doc.path
            data = doc.data
        elif isinstance(doc, frontmatter.Frontmatter):
            path = "<frontmatter>"
            data = doc.data
        elif isinstance(doc, dict):
            path = "<dict>"
            data = doc
        elif isinstance(doc, str):
            path = doc
            if content is None:
                read = self.brain.read(token, doc)
                parsed = frontmatter.parse(read.content)
                data = parsed.data
            else:
                data = frontmatter.parse(content).data
        else:
            raise ValidationError(
                "validate ожидает путь, Document, Frontmatter или словарь.",
                hint="Передайте path документа или его данные.",
            )
        doc_type = str(data.get("type") or "").strip().lower() or None
        required = rules.get(doc_type or "", ())
        missing = tuple(field for field in required if not data.get(field))
        return SchemaResult(
            path=path,
            doc_type=doc_type,
            required=tuple(required),
            missing=missing,
            valid=not missing,
        )

    def _strict_path(self, path: str) -> bool:
        return any(in_prefix(path, prefix) for prefix in self.strict_prefixes)

    def strict_check(self, *args: Any, token: str | None = None) -> None:
        """Отказать в записи в строгую область, если схема не выполнена."""
        if len(args) == 3:
            token, path, content = args
        elif len(args) == 2:
            path, content = args
        else:
            raise ValidationError(
                "strict_check ожидает (path, content) или (token, path, content).",
                hint="Передайте содержимое документа для проверки.",
            )
        if not self._strict_path(path):
            return
        token = self._token(token)
        schemas = self.load_schemas(token)
        if not schemas:
            return
        result = self.validate(token, path, content=content, schemas=schemas)
        if not result.valid:
            raise ValidationError(
                f"Документ «{path}» не проходит схему типа «{result.doc_type}»: "
                f"нет полей {', '.join(result.missing)}.",
                hint=(
                    "Добавьте обязательные поля в frontmatter или запишите "
                    "документ вне approved/."
                ),
            )

    # ------------------------------------------------------------------
    # Метрики
    # ------------------------------------------------------------------
    def report(self, token: str | None = None, prefix: str = "") -> dict[str, Any]:
        """Свежесть, покрытие refs и число stale-документов."""
        from .review_queue import ReviewQueue

        docs = iter_documents(self.brain, self._token(token), prefix)
        total = len(docs)
        with_refs = sum(1 for doc in docs if as_list(doc.data.get("refs")))
        with_review_by = sum(1 for doc in docs if doc.data.get("review_by"))
        overdue = sum(
            1
            for doc in docs
            if (moment := parse_date(doc.data.get("review_by"))) is not None
            and moment < now_utc()
        )
        stale_paths = sorted(doc.path for doc in docs if doc.status == "stale")
        queue_open = ReviewQueue(
            self.brain.store, getattr(self.brain, "access", None)
        ).count_open()
        return {
            "documents": total,
            "with_refs": with_refs,
            "coverage": (with_refs / total) if total else 0.0,
            "with_review_by": with_review_by,
            "overdue": overdue,
            "freshness": (overdue / total) if total else 0.0,
            "freshness_overdue": (overdue / total) if total else 0.0,
            "fresh_ratio": (1.0 - overdue / total) if total else 1.0,
            "stale": len(stale_paths),
            "stale_paths": stale_paths,
            "review_queue_open": queue_open,
        }
