"""Квоты хранилища: размер документа, число версий, записей на автора в сутки."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from .errors import QuotaError

MIB = 1024 * 1024


@dataclass(frozen=True)
class Quota:
    """Лимиты хранилища.

    По умолчанию: документ до 1 МиБ, до 10 000 версий на документ,
    до 100 000 записей автора в сутки (UTC).
    """

    max_document_bytes: int = MIB
    max_versions_per_document: int = 10_000
    max_writes_per_author_per_day: int = 100_000
    max_disk_bytes: int | None = None

    @classmethod
    def from_settings(cls, root: str | Path) -> "Quota":
        """Лимиты из блока ``quota`` в settings.json; ошибки = значения по умолчанию.

        Пример: ``{"quota": {"max_document_bytes": 2097152, "max_disk_bytes": null}}``.
        """
        try:
            raw: Any = json.loads((Path(root) / "settings.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return cls()
        block = raw.get("quota") if isinstance(raw, dict) else None
        if not isinstance(block, dict):
            return cls()
        values: dict[str, Any] = {}
        for item in fields(cls):
            if item.name not in block:
                continue
            value = block[item.name]
            if value is None and item.name == "max_disk_bytes":
                values[item.name] = None
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                values[item.name] = number
        return cls(**values)

    def check_document_size(self, size: int, path: str | None = None) -> None:
        """Проверить размер содержимого в байтах."""
        if size > self.max_document_bytes:
            raise QuotaError(
                "размер документа",
                self.max_document_bytes,
                size,
                path=path,
                hint="Сократите документ или разбейте его на несколько документов.",
            )

    def check_versions(self, used: int, path: str | None = None) -> None:
        """Проверить, можно ли добавить ещё одну версию документа."""
        if used >= self.max_versions_per_document:
            raise QuotaError(
                "версии документа",
                self.max_versions_per_document,
                used,
                path=path,
                hint="Удалите ненужные версии или увеличьте max_versions_per_document.",
            )

    def check_daily_writes(self, used: int, author: str | None = None) -> None:
        """Проверить суточный лимит записей автора."""
        if used >= self.max_writes_per_author_per_day:
            raise QuotaError(
                "записи автора за сутки",
                self.max_writes_per_author_per_day,
                used,
                path=author,
                hint="Подождите до следующих суток UTC или увеличьте лимит.",
            )

    def check_disk(self, used: int) -> None:
        """Проверить жёсткий лимит диска (если он задан)."""
        if self.max_disk_bytes is not None and used > self.max_disk_bytes:
            raise QuotaError(
                "диск",
                self.max_disk_bytes,
                used,
                hint=(
                    "Удалите лишние версии/блобы через gc(), включите архив "
                    "archive_old_versions() или увеличьте max_disk_bytes."
                ),
            )
