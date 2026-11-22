"""Атомарность: ошибка посреди записи не оставляет полузапись."""

from __future__ import annotations

import pytest

from artek_brain import Store


def test_failed_insert_does_not_change_document(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.write("a.md", "# A\n\nbase\n", "alice", "v1")

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("сбой посреди записи")

    monkeypatch.setattr(store, "_insert_version", boom)
    with pytest.raises(RuntimeError):
        store.write("a.md", "# A\n\nnew\n", "bob", "v2", base_version=1)

    read = store.read("a.md", actor="checker")
    assert read.version == 1
    assert read.content == "# A\n\nbase\n"
    assert [item.n for item in store.history("a.md")] == [1]
    assert store.stats()["versions"] == 1

    removed = store.gc(actor="checker", min_age_seconds=0)
    assert removed.removed >= 1
    assert store.read("a.md").version == 1
