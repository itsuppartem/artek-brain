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


@pytest.fixture()
def access(tmp_path: Path):
    """Отдельное хранилище токенов во временном каталоге."""
    from artek_brain import Access

    instance = Access(tmp_path / "access")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def brain(store: Store, access):
    """Фасад Brain над чистым Store и Access."""
    from artek_brain import Brain

    instance = Brain(store, access)
    try:
        yield instance
    finally:
        instance.close()


def pytest_collection_modifyitems(config, items):  # noqa: ANN001
    """Тесты песочницы запускают systemd-run; на рабочей машине это окна
    с паролем (polkit). По умолчанию пропускаем, включение: BRAIN_SANDBOX_TESTS=1."""
    import os

    import pytest

    if os.environ.get("BRAIN_SANDBOX_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="песочница: BRAIN_SANDBOX_TESTS=1 для запуска")
    for item in items:
        if item.fspath.basename.startswith("test_sandbox"):
            item.add_marker(skip)
