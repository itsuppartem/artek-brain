"""Очередь ревью: причины, просрочка, права и решения."""

from __future__ import annotations

import pytest

from artek_brain import Brain, ReviewQueue
from artek_brain.access import Access
from artek_brain.errors import PermissionDenied


@pytest.fixture()
def tokens(access: Access) -> dict[str, str]:
    return {
        "owner": str(access.create_token("owner", "owner")),
        "human": str(access.create_token("human-1", "human", scopes=[("", "read")])),
        "agent": str(access.create_token("agent-1", "agent", scopes=[("docs", "write")])),
    }


def _write(brain: Brain, token: str, path: str, status: str, review_by: str) -> None:
    content = (
        "---\n"
        "type: decision\n"
        f"status: {status}\n"
        "owner: owner\n"
        f"review_by: {review_by}\n"
        "---\n"
        "# Документ\n"
    )
    brain.write(token, path, content, "create")


def test_sync_adds_review_and_overdue_without_duplicates(
    brain: Brain, access: Access, tokens: dict[str, str]
) -> None:
    _write(brain, tokens["owner"], "docs/a.md", "review", "2030-01-01")
    _write(brain, tokens["owner"], "docs/b.md", "approved", "2000-01-01")
    queue = ReviewQueue(brain.store, access)

    added = queue.sync(brain, tokens["owner"])
    reasons = {(item.path, item.reason) for item in added}
    assert ("docs/a.md", "status_review") in reasons
    assert ("docs/b.md", "overdue_review") in reasons

    again = queue.sync(brain, tokens["owner"])
    assert again == []
    items = queue.list(tokens["owner"])
    assert len(items) == 2
    assert all(item.ref.startswith(item.path + "@") for item in items)


def test_queue_actions_only_owner_or_human(
    brain: Brain, access: Access, tokens: dict[str, str]
) -> None:
    _write(brain, tokens["owner"], "docs/a.md", "review", "2030-01-01")
    queue = ReviewQueue(brain.store, access)
    queue.sync(brain, tokens["owner"])
    item = queue.list(tokens["owner"])[0]

    with pytest.raises(PermissionDenied):
        queue.approve(tokens["agent"], item.id)

    approved = queue.approve(tokens["human"], item.id, "ок")
    assert approved.status == "approved"
    assert approved.actor == "human-1"
    assert approved.note == "ок"


def test_reject_and_defer(
    brain: Brain, access: Access, tokens: dict[str, str]
) -> None:
    _write(brain, tokens["owner"], "docs/a.md", "review", "2030-01-01")
    queue = ReviewQueue(brain.store, access)
    queue.sync(brain, tokens["owner"])
    item = queue.list(tokens["owner"])[0]

    deferred = queue.defer(tokens["owner"], item.id, "позже", "2031-01-01")
    assert deferred.status == "deferred"
    assert deferred.defer_until == "2031-01-01"

    rejected = queue.reject(tokens["owner"], item.id, "неверно")
    assert rejected.status == "rejected"


def test_queue_filters_by_read_permissions(
    brain: Brain, access: Access, tokens: dict[str, str]
) -> None:
    _write(brain, tokens["owner"], "docs/a.md", "review", "2030-01-01")
    _write(brain, tokens["owner"], "secret/b.md", "review", "2030-01-01")
    queue = ReviewQueue(brain.store, access)
    queue.sync(brain, tokens["owner"])

    agent_items = queue.list(tokens["agent"])
    assert [item.path for item in agent_items] == ["docs/a.md"]
    owner_items = queue.list(tokens["owner"])
    assert {item.path for item in owner_items} == {"docs/a.md", "secret/b.md"}
