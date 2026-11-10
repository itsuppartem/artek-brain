"""Базовые операции хранилища: версии, конфликты, склейка, чтение, blame."""

from __future__ import annotations

from pathlib import Path

import pytest

from artek_brain import (
    BaseVersionRequiredError,
    ConflictError,
    DeletedError,
    InvalidVersionError,
    NotFoundError,
    Store,
)


def test_create_read_history_and_stats(store: Store) -> None:
    result = store.write("docs/a.md", "# A\n\nhello\n", "alice", "создал")
    assert result.version == 1
    assert result.op == "create"
    assert result.created is True

    read = store.read("docs/a.md", actor="bob")
    assert read.content == "# A\n\nhello\n"
    assert read.version == 1
    assert read.truncated is False

    history = store.history("docs/a.md")
    assert [item.n for item in history] == [1]
    assert history[0].author == "alice"

    stats = store.stats()
    assert stats["documents"] == 1
    assert stats["versions"] == 1
    assert stats["reads"] == 1
    assert stats["top_authors"] == [{"author": "alice", "versions": 1}]


def test_existing_document_requires_base_version(store: Store) -> None:
    store.write("a.md", "text", "alice", "создал")
    with pytest.raises(BaseVersionRequiredError) as error:
        store.write("a.md", "text 2", "bob", "правка")
    assert "base_version" in str(error.value)


def test_new_document_rejects_base_version(store: Store) -> None:
    with pytest.raises(InvalidVersionError):
        store.write("a.md", "text", "alice", "создал", base_version=1)


def test_conflict_in_same_section(store: Store) -> None:
    original = "# A\n\nA0\n\n# B\n\nB0\n"
    store.write("doc.md", original, "alice", "v1")
    store.write("doc.md", "# A\n\nA1\n\n# B\n\nB0\n", "alice", "v2", base_version=1)

    with pytest.raises(ConflictError) as error:
        store.write(
            "doc.md",
            "# A\n\nA2\n\n# B\n\nB0\n",
            "bob",
            "конфликт",
            base_version=1,
        )
    conflict = error.value
    assert conflict.current_version == 2
    assert "A" in conflict.conflicting_sections
    assert "-" in conflict.diff and "+" in conflict.diff


def test_auto_merge_different_sections(store: Store) -> None:
    original = "# A\n\nA0\n\n# B\n\nB0\n"
    store.write("doc.md", original, "alice", "v1")
    store.write("doc.md", "# A\n\nA1\n\n# B\n\nB0\n", "alice", "правка A", base_version=1)
    merged = store.write(
        "doc.md",
        "# A\n\nA0\n\n# B\n\nB1\n",
        "bob",
        "правка B",
        base_version=1,
    )

    assert merged.op == "merge"
    assert merged.merged is True
    assert merged.version == 3
    assert merged.parent_version == 2
    text = store.read("doc.md").content
    assert "A1" in text and "B1" in text
    assert "A0" not in text and "B0" not in text


def test_expected_sha256_mismatch_is_conflict(store: Store) -> None:
    store.write("a.md", "v1", "alice", "v1")
    old = store.read("a.md").content
    store.write("a.md", "v2", "alice", "v2", base_version=1)
    old_sha = store.history("a.md")[-1].sha256
    assert old == "v1"
    with pytest.raises(ConflictError):
        store.write("a.md", "v3", "bob", "v3", base_version=2, expected_sha256=old_sha)


def test_diff_between_versions(store: Store) -> None:
    store.write("a.md", "one\ntwo\n", "alice", "v1")
    store.write("a.md", "one\nthree\n", "alice", "v2", base_version=1)
    text = store.diff("a.md", 1, 2)
    assert "-two" in text
    assert "+three" in text
    assert "a.md@1" in text


