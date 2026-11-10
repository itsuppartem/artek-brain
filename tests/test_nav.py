"""Навигация: ls/tree, оглавление, ссылки, лента изменений и карта памяти."""

from __future__ import annotations

import pytest

from artek_brain import Brain, Nav, PermissionDenied, Store


def token(brain: Brain, name: str = "alice", scopes=((("", "write")),)) -> str:
    return str(brain.access.create_token(name, "agent", list(scopes)))


def test_ls_and_tree_with_sizes_and_versions(brain: Brain) -> None:
    owner = str(brain.access.create_token("root", "owner"))
    brain.write(owner, "docs/a.md", "12345", "v1")
    brain.write(owner, "docs/a.md", "1234567890", "v2", base_version=1)
    brain.write(owner, "docs/b.md", "abc", "создал")

    [docs_dir] = brain.ls(owner, "")
    assert docs_dir.path == "docs"
    assert docs_dir.kind == "dir"
    assert docs_dir.size == 13
    assert docs_dir.versions == 3

    nodes = brain.tree(owner, "")
    paths = [node.path for node in nodes]
    assert paths.index("docs") < paths.index("docs/a.md")
    file_a = next(node for node in nodes if node.path == "docs/a.md")
    assert file_a.kind == "file"
    assert file_a.size == 10
    assert file_a.versions == 2
    assert file_a.version == 2
    assert file_a.depth == 1

    assert [node.path for node in brain.tree(owner, "", max_depth=0)] == ["docs"]


def test_toc_sections_with_char_sizes(brain: Brain) -> None:
    owner = str(brain.access.create_token("root", "owner"))
    brain.write(owner, "docs/a.md", "# A\n\n123\n\n## B\n\n4567\n", "создал")
    items = brain.toc(owner, "docs/a.md")
    assert [item.title for item in items] == ["A", "B"]
    assert [item.level for item in items] == [1, 2]
    assert items[0].chars == len("# A\n\n123\n\n")
    assert items[1].chars == len("## B\n\n4567\n")
    assert items[0].size == items[0].chars


def test_links_backlinks_and_broken_links(brain: Brain) -> None:
    owner = str(brain.access.create_token("root", "owner"))
    brain.write(
        owner,
        "docs/a.md",
        "# A\n\n[B](b.md)\n\n[[c.md]]\n\n[внешняя](https://example.com)\n",
        "создал",
    )
    brain.write(owner, "docs/b.md", "# B\n", "создал")

    links = brain.links(owner, "docs/a.md")
    assert [(link.target, link.exists) for link in links] == [
        ("docs/c.md", False),
        ("docs/b.md", True),
        ("https://example.com", False),
    ]
    assert links[2].external is True

    backlinks = brain.backlinks(owner, "docs/b.md")
    assert [link.source for link in backlinks] == ["docs/a.md"]

    broken = brain.broken_links(owner)
    assert [link.target for link in broken] == ["docs/c.md"]


def test_changes_since_cursor(brain: Brain) -> None:
    owner = str(brain.access.create_token("root", "owner"))
    brain.write(owner, "docs/a.md", "# A\n", "v1")
    first = brain.changes_since(owner, None)
    assert [item.path for item in first.items] == ["docs/a.md"]
    cursor = first.next_cursor

    same = brain.changes_since(owner, cursor)
    assert same.items == []

    brain.write(owner, "docs/b.md", "# B\n", "v2")
    second = brain.changes_since(owner, cursor)
    assert [item.path for item in second.items] == ["docs/b.md"]
    assert second.next_cursor > cursor


def test_changes_since_skips_failed_actions(brain: Brain) -> None:
    owner = str(brain.access.create_token("root", "owner"))
    reader = str(brain.access.create_token("reader", "agent", [("", "read")]))
    brain.write(owner, "docs/a.md", "# A\n", "v1")
    with pytest.raises(PermissionDenied):
        brain.write(reader, "docs/b.md", "# B\n", "нельзя")
    page = brain.changes_since(owner, None)
    assert all(item.detail.get("ok") is not False for item in page.items)


def test_readme_map(brain: Brain) -> None:
    owner = str(brain.access.create_token("root", "owner"))
    brain.write(owner, "docs/a.md", "# A\n", "создал")
    brain.write(owner, "docs/b.md", "# B\n", "создал")
    brain.write(
        owner,
        "README",
        "# Память\n\n## Разделы\n\n- [A](docs/a.md)\n- [B](docs/b.md)\n",
        "создал",
    )
    result = brain.readme_map(owner)
    assert result.readme == "README"
    assert result.title == "Память"
    assert [item.title for item in result.sections] == ["Память", "Разделы"]
    assert {link.target for link in result.links} == {"docs/a.md", "docs/b.md"}
    assert all(link.exists for link in result.links)
    top = {node.path for node in result.top_level}
    assert {"README", "docs"} <= top


def test_nav_respects_permissions(brain: Brain) -> None:
    writer = str(brain.access.create_token("writer", "agent", [("", "write")]))
    reader = str(
        brain.access.create_token("reader", "agent", [("docs/public", "read")])
    )
    brain.write(writer, "docs/public/a.md", "# A\n", "создал")
    brain.write(writer, "docs/secret/b.md", "# B\n", "создал")

    for node in brain.tree(reader, ""):
        assert not node.path.startswith("docs/secret")
    assert any(node.path == "docs/public/a.md" for node in brain.tree(reader, ""))
    with pytest.raises(PermissionDenied):
        brain.toc(reader, "docs/secret/b.md")
    with pytest.raises(PermissionDenied):
        brain.links(reader, "docs/secret/b.md")


def test_nav_direct_use(store: Store) -> None:
    store.write("docs/a.md", "# A\n\n[B](b.md)\n", "alice", "создал")
    store.write("docs/b.md", "# B\n", "alice", "создал")
    nav = Nav(store)
    assert [node.path for node in nav.ls("docs")] == ["docs/a.md", "docs/b.md"]
    assert [link.target for link in nav.links("docs/a.md")] == ["docs/b.md"]
    assert [link.source for link in nav.backlinks("docs/b.md")] == ["docs/a.md"]
