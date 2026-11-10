"""Квоты: размер, версии, суточные записи и gc."""

from __future__ import annotations

from pathlib import Path

import pytest

from artek_brain import Quota, QuotaError, Store


def test_document_size_quota(tmp_path: Path) -> None:
    store = Store(tmp_path / "brain", quota=Quota(max_document_bytes=10))
    try:
        with pytest.raises(QuotaError) as error:
            store.write("a.md", "x" * 11, "alice", "слишком большой")
        assert error.value.kind == "размер документа"
        assert error.value.limit == 10
        assert store.stats()["versions"] == 0
    finally:
        store.close()


def test_versions_quota(tmp_path: Path) -> None:
    store = Store(
        tmp_path / "brain",
        quota=Quota(max_versions_per_document=2),
    )
    try:
        store.write("a.md", "v1", "alice", "v1")
        store.write("a.md", "v2", "alice", "v2", base_version=1)
        with pytest.raises(QuotaError) as error:
            store.write("a.md", "v3", "alice", "v3", base_version=2)
        assert error.value.kind == "версии документа"
        assert store.read("a.md").content == "v2"
        assert len(store.history("a.md")) == 2
    finally:
        store.close()


def test_daily_writes_quota(tmp_path: Path) -> None:
    store = Store(
        tmp_path / "brain",
        quota=Quota(max_writes_per_author_per_day=2),
    )
    try:
        store.write("a.md", "v1", "alice", "v1")
        store.write("b.md", "v1", "alice", "v1")
        with pytest.raises(QuotaError) as error:
            store.write("c.md", "v1", "alice", "v1")
        assert error.value.kind == "записи автора за сутки"
        assert error.value.used == 2
    finally:
        store.close()


def test_quota_error_is_audited(store: Store) -> None:
    store.quota = Quota(max_document_bytes=2)
    with pytest.raises(QuotaError):
        store.write("a.md", "длинно", "alice", "не влезло")
    rows = store.audit(limit=5)
    assert rows[0].detail["ok"] is False
    assert rows[0].detail["error"] == "QuotaError"
