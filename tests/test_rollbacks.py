"""Точечные откаты: версия, автор за период, пачка."""

from __future__ import annotations

from artek_brain import Store


def test_rollback_author_restores_prior_content(store: Store) -> None:
    store.write("a.md", "# A\n\nbase\n", "owner", "v1")
    store.write("a.md", "# A\n\nalice-1\n", "alice", "v2", base_version=1)
    store.write("a.md", "# A\n\nalice-2\n", "alice", "v3", base_version=2)

    report = store.rollback_author(
        "alice", since=None, until=None, actor="admin", reason="откат ошибок"
    )
    assert len(report.rolled_back) == 1
    assert report.rolled_back[0].op == "rollback_author"
    assert store.read("a.md").content == "# A\n\nbase\n"
    assert report.skipped == []


def test_rollback_author_skips_when_other_wrote_after(store: Store) -> None:
    store.write("a.md", "base", "owner", "v1")
    store.write("a.md", "alice", "alice", "v2", base_version=1)
    store.write("a.md", "bob", "bob", "v3", base_version=2)

    report = store.rollback_author(
        "alice", None, None, actor="admin", reason="откат"
    )
    assert report.rolled_back == []
    assert len(report.skipped) == 1
    assert "другой автор" in report.skipped[0].reason
    assert store.read("a.md").content == "bob"


def test_rollback_author_skips_when_other_wrote_between(store: Store) -> None:
    store.write("a.md", "v1", "owner", "v1")
    store.write("a.md", "alice-1", "alice", "v2", base_version=1)
    store.write("a.md", "bob", "bob", "v3", base_version=2)
    store.write("a.md", "alice-2", "alice", "v4", base_version=3)

    report = store.rollback_author("alice", None, None, actor="admin", reason="откат")
    assert report.rolled_back == []
    assert report.skipped
    assert store.read("a.md").content == "alice-2"


def test_rollback_batch_restores_before_batch(store: Store) -> None:
    store.write("a.md", "base\n", "owner", "v1")
    store.write("a.md", "batch-1\n", "alice", "v2", base_version=1, batch_id="b-1")
    store.write("a.md", "batch-2\n", "alice", "v3", base_version=2, batch_id="b-1")

    report = store.rollback_batch("b-1", "admin", "откат пачки")
    assert len(report.rolled_back) == 1
    assert report.rolled_back[0].op == "rollback_batch"
    assert store.read("a.md").content == "base\n"


def test_rollback_batch_skips_when_newer_writes_exist(store: Store) -> None:
    store.write("a.md", "base\n", "owner", "v1")
    store.write("a.md", "batch\n", "alice", "v2", base_version=1, batch_id="b-2")
    store.write("a.md", "later\n", "bob", "v3", base_version=2)

    report = store.rollback_batch("b-2", "admin", "откат пачки")
    assert report.rolled_back == []
    assert report.skipped
    assert store.read("a.md").content == "later\n"


def test_rollback_batch_multiple_documents(store: Store) -> None:
    store.write("a.md", "a-base", "owner", "a1")
    store.write("b.md", "b-base", "owner", "b1")
    store.write("a.md", "a-batch", "alice", "a2", base_version=1, batch_id="b-3")
    store.write("b.md", "b-batch", "alice", "b2", base_version=1, batch_id="b-3")

    report = store.rollback_batch("b-3", "admin", "откат")
    assert {item.path for item in report.rolled_back} == {"a.md", "b.md"}
    assert store.read("a.md").content == "a-base"
    assert store.read("b.md").content == "b-base"
