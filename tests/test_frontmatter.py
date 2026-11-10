"""Frontmatter: разбор, запись, ошибки."""

from __future__ import annotations

import pytest

from artek_brain import FrontmatterError
from artek_brain import frontmatter


def test_parse_and_roundtrip() -> None:
    text = (
        "---\n"
        "type: guide\n"
        "status: draft\n"
        "owner: alice\n"
        "tags: [api, docs, 'с пробелом']\n"
        "review_by: 2026-12-31\n"
        "---\n"
        "# Заголовок\n\nтело\n"
    )
    parsed = frontmatter.parse(text)
    assert parsed.present is True
    assert parsed.get("type") == "guide"
    assert parsed["tags"] == ["api", "docs", "с пробелом"]
    assert parsed.body.startswith("# Заголовок")
    again = frontmatter.parse(parsed.to_text())
    assert again.data == parsed.data
    assert again.body == parsed.body


def test_no_frontmatter() -> None:
    text = "# Просто документ\n"
    parsed = frontmatter.parse(text)
    assert parsed.present is False
    assert parsed.data == {}
    assert parsed.body == text
    assert parsed.to_text() == text


def test_update_text() -> None:
    text = "---\nstatus: draft\ntags: []\n---\n# Doc\n"
    updated = frontmatter.update_text(text, status="approved", owner="bob")
    parsed = frontmatter.parse(updated)
    assert parsed.get("status") == "approved"
    assert parsed.get("owner") == "bob"
    assert parsed.get("tags") == []
    assert parsed.body == "# Doc\n"


def test_dump_quotes_unsafe_values() -> None:
    text = frontmatter.dump({"owner": "Иван Петров", "tags": ["a, b"]}, "body\n")
    parsed = frontmatter.parse(text)
    assert parsed.get("owner") == "Иван Петров"
    assert parsed.get("tags") == ["a, b"]
    assert parsed.body == "body\n"


@pytest.mark.parametrize(
    "text",
    [
        "---\ntype guide\n---\nbody\n",
        "---\ntype: guide\n",
        "---\ntype: guide\nstatus: draft\ntype: docs\n---\n",
        "---\ntags: [a, b\n---\n",
        "---\ntags: not-a-list\n---\n",
        "---\nowner: [a, b]\n---\n",
        "---\n- item\n---\n",
    ],
)
def test_invalid_frontmatter(text: str) -> None:
    with pytest.raises(FrontmatterError):
        frontmatter.parse(text)
