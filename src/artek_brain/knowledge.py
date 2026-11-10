"""Общие помощники проверок качества знаний.

Модуль только читает документы через публичные методы Store/Nav и разбирает
frontmatter. Сети и LLM здесь нет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from . import frontmatter
from .errors import ArtekBrainError, FrontmatterError

_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class Document:
    """Снимок текущей версии документа для проверок качества."""

    path: str
    version: int
    text: str
    data: dict[str, Any]
    body: str
    body_start: int = 0
    error: str | None = None

    @property
    def status(self) -> str:
        """Нормализованный статус из frontmatter."""
        return str(self.data.get("status") or "").strip().lower()

    @property
    def doc_type(self) -> str:
        """Нормализованный тип из frontmatter."""
        return str(self.data.get("type") or "").strip().lower()

    def ref(self) -> str:
        """Ссылка на версию в очереди ревью."""
        return f"{self.path}@{self.version}"


def in_prefix(path: str, prefix: str) -> bool:
    """Попадает ли путь в префикс (точное совпадение или вложенность)."""
    if not prefix:
        return True
    return path == prefix or path.startswith(prefix + "/")


def body_start_line(text: str) -> int:
    """Номер первой строки тела (0-based), после закрывающего ---."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            return index + 1
    return 0


def parse_document(path: str, version: int, text: str) -> Document:
    """Разобрать текст документа, не падая на битом frontmatter."""
    try:
        parsed = frontmatter.parse(text)
    except FrontmatterError as exc:
        return Document(
            path=path,
            version=version,
            text=text,
            data={},
            body=text,
            body_start=0,
            error=str(exc),
        )
    return Document(
        path=path,
        version=version,
        text=text,
        data=parsed.data,
        body=parsed.body,
        body_start=body_start_line(text),
    )


def iter_documents(
    brain: Any,
    token: str,
    prefix: str = "",
) -> list[Document]:
    """Прочитать доступные документы с учётом прав токена."""
    principal = brain.access.verify(token)
    result: list[Document] = []
    for info in brain.nav._documents():
        path = info["path"]
        if not in_prefix(path, prefix):
            continue
        if principal.role != "owner" and not principal.can_read(path):
            continue
        try:
            read = brain.store.read(path, actor=principal.name)
        except ArtekBrainError:
            continue
        result.append(parse_document(path, read.version, read.content))
    return result


def as_list(value: Any) -> list[str]:
    """Привести поле frontmatter к списку строк."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def normalize_value(value: str) -> str:
    """Каноническое значение для сравнения фактов."""
    text = value.strip().strip("'\"").strip()
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return text.lower()
    if number.is_integer():
        return f"num:{int(number)}"
    return f"num:{number}"


def parse_date(value: Any) -> datetime | None:
    """Разобрать дату/время из frontmatter в UTC."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def now_utc() -> datetime:
    """Текущее время в UTC."""
    return datetime.now(timezone.utc)


def word_shingles(text: str, size: int = 5) -> set[tuple[str, ...]]:
    """Множество n-грамм слов (5-граммы по умолчанию)."""
    words = _WORD_RE.findall(text.lower())
    if not words:
        return set()
    if len(words) < size:
        return {tuple(words)}
    return {tuple(words[index : index + size]) for index in range(len(words) - size + 1)}


def jaccard(left: Iterable[Any], right: Iterable[Any]) -> float:
    """Коэффициент Жаккара для двух множеств."""
    first = set(left)
    second = set(right)
    union = first | second
    if not union:
        return 1.0
    return len(first & second) / len(union)
