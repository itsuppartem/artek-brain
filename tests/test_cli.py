"""CLI Artek Brain: полный цикл и отказы через subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _run(
    root: Path,
    *args: str,
    token: str | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "artek_brain.cli", "--root", str(root)]
    if token is not None:
        command += ["--token", token]
    command += list(args)
    full_env = os.environ.copy()
    full_env.pop("BRAIN_TOKEN", None)
    full_env.pop("BRAIN_HOME", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        command,
        input=stdin,
        text=True,
        capture_output=True,
        env=full_env,
        timeout=60,
    )


def _json(result: subprocess.CompletedProcess[str]) -> Any:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _init(root: Path) -> str:
    data = _json(_run(root, "--json", "init"))
    assert data["role"] == "owner"
    return data["token"]


def _agent(root: Path, owner: str, name: str = "agent-1") -> str:
    data = _json(
        _run(
            root,
            "--json",
            "token",
            "create",
            "--name",
            name,
            "--role",
            "agent",
            "--scope",
            "docs:write",
            token=owner,
        )
    )
    return data["token"]


def test_cli_full_cycle_write_conflict_rollback_search(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    owner = _init(root)
    agent = _agent(root, owner)

    created = _json(
        _run(
            root,
            "--json",
            "write",
            "docs/a.md",
            "--reason",
            "create",
            token=agent,
            stdin="# A\n\nv1\n",
        )
    )
    assert created["version"] == 1 and created["created"] is True

    edited = _json(
        _run(
            root,
            "--json",
            "write",
            "docs/a.md",
            "--reason",
            "edit",
            "--base-version",
            "1",
            token=agent,
            stdin="# A\n\nv2\n",
        )
    )
    assert edited["version"] == 2

    conflict = _run(
        root,
        "write",
        "docs/a.md",
        "--reason",
        "stale",
        "--base-version",
        "1",
        token=agent,
        stdin="# A\n\nv3\n",
    )
    assert conflict.returncode != 0
    assert "Конфликт" in conflict.stderr
    assert "base_version=2" in conflict.stderr

    history = _json(_run(root, "--json", "history", "docs/a.md", token=agent))
    assert [item["n"] for item in history] == [2, 1]

    rolled = _json(
        _run(
            root,
            "--json",
            "rollback",
            "docs/a.md",
            "--to",
            "1",
            "--reason",
            "undo",
            token=agent,
        )
    )
    assert rolled["version"] == 3 and rolled["op"] == "rollback"

    read = _run(root, "read", "docs/a.md", token=agent)
    assert read.returncode == 0
    assert read.stdout == "# A\n\nv1\n"

    found = _json(_run(root, "--json", "search", "v1", token=agent))
    assert found["total"] >= 1
    assert found["items"][0]["path"] == "docs/a.md"
    assert found["items"][0]["version"] == 3

    deleted = _json(
        _run(
            root,
            "--json",
            "delete",
            "docs/a.md",
            "--reason",
            "устарело",
            token=agent,
        )
    )
    assert deleted["op"] == "delete"
    assert _run(root, "read", "docs/a.md", token=agent).returncode != 0

    restored = _json(
        _run(
            root,
            "--json",
            "restore",
            "docs/a.md",
            "--reason",
            "вернуть",
            token=agent,
        )
    )
    assert restored["op"] == "restore"
    assert _run(root, "read", "docs/a.md", token=agent).stdout == "# A\n\nv1\n"

    audit = _json(_run(root, "--json", "audit", token=owner))
    actions = {item["action"] for item in audit}
    assert {"write", "rollback_version", "delete", "restore"} <= actions

    stats = _json(_run(root, "--json", "stats", token=owner))
    assert stats["documents"] == 1
    assert stats["versions"] >= 5

    changes = _json(_run(root, "--json", "changes", token=agent))
    assert "next_cursor" in changes and changes["items"]


def test_cli_token_from_env_and_permission_denied(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    owner = _init(root)
    agent = _agent(root, owner)

    ok = _run(
        root,
        "write",
        "docs/env.md",
        "--reason",
        "env",
        env={"BRAIN_TOKEN": agent},
        stdin="# env\n",
    )
    assert ok.returncode == 0, ok.stderr

    denied = _run(
        root,
        "write",
        "other/env.md",
        "--reason",
        "мимо",
        token=agent,
        stdin="# no\n",
    )
    assert denied.returncode == 1
    assert "write" in denied.stderr and "other/env.md" in denied.stderr

    tokens = _json(_run(root, "--json", "token", "list", token=owner))
    agent_id = next(item["id"] for item in tokens if item["name"] == "agent-1")
    revoked = _json(
        _run(root, "--json", "token", "revoke", "--id", str(agent_id), token=owner)
    )
    assert revoked["revoked"] == 1

    after = _run(root, "read", "docs/env.md", token=agent)
    assert after.returncode == 3
    assert "токен" in after.stderr


def test_cli_tasks_review_inbox_and_brief(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    owner = _init(root)
    agent = _agent(root, owner)

    task = _json(
        _run(
            root,
            "--json",
            "task",
            "create",
            "--title",
            "Починить API",
            "--body",
            "Описание",
            "--area",
            "docs",
            token=agent,
        )
    )
    task_id = task["id"]
    assert task_id.startswith("DOCS-")

    listed = _run(root, "task", "list", token=agent)
    assert listed.returncode == 0 and task_id in listed.stdout

    claimed = _json(
        _run(
            root,
            "--json",
            "task",
            "claim",
            task_id,
            "--revision",
            "1",
            token=agent,
        )
    )
    claim_token = claimed["claim_token"]
    assert claimed["status"] == "claimed"

    started = _json(
        _run(
            root,
            "--json",
            "task",
            "start",
            task_id,
            "--revision",
            "2",
            "--claim-token",
            claim_token,
            token=agent,
        )
    )
    assert started["status"] == "in_progress"

    submitted = _json(
        _run(
            root,
            "--json",
            "task",
            "submit",
            task_id,
            "--revision",
            "3",
            "--claim-token",
            claim_token,
            "--evidence",
            "pytest -q: 100 passed",
            token=agent,
        )
    )
    assert submitted["status"] == "review"

    accepted = _json(
        _run(
            root,
            "--json",
            "task",
            "accept",
            task_id,
            "--revision",
            "4",
            "--note",
            "ок",
            token=owner,
        )
    )
    assert accepted["status"] == "done"

    sent = _json(
        _run(
            root,
            "--json",
            "inbox",
            "send",
            "--to",
            "agent-1",
            "--subject",
            "Готово",
            "--body",
            "Смотри задачу",
            token=owner,
        )
    )
    assert sent["read"] is False

    inbox = _json(_run(root, "--json", "inbox", "list", "--unread", token=agent))
    assert len(inbox) == 1 and inbox[0]["subject"] == "Готово"

    marked = _json(
        _run(
            root,
            "--json",
            "inbox",
            "read",
            "--message-id",
            str(inbox[0]["id"]),
            token=agent,
        )
    )
    assert marked["changed"] == 1

    brief = _run(root, "brief", token=agent)
    assert brief.returncode == 0
    assert "Бриф для agent-1" in brief.stdout
