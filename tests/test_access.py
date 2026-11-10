"""Токены, роли, TTL, отзыв и политика защищённых путей."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from artek_brain import Access, PermissionDenied, Scope


def test_token_is_shown_once_and_only_hash_is_stored(access: Access) -> None:
    issued = access.create_token("agent-1", "agent", [("docs", "write")])
    token = str(issued)
    assert token.startswith("ab_")
    assert issued.record.name == "agent-1"
    assert issued.record.role == "agent"
    assert issued.record.scopes == (Scope("docs", "write"),)

    principal = access.verify(token)
    assert principal.name == "agent-1"
    assert principal.can_read("docs/a.md")
    assert principal.can_write("docs/a.md")
    assert not principal.can_write("other/a.md")

    with sqlite3.connect(access.db_path) as conn:
        row = conn.execute(
            "SELECT token_hash, scopes_json FROM tokens WHERE id=?", (issued.record.id,)
        ).fetchone()
    assert row is not None
    assert row[0] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in row
    assert token not in row[1]

    listed = access.list_tokens()
    assert len(listed) == 1
    assert listed[0].id == issued.record.id


def test_bad_and_empty_token_are_denied(access: Access) -> None:
    with pytest.raises(PermissionDenied) as empty:
        access.verify("")
    assert "valid token" in str(empty.value)

    with pytest.raises(PermissionDenied) as unknown:
        access.verify("ab_does-not-exist")
    assert "неизвестный токен" in str(unknown.value)


def test_ttl_expires_token(access: Access) -> None:
    expired = access.create_token("short", "agent", [("", "read")], ttl=0)
    with pytest.raises(PermissionDenied) as error:
        access.verify(str(expired))
    assert "истёк" in str(error.value)

    alive = access.create_token("long", "agent", [("", "read")], ttl=3600)
    assert access.verify(str(alive)).name == "long"


def test_revoke_token(access: Access) -> None:
    issued = access.create_token("agent-1", "agent", [("", "read")])
    assert access.revoke(str(issued)) is True
    with pytest.raises(PermissionDenied) as error:
        access.verify(str(issued))
    assert "отозван" in str(error.value)
    assert access.revoke("ab_unknown") is False


def test_scopes_and_roles(access: Access) -> None:
    human = access.create_token("human-1", "human", [("docs", "read")])
    principal = access.verify(str(human))
    access.require_read(principal, "docs/a.md")
    with pytest.raises(PermissionDenied) as denied_write:
        access.require_write(principal, "docs/a.md")
    assert "write" in str(denied_write.value)
    assert "docs/a.md" in str(denied_write.value)

    auditor = access.verify(
        str(access.create_token("audit-1", "auditor", [("", "admin")]))
    )
    access.require_journal(auditor, action="audit")
    with pytest.raises(PermissionDenied):
        access.require_read(auditor, "docs/a.md")
    with pytest.raises(PermissionDenied):
        access.require_write(auditor, "docs/a.md")


def test_protected_paths_and_inbox(access: Access) -> None:
    agent = access.verify(str(access.create_token("agent", "agent", [("", "write")])))
    for path in ("owner/x.md", "rules/x.md", "approved/x.md", "README"):
        with pytest.raises(PermissionDenied) as error:
            access.require_write(agent, path)
        assert "owner" in str(error.value)
        assert path in str(error.value)

    access.require_write(agent, "owner/inbox/idea.md", creating=True)
    with pytest.raises(PermissionDenied) as edit:
        access.require_write(agent, "owner/inbox/idea.md", creating=False)
    assert "только создавать" in str(edit.value)
    with pytest.raises(PermissionDenied):
        access.require_write(agent, "owner/inbox/sub/idea.md", creating=True)

    owner = access.verify(str(access.create_token("root", "owner")))
    for path in ("owner/x.md", "rules/x.md", "approved/x.md", "README"):
        access.require_write(owner, path)
    access.require_write(owner, "owner/inbox/idea.md", creating=False)
