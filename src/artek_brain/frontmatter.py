"""Разбор и запись простого YAML-подобного frontmatter без внешних библиотек."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .errors import FrontmatterError

_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_./ @:+-]*$")
_STRING_KEYS = ("type", "status", "owner", "review_by")


@dataclass
class Frontmatter:
    """Разобранный frontmatter: данные, тело и признак наличия блока."""

    data: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    present: bool = True

    def get(self, key: str, default: Any = None) -> Any:
        """Значение поля или default."""
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __contains__(self, key: object) -> bool:
        return key in self.data

    def to_text(self) -> str:
        """Собрать текст обратно."""
        if not self.present and not self.data:
            return self.body
        return dump(self.data, self.body)


def parse(text: str) -> Frontmatter:
    """Разобрать текст с frontmatter. Без блока возвращает пустые данные."""
    if not isinstance(text, str):
        raise FrontmatterError("ожидается строка текста")
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return Frontmatter(data={}, body=text, present=False)
    end = _find_closing(lines)
    block = lines[1:end]
    body = "".join(lines[end + 1 :])
    data = _parse_block(block)
    return Frontmatter(data=data, body=body, present=True)


def parse_data(text: str) -> tuple[dict[str, Any], str]:
    """Вернуть пару (данные frontmatter, тело) без объекта Frontmatter."""
    parsed = parse(text)
    return parsed.data, parsed.body


def has_frontmatter(text: str) -> bool:
    """Есть ли блок frontmatter в начале текста."""
    return parse(text).present


def dump(data: dict[str, Any], body: str = "") -> str:
    """Собрать текст с frontmatter из словаря."""
    if not isinstance(data, dict):
        raise FrontmatterError("данные frontmatter должны быть словарём")
    _validate(data)
    lines = ["---"]
    for key, value in data.items():
        lines.append(f"{key}: {_format_value(value)}")
    lines.append("---")
    prefix = "\n".join(lines) + "\n"
    return prefix + body


def update_text(text: str, **changes: Any) -> str:
    """Заменить/добавить поля frontmatter в тексте."""
    parsed = parse(text)
    data = dict(parsed.data)
    data.update(changes)
    _validate(data)
    return dump(data, parsed.body)


def _find_closing(lines: list[str]) -> int:
    for index in range(1, len(lines)):
        marker = lines[index].strip()
        if marker in ("---", "..."):
            return index
    raise FrontmatterError(
        "блок открыт строкой ---, но не закрыт",
        hint="Добавьте закрывающую строку --- после полей frontmatter.",
    )


def _parse_block(block: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for offset, raw in enumerate(block, start=2):
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            raise FrontmatterError(
                "ожидается строка key: value, а не элемент списка",
                line=offset,
                hint="Списки записывайте в одну строку: tags: [a, b].",
            )
        if ":" not in line:
            raise FrontmatterError(
                "нет двоеточия после имени поля", line=offset
            )
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not _KEY_RE.match(key):
            raise FrontmatterError(
                f"недопустимое имя поля «{key}»", line=offset
            )
        if key in data:
            raise FrontmatterError(f"поле «{key}» повторяется", line=offset)
        data[key] = _parse_value(raw_value.strip(), offset)
    _validate(data)
    return data


def _parse_value(value: str, line: int) -> Any:
    if value == "":
        return ""
    if value.startswith("["):
        if not value.endswith("]"):
            raise FrontmatterError("список не закрыт символом ]", line=line)
        inner = value[1:-1].strip()
        if inner == "":
            return []
        items = _split_list(inner, line)
        return [_unquote(item.strip(), line) for item in items]
    return _unquote(value, line)


def _split_list(inner: str, line: int) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in inner:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            current.append(char)
        elif char == ",":
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    if quote:
        raise FrontmatterError("не закрыта кавычка в списке", line=line)
    items.append("".join(current))
    return items


def _unquote(value: str, line: int) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if value[0] == '"':
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise FrontmatterError(
                    f"некорректная строка в кавычках: {exc.msg}", line=line
                ) from exc
        return inner.replace("''", "'")
    if value[:1] in ("'", '"'):
        raise FrontmatterError("не закрыта кавычка", line=line)
    return value


def _validate(data: dict[str, Any]) -> None:
    for key in _STRING_KEYS:
        if key in data and not isinstance(data[key], str):
            raise FrontmatterError(
                f"поле «{key}» должно быть строкой",
                hint="Передайте значение без списка, например owner: ivan.",
            )
    if "tags" in data and not isinstance(data["tags"], list):
        raise FrontmatterError(
            "поле «tags» должно быть списком",
            hint="Запишите теги в квадратных скобках: tags: [api, docs].",
        )
    if "tags" in data:
        for item in data["tags"]:
            if not isinstance(item, str):
                raise FrontmatterError("элементы «tags» должны быть строками")


def _format_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_format_scalar(item) for item in value) + "]"
    return _format_scalar(value)


def _format_scalar(value: Any) -> str:
    if not isinstance(value, str):
        value = str(value)
    if value == "":
        return ""
    if _SAFE_VALUE_RE.match(value) and not value.startswith(("[", "{")) and ": " not in value:
        return value
    return json.dumps(value, ensure_ascii=False)


# Понятные псевдонимы для внешних вызовов.
parse_frontmatter = parse
dump_frontmatter = dump
split = parse_data
serialize = dump
