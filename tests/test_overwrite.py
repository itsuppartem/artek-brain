"""Правка на горячую: overwrite_current заменяет текущую версию без следа."""

from __future__ import annotations

import pytest

from artek_brain import Brain, PermissionDenied


@pytest.fixture()
def tokens(brain: Brain) -> dict[str, str]:
    access = brain.access
    return {
        "owner": str(access.create_token("root", "owner")),
        "agent": str(access.create_token("agent-1", "agent", [("notes", "write")])),
    }


def _audit_count(brain: Brain, path: str) -> int:
    """Число записей журнала об изменениях документа (чтения не считаем)."""
    rows = brain.store.audit(limit=1000)
    mutating = {"write", "create", "overwrite", "merge", "delete", "restore"}
    return sum(
        1
        for r in rows
        if getattr(r, "path", None) == path and getattr(r, "action", None) in mutating
    )


def test_overwrite_keeps_single_version_and_leaves_trace(brain: Brain, tokens: dict[str, str]) -> None:
    brain.write(tokens["owner"], "notes/a.md", "первая\n", "старт")
    assert brain.store.history("notes/a.md")  # версия 1 есть
    audit_before = _audit_count(brain, "notes/a.md")

    res = brain.overwrite(tokens["owner"], "notes/a.md", "вторая\n")
    assert res.version == 1 and res.op == "overwrite" and res.changed

    # Версия по-прежнему одна, но содержимое новое.
    hist = brain.store.history("notes/a.md")
    assert len(hist) == 1
    assert brain.read(tokens["owner"], "notes/a.md").content == "вторая\n"
    # Новой версии нет, но в журнале осталась одна строка overwrite.
    assert _audit_count(brain, "notes/a.md") == audit_before + 1


def test_overwrite_preserves_prior_history(brain: Brain, tokens: dict[str, str]) -> None:
    brain.write(tokens["owner"], "notes/b.md", "v1\n", "старт")
    brain.write(tokens["owner"], "notes/b.md", "v2\n", "правка", base_version=1)
    before = len(brain.store.history("notes/b.md"))
    assert before == 2

    brain.overwrite(tokens["owner"], "notes/b.md", "v2-горячая\n")
    hist = brain.store.history("notes/b.md")
    assert len(hist) == 2  # число версий не изменилось
    # Старая версия 1 читается без изменений.
    assert brain.read(tokens["owner"], "notes/b.md", version=1).content == "v1\n"
    assert brain.read(tokens["owner"], "notes/b.md").content == "v2-горячая\n"


def test_overwrite_noop_when_same(brain: Brain, tokens: dict[str, str]) -> None:
    brain.write(tokens["owner"], "notes/c.md", "одно и то же\n", "старт")
    res = brain.overwrite(tokens["owner"], "notes/c.md", "одно и то же\n")
    assert res.op == "noop" and not res.changed


def test_overwrite_creates_when_absent(brain: Brain, tokens: dict[str, str]) -> None:
    res = brain.overwrite(tokens["owner"], "notes/new.md", "сразу\n")
    assert res.version == 1
    assert brain.read(tokens["owner"], "notes/new.md").content == "сразу\n"


def test_overwrite_denied_for_non_owner(brain: Brain, tokens: dict[str, str]) -> None:
    brain.write(tokens["owner"], "notes/d.md", "x\n", "старт")
    with pytest.raises(PermissionDenied):
        brain.overwrite(tokens["agent"], "notes/d.md", "y\n")


def test_overwrite_then_search_finds_new_text(brain: Brain, tokens: dict[str, str]) -> None:
    brain.write(tokens["owner"], "notes/e.md", "старое слово гвоздь\n", "старт")
    brain.overwrite(tokens["owner"], "notes/e.md", "новое слово молоток\n")
    hits = brain.search(tokens["owner"], "молоток")
    assert any(h.path == "notes/e.md" for h in hits.items)
