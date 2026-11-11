"""Инкрементальное обновление FTS-индекса по отпечатку хранилища."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from artek_brain import Search, Store


def _paths(page) -> list[tuple[str, int]]:
    return [(item.path, item.version) for item in page.items]


def test_reopen_does_not_write_index(tmp_path: Path) -> None:
    store = Store(tmp_path / "brain")
    store.write("docs/a.md", "# A\n\nhello\n", "alice", "создал")
    first = Search(store)
    assert first.search("hello").total == 1
    first.close()
    try:
        observer = sqlite3.connect(str(store.root / "search.sqlite3"))
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        second = Search(store)
        assert second.search("hello").total == 1
        second.close()
        after = observer.execute("PRAGMA data_version").fetchone()[0]
        observer.close()
        assert after == before
    finally:
        store.close()


def test_other_instance_write_is_caught_and_matches_full_rebuild(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "brain")
    try:
        store.write("docs/a.md", "# A\n\nпервый alpha\n", "alice", "v1")
        store.write("docs/b.md", "# B\n\nвторой alpha\n", "alice", "v1")
        search = Search(store)
        assert search.search("alpha").total == 2

        other = Store(tmp_path / "brain")
        try:
            other.write(
                "docs/a.md",
                "# A\n\nновый beta\n",
                "bob",
                "v2",
                base_version=1,
            )
            other.write("docs/c.md", "# C\n\nтретий gamma\n", "bob", "v1")
            other.delete("docs/b.md", "bob", "удалил")
            other_search = Search(other)
            other_search.close()
        finally:
            other.close()

        assert _paths(search.search("beta")) == [("docs/a.md", 2)]
        assert _paths(search.search("gamma")) == [("docs/c.md", 1)]
        assert search.search("alpha").total == 0
        assert search.search_history("alpha").total == 3

        full = Search(store, path=tmp_path / "full.sqlite3")
        try:
            for query in ("alpha", "beta", "gamma"):
                assert _paths(search.search(query)) == _paths(full.search(query))
                assert _paths(search.search_history(query)) == _paths(
                    full.search_history(query)
                )
        finally:
            full.close()

        other = Store(tmp_path / "brain")
        try:
            other.restore("docs/b.md", "bob", "вернул")
            other_search = Search(other)
            other_search.close()
        finally:
            other.close()
        assert search.search("alpha").total == 1
    finally:
        store.close()


def test_legacy_index_without_meta_rebuilds_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "brain")
    store.write("docs/a.md", "# A\n\nстарое слово\n", "alice", "создал")
    try:
        first = Search(store)
        assert first.search("старое").total == 1
        first.close()

        db = sqlite3.connect(str(store.root / "search.sqlite3"))
        db.execute("DROP TABLE meta")
        db.execute("DROP TABLE indexed_docs")
        db.commit()
        db.close()

        rebuilt = Search(store)
        assert rebuilt.search("старое").total == 1
        rebuilt.close()

        observer = sqlite3.connect(str(store.root / "search.sqlite3"))
        before = observer.execute("PRAGMA data_version").fetchone()[0]
        again = Search(store)
        assert again.search("старое").total == 1
        again.close()
        after = observer.execute("PRAGMA data_version").fetchone()[0]
        observer.close()
        assert after == before
    finally:
        store.close()


def test_corrupted_index_is_recreated(tmp_path: Path) -> None:
    store = Store(tmp_path / "brain")
    try:
        store.write("docs/a.md", "# A\n\nцелое слово\n", "alice", "создал")
        search = Search(store)
        assert search.search("целое").total == 1
        search.close()
        index = store.root / "search.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            target = index.with_name(index.name + suffix)
            if target.exists():
                target.unlink()
        index.write_bytes(b"not a sqlite database")
        restored = Search(store)
        assert restored.search("целое").total == 1
        restored.close()
    finally:
        store.close()
