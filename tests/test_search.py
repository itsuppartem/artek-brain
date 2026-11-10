"""Поиск FTS5: фильтры, сниппеты, пагинация, актуальность и история."""

from __future__ import annotations

import pytest

from artek_brain import Brain, PermissionDenied, Search, Store


def make_token(brain: Brain, name: str, role: str = "agent", scopes=()) -> str:
    return str(brain.access.create_token(name, role, scopes))


def test_search_finds_document_and_snippet(brain: Brain) -> None:
    token = make_token(brain, "alice", scopes=[("", "write")])
    brain.write(
        token,
        "docs/a.md",
        "---\ntype: note\nstatus: draft\ntags: [api, docs]\n---\n# A\n\nhello world\n",
        "создал",
    )
    page = brain.search(token, "hello")
    assert page.total == 1
    [item] = page.items
    assert item.path == "docs/a.md"
    assert item.title == "A"
    assert item.type == "note"
    assert item.status == "draft"
    assert item.tags == ("api", "docs")
    assert "hello" in item.snippet.lower()


def test_search_filters(brain: Brain) -> None:
    alice = make_token(brain, "alice", scopes=[("", "write")])
    bob = make_token(brain, "bob", scopes=[("", "write")])
    brain.write(
        alice,
        "docs/api.md",
        "---\ntype: api\nstatus: draft\ntags: [api, backend]\n---\n# API\n\nобщий текст\n",
        "alice",
    )
    brain.write(
        bob,
        "notes/note.md",
        "---\ntype: note\nstatus: done\ntags: [журнал]\n---\n# Note\n\nобщий текст\n",
        "bob",
    )
    brain.write(
        alice,
        "docs/guide.md",
        "---\ntype: guide\nstatus: draft\ntags: [api]\n---\n# Guide\n\nобщий текст\n",
        "alice",
    )

    assert {item.path for item in brain.search(alice, "текст").items} == {
        "docs/api.md",
        "notes/note.md",
        "docs/guide.md",
    }
    assert {
        item.path
        for item in brain.search(alice, "текст", path_prefix="docs").items
    } == {"docs/api.md", "docs/guide.md"}
    assert [item.path for item in brain.search(alice, "текст", type="note").items] == [
        "notes/note.md"
    ]
    assert [item.path for item in brain.search(alice, "текст", status="done").items] == [
        "notes/note.md"
    ]
    assert {
        item.path for item in brain.search(alice, "текст", tags=["api"]).items
    } == {"docs/api.md", "docs/guide.md"}
    assert [
        item.path for item in brain.search(alice, "текст", tags=["api", "backend"]).items
    ] == ["docs/api.md"]
    assert [item.path for item in brain.search(alice, "текст", author="bob").items] == [
        "notes/note.md"
    ]
    assert brain.search(alice, "текст", since="2000-01-01").total == 3
    assert brain.search(alice, "текст", until="2000-01-01").total == 0


def test_search_pagination(brain: Brain) -> None:
    token = make_token(brain, "alice", scopes=[("", "write")])
    for index in range(5):
        brain.write(token, f"docs/{index}.md", f"# {index}\n\nобщее слово\n", "создал")
    first = brain.search(token, "общее", limit=2, offset=0)
    second = brain.search(token, "общее", limit=2, offset=2)
    last = brain.search(token, "общее", limit=2, offset=4)
    assert first.total == 5 and first.has_more is True
    assert len(first.items) == 2 and len(second.items) == 2 and len(last.items) == 1
    assert last.has_more is False
    assert {item.path for item in first.items}.isdisjoint(
        {item.path for item in second.items}
    )


def test_search_without_query_lists_documents(brain: Brain) -> None:
    token = make_token(brain, "alice", scopes=[("", "write")])
    brain.write(token, "docs/a.md", "# A\n\nтекст\n", "создал")
    page = brain.search(token, "")
    assert [item.path for item in page.items] == ["docs/a.md"]


def test_index_updates_after_write_rollback_delete_restore(brain: Brain) -> None:
    token = make_token(brain, "alice", scopes=[("", "write")])
    brain.write(token, "docs/a.md", "# A\n\nпервый hello\n", "v1")
    brain.write(token, "docs/a.md", "# A\n\nвторой goodbye\n", "v2", base_version=1)
    assert brain.search(token, "hello").total == 0
    assert brain.search(token, "goodbye").total == 1

    brain.rollback_version(token, "docs/a.md", 1, "вернул")
    assert brain.search(token, "hello").total == 1
    assert brain.search(token, "goodbye").total == 0

    brain.delete(token, "docs/a.md", "удалил")
    assert brain.search(token, "hello").total == 0
    assert brain.search_history(token, "hello").total >= 1

    brain.restore(token, "docs/a.md", "вернул")
    assert brain.search(token, "hello").total == 1


def test_search_history_finds_old_versions(brain: Brain) -> None:
    token = make_token(brain, "alice", scopes=[("", "write")])
    brain.write(token, "docs/a.md", "# A\n\nстарый hello\n", "v1")
    brain.write(token, "docs/a.md", "# A\n\nновый goodbye\n", "v2", base_version=1)
    assert brain.search(token, "hello").total == 0

    history = brain.search_history(token, "hello")
    assert history.total == 1
    assert history.items[0].path == "docs/a.md"
    assert history.items[0].version == 1
    only_v2 = brain.search_history(token, "hello", version=2)
    assert only_v2.total == 0


def test_search_respects_read_scopes(brain: Brain) -> None:
    writer = make_token(brain, "writer", scopes=[("", "write")])
    reader = make_token(
        brain, "reader", scopes=[("docs/public", "read")]
    )
    brain.write(writer, "docs/public/a.md", "# A\n\nобщий текст\n", "создал")
    brain.write(writer, "docs/secret/b.md", "# B\n\nобщий текст\n", "создал")

    page = brain.search(reader, "общий")
    assert [item.path for item in page.items] == ["docs/public/a.md"]
    assert page.total == 1
    with pytest.raises(PermissionDenied):
        brain.search(reader, "общий", path_prefix="docs/secret")


def test_search_syncs_existing_store(tmp_path) -> None:
    store = Store(tmp_path / "brain")
    store.write("docs/a.md", "# A\n\nстарое содержимое\n", "alice", "создал")
    try:
        search = Search(store)
        page = search.search("старое")
        assert [item.path for item in page.items] == ["docs/a.md"]
        search.close()
    finally:
        store.close()
