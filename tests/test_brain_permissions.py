"""Права в фасаде Brain: роли, защищённые пути, inbox и откаты."""

from __future__ import annotations

import pytest

from artek_brain import Brain, PermissionDenied


def tokens(brain: Brain) -> dict[str, str]:
    """Создать набор токенов для тестов и вернуть их строки."""
    access = brain.access
    return {
        "owner": str(access.create_token("root", "owner")),
        "human": str(
            access.create_token("human-1", "human", [("docs", "write")])
        ),
        "agent": str(
            access.create_token("agent-1", "agent", [("docs", "write")])
        ),
        "auditor": str(access.create_token("auditor-1", "auditor", [("", "read")])),
        "reader": str(access.create_token("reader", "agent", [("docs", "read")])),
    }


def test_agent_can_write_and_read_its_scope(brain: Brain) -> None:
    token = tokens(brain)
    result = brain.write(token["agent"], "docs/a.md", "# A\n\ntext\n", "создал")
    assert result.version == 1
    read = brain.read(token["agent"], "docs/a.md")
    assert read.content == "# A\n\ntext\n"
    assert brain.read(token["reader"], "docs/a.md").version == 1


def test_rejects_write_and_read_outside_scope(brain: Brain) -> None:
    token = tokens(brain)
    with pytest.raises(PermissionDenied) as error:
        brain.write(token["agent"], "other/a.md", "data", "мимо")
    message = str(error.value)
    assert "write" in message and "other/a.md" in message

    with pytest.raises(PermissionDenied) as read_error:
        brain.read(token["reader"], "other/a.md")
    assert "read" in str(read_error.value)


def test_protected_paths_only_for_owner(brain: Brain) -> None:
    token = tokens(brain)
    for role in ("agent", "human"):
        for path in ("owner/a.md", "rules/a.md", "approved/a.md", "README"):
            with pytest.raises(PermissionDenied):
                brain.write(token[role], path, "# x\n", "нельзя")
    assert brain.write(token["owner"], "rules/a.md", "# Правило\n", "owner").version == 1
    assert brain.write(token["owner"], "README", "# Память\n", "owner").version == 1


def test_inbox_allows_only_creation(brain: Brain) -> None:
    token = tokens(brain)
    created = brain.write(
        token["agent"], "owner/inbox/idea.md", "# Идея\n", "предложение"
    )
    assert created.version == 1
    assert brain.read(token["owner"], "owner/inbox/idea.md").content == "# Идея\n"

    with pytest.raises(PermissionDenied) as error:
        brain.write(
            token["agent"],
            "owner/inbox/idea.md",
            "# Идея 2\n",
            "правка",
            base_version=1,
        )
    assert "только создавать" in str(error.value)

    assert brain.write(
        token["owner"],
        "owner/inbox/idea.md",
        "# Идея 2\n",
        "owner правит",
        base_version=1,
    ).version == 2


def test_auditor_only_reads_audit_and_stats(brain: Brain) -> None:
    token = tokens(brain)
    brain.write(token["owner"], "docs/a.md", "# A\n", "создал")

    assert brain.stats(token["auditor"])["documents"] == 1
    assert brain.audit(token["auditor"], action="write")
    with pytest.raises(PermissionDenied):
        brain.read(token["auditor"], "docs/a.md")
    with pytest.raises(PermissionDenied):
        brain.write(token["auditor"], "docs/b.md", "# B\n", "нельзя")
    with pytest.raises(PermissionDenied):
        brain.audit(token["agent"])


def test_denials_are_written_to_audit(brain: Brain) -> None:
    token = tokens(brain)
    with pytest.raises(PermissionDenied):
        brain.write(token["agent"], "rules/x.md", "# x\n", "нельзя")

    entries = brain.store.audit()
    denied = [
        entry
        for entry in entries
        if entry.action == "write" and entry.detail.get("ok") is False
    ]
    assert denied
    assert denied[0].actor == "agent-1"
    assert denied[0].detail["error"] == "PermissionDenied"
    assert "rules/x.md" in denied[0].detail["message"]


def test_agent_cannot_rollback_foreign_edits(brain: Brain) -> None:
    token = tokens(brain)
    brain.write(token["owner"], "docs/a.md", "# A\n\nбаза\n", "v1")
    brain.write(token["agent"], "docs/a.md", "# A\n\nагент\n", "v2", base_version=1)
    own = brain.rollback_version(token["agent"], "docs/a.md", 1, "откат своей правки")
    assert own.version == 3

    brain.write(token["owner"], "docs/a.md", "# A\n\nowner\n", "v4", base_version=3)
    with pytest.raises(PermissionDenied) as error:
        brain.rollback_version(token["agent"], "docs/a.md", 2, "чужое")
    assert "чужие правки" in str(error.value)

    allowed = brain.rollback_version(token["owner"], "docs/a.md", 2, "owner может")
    assert allowed.version == 5


def test_agent_cannot_rollback_approved_even_own(brain: Brain) -> None:
    token = tokens(brain)
    brain.write(token["owner"], "approved/a.md", "# Утверждено\n", "v1")
    with pytest.raises(PermissionDenied):
        brain.rollback_version(token["agent"], "approved/a.md", 1, "нельзя")


def test_revoked_token_is_denied_and_audited(brain: Brain) -> None:
    token = tokens(brain)
    brain.access.revoke(token["reader"])
    with pytest.raises(PermissionDenied) as error:
        brain.read(token["reader"], "docs/a.md")
    assert "отозван" in str(error.value)
    entries = brain.store.audit()
    assert any(
        entry.detail.get("error") == "PermissionDenied" for entry in entries
    )
