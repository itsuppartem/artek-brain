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


def test_cli_coord_inbox_and_brief(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    owner = _init(root)
    agent = _agent(root, owner)

    task_cmd = _run(root, "task", "list", token=agent)
    assert task_cmd.returncode != 0

    present = _json(
        _run(root, "--json", "coord", "presence", "--activity", "пишу тесты", token=agent)
    )
    assert present["name"] == "agent-1" and present["activity"] == "пишу тесты"
    online = _json(_run(root, "--json", "coord", "agents", token=owner))
    assert [item["name"] for item in online] == ["agent-1"]

    _run(root, "write", "docs/a.md", "--content", "# A\n", "--reason", "старт", token=agent)
    who = _json(_run(root, "--json", "coord", "who-knows", "--path", "docs", token=owner))
    assert who["author"] == "agent-1"

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
            "Смотри заметку",
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
    assert "доски задач нет" in brief.stdout
    assert "Сейчас работают: 1" in brief.stdout


def test_cli_repack_requires_owner_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    owner = _init(root)
    agent = _agent(root, owner)

    def document(version: int) -> str:
        lines = ["# A", "", f"Версия {version}.", ""]
        for section in range(4):
            lines += [f"## Раздел {section}", ""]
            for line in range(15):
                words = [
                    f"слово{(section * 7 + line * 3 + offset) % 40}"
                    for offset in range(16)
                ]
                lines.append(f"- {line}: " + " ".join(words))
            lines.append("")
        lines += ["## Меняется", "", f"значение {version}"]
        return "\n".join(lines) + "\n"

    for version in range(1, 6):
        _json(
            _run(
                root,
                "--json",
                "write",
                "docs/a.md",
                "--reason",
                f"v{version}",
                *(
                    []
                    if version == 1
                    else ["--base-version", str(version - 1)]
                ),
                token=owner,
                stdin=document(version),
            )
        )

    denied = _run(root, "repack", token=agent)
    assert denied.returncode != 0
    assert "owner" in denied.stderr

    first = _json(
        _run(root, "--json", "repack", "--snapshot-every", "2", token=owner)
    )
    assert first["versions"] == 5
    assert first["rewritten"] > 0
    assert first["deltas"] > 0

    second = _json(
        _run(root, "--json", "repack", "--snapshot-every", "2", token=owner)
    )
    assert second["rewritten"] == 0
    assert second["deltas"] == first["deltas"]

    history = _json(_run(root, "--json", "history", "docs/a.md", token=owner))
    assert len(history) == 5
    content = _run(root, "read", "docs/a.md", "--version", "2", token=owner)
    assert content.returncode == 0
    assert "значение 2" in content.stdout


def test_cli_backup_and_maintain(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    owner = _init(root)
    agent = _agent(root, owner)
    _json(
        _run(
            root,
            "--json",
            "write",
            "docs/a.md",
            "--content",
            "# A\n\nтекст\n",
            "--reason",
            "создал",
            token=agent,
        )
    )
    output = tmp_path / "backups"
    created = _json(
        _run(
            root,
            "--json",
            "backup",
            "create",
            "--output",
            str(output),
            "--keep",
            "2",
            token=owner,
        )
    )
    assert Path(created["archive"]).is_file()
    verified = _json(
        _run(
            root,
            "--json",
            "backup",
            "verify",
            created["archive"],
            token=owner,
        )
    )
    assert verified["ok"] is True and verified["documents"] == 1
    maintained = _json(_run(root, "--json", "maintain", token=owner))
    assert maintained["action"] == "maintain"
    assert maintained["checkpoints"]

    denied = _run(
        root,
        "--json",
        "backup",
        "create",
        "--output",
        str(output),
        token=agent,
    )
    assert denied.returncode == 1
