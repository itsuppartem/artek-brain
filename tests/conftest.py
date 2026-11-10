"""Общие фикстуры тестов Artek Brain."""

from __future__ import annotations

from pathlib import Path

import pytest

from artek_brain import Quota, Store


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    """Чистое хранилище во временном каталоге."""
    instance = Store(tmp_path / "brain")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def small_quota() -> Quota:
    """Небольшие лимиты для проверки отказов."""
    return Quota(
        max_document_bytes=64,
        max_versions_per_document=3,
        max_writes_per_author_per_day=10,
    )
