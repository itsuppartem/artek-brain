"""Сканер секретов: находит ключи и токены в тексте, не показывает их в ошибках."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .errors import SecretDetectedError


@dataclass(frozen=True)
class SecretFinding:
    """Найденный секрет: тип и номер строки (сам секрет не хранится)."""

    kind: str
    line: int
    start: int
    end: int


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}\b"),
    ),
    (
        "aws_secret_key",
        re.compile(
            r"(?i)\baws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?"
        ),
    ),
    (
        "openai_key",
        re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "github_token",
        re.compile(
            r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"
            r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
        ),
    ),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+\s+)*PRIVATE KEY-----"
            r"(?:[\s\S]+?-----END (?:[A-Z0-9]+\s+)*PRIVATE KEY-----)?"
        ),
    ),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"
        ),
    ),
    (
        "password",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token)"
            r"\s*[:=]\s*['\"]?[^\s'\"`]{3,}"
        ),
    ),
)


def scan(text: str) -> list[SecretFinding]:
    """Найти секреты в тексте. Возвращает список без самих секретов."""
    findings: list[SecretFinding] = []
    taken: list[tuple[int, int]] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < e and end > s for s, e in taken):
                continue
            line = text.count("\n", 0, start) + 1
            findings.append(SecretFinding(kind=kind, line=line, start=start, end=end))
            taken.append((start, end))
    findings.sort(key=lambda item: item.start)
    return findings


def assert_clean(text: str, path: str | None = None) -> None:
    """Отклонить текст с секретом, не раскрывая сам секрет."""
    findings = scan(text)
    if findings:
        first = findings[0]
        raise SecretDetectedError(first.kind, first.line, path=path)


def redact(text: str) -> str:
    """Заменить секреты маркерами (для журнала аудита и логов)."""
    findings = scan(text)
    if not findings:
        return text
    parts: list[str] = []
    cursor = 0
    for finding in findings:
        parts.append(text[cursor : finding.start])
        parts.append(f"[REDACTED:{finding.kind}]")
        cursor = finding.end
    parts.append(text[cursor:])
    return "".join(parts)


def sanitize(value: Any) -> Any:
    """Рекурсивно вычистить секреты из значения для JSON-журнала."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {sanitize(str(key)): sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item) for item in value]
    return value


def sanitize_json(value: Any) -> str:
    """Сериализовать значение в JSON без секретов."""
    return json.dumps(
        sanitize(value), ensure_ascii=False, sort_keys=True, default=str
    )


# Понятные псевдонимы для внешних вызовов.
scan_text = scan
redact_secrets = redact
clean_audit_detail = sanitize_json