def test_read_section_line_range_and_truncation(store: Store) -> None:
    store.write(
        "doc.md",
        "# Первый\n\nстрока 1\nстрока 2\n\n# Второй\n\nстрока 3\n",
        "alice",
        "создал",
    )

    section = store.read("doc.md", section="Второй")
    assert section.content.startswith("# Второй")
    assert "строка 3" in section.content
    assert "Первый" not in section.content

    lines = store.read("doc.md", line_range=(3, 4))
    assert lines.content == "строка 1\nстрока 2\n"

    truncated = store.read("doc.md", max_chars=5)
    assert truncated.truncated is True
    assert truncated.content == "# Пер"
    assert truncated.total_chars > 5

    with pytest.raises(NotFoundError):
        store.read("doc.md", section="Нет такого")


def test_soft_delete_and_restore(store: Store) -> None:
    store.write("a.md", "text", "alice", "создал")
    deleted = store.delete("a.md", "alice", "ошибся")
    assert deleted.op == "delete"
    with pytest.raises(DeletedError):
        store.read("a.md")
    with pytest.raises(DeletedError):
        store.write("a.md", "new", "bob", "правка", base_version=2)

    restored = store.restore("a.md", "bob", "вернул")
    assert restored.op == "restore"
    assert store.read("a.md").content == "text"
    versions = [item.op for item in store.history("a.md")]
    assert versions == ["restore", "delete", "create"]


def test_rollback_version_keeps_history(store: Store) -> None:
    store.write("a.md", "v1", "alice", "v1")
    store.write("a.md", "v2", "alice", "v2", base_version=1)
    result = store.rollback_version("a.md", 1, "bob", "вернул v1")
    assert result.op == "rollback"
    assert result.version == 3
    assert store.read("a.md").content == "v1"
    assert [item.n for item in store.history("a.md")] == [3, 2, 1]


def test_blame_by_sections(store: Store) -> None:
    store.write("doc.md", "# A\n\nA0\n\n# B\n\nB0\n", "alice", "v1")
    store.write("doc.md", "# A\n\nA0\n\n# B\n\nB1\n", "bob", "правка B", base_version=1)
    blame = {item.title: item for item in store.blame("doc.md")}
    assert blame["A"].author == "alice"
    assert blame["B"].author == "bob"
    assert blame["B"].version == 2


def test_blob_deduplication(store: Store) -> None:
    content = "# same\n\nодинаковый текст\n"
    store.write("a.md", content, "alice", "a")
    store.write("b.md", content, "bob", "b")
    blobs = [path for path in store.blobs_dir.rglob("*") if path.is_file()]
    assert len(blobs) == 1
    assert store.stats()["blobs"] == 1


def test_gc_removes_orphan_blob(store: Store) -> None:
    store.write("a.md", "kept", "alice", "a")
    referenced = [path for path in store.blobs_dir.rglob("*") if path.is_file()]
    assert len(referenced) == 1

    orphan_dir = store.blobs_dir / "aa"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / ("b" * 62)
    orphan.write_bytes(b"orphan")

    result = store.gc(actor="admin")
    assert result.removed == 1
    assert orphan.exists() is False
    assert referenced[0].exists() is True


def test_audit_records_reads_and_errors(store: Store) -> None:
    store.write("a.md", "text", "alice", "создал")
    store.read("a.md", actor="bob")
    store.write("a.md", "text 2", "alice", "v2", base_version=1)
    with pytest.raises(ConflictError):
        store.write("a.md", "other", "bob", "устаревшая база", base_version=1)

    entries = store.audit(limit=10)
    actions = [entry.action for entry in entries]
    assert "read" in actions
    assert "write" in actions
    failures = [entry for entry in entries if entry.detail.get("ok") is False]
    assert failures
    assert failures[0].detail["error"] == "ConflictError"
    assert store.stats()["errors"] >= 1


def test_import_export_roundtrip(store: Store, tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Импорт\n\nтекст\n", encoding="utf-8")
    result = store.import_file("imported.md", source, "alice", "импорт")
    assert result.op == "create"
    assert store.export_markdown("imported.md") == "# Импорт\n\nтекст\n"

    imported = store.audit(limit=10, action="import")
    assert imported
