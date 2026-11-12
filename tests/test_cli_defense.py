"""CLI защиты: предложения, прогоны, аномалии и приостановка токенов."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from artek_brain.service import Service


def _run(
    root: Path,
    *args: str,
    token: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "artek_brain.cli", "--root", str(root)]
    if token is not None:
        command += ["--token", token]
    command += list(args)
    env = os.environ.copy()
    env.pop("BRAIN_TOKEN", None)
    env.pop("BRAIN_HOME", None)
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )


def _json(result: subprocess.CompletedProcess[str]) -> Any:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _init(root: Path) -> str:
    return _json(_run(root, "--json", "init"))["token"]


def _token(root: Path, owner: str, name: str, role: str, scope: str) -> str:
    return _json(
        _run(
            root,
            "--json",
            "token",
            "create",
            "--name",
            name,
            "--role",
            role,
            "--scope",
            scope,
            token=owner,
        )
    )["token"]


def test_cli_proposal_list_approve_reject(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    owner = _init(root)
    agent = _token(root, owner, "agent-1", "agent", "docs:write")
    human = _token(root, owner, "reviewer", "human", ":write")

    with Service(root) as service:
        proposal = service.proposals.submit(
            agent, "docs/a.md", "# A\n\nтекст\n", "предлагаю"
        )
        rejected = service.proposals.submit(
            agent, "docs/b.md", "# B\n", "предлагаю"
        )
    assert proposal.path == "proposals/agent-1/docs/a.md"

    listed = _json(_run(root, "--json", "proposal", "list", token=human))
    assert [item["path"] for item in listed] == [rejected.path, proposal.path]

    approved = _json(
        _run(root, "--json", "proposal", "approve", proposal.path, token=human)
    )
    assert approved["proposal"]["status"] == "approved"
    assert approved["approval"]["approved_by"] == "reviewer"

    read = _json(_run(root, "--json", "read", "approved/docs/a.md", token=owner))
    assert read["content"] == "# A\n\nтекст\n"

    result = _json(
        _run(
            root,
            "--json",
            "proposal",
            "reject",
            rejected.path,
            "--reason",
            "нет",
            token=human,
        )
    )
    assert result["status"] == "rejected"


def test_cli_token_suspend_and_resume(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    owner = _init(root)
    agent = _token(root, owner, "agent-1", "agent", "docs:write")

    assert _run(
        root,
        "--json",
        "write",
        "docs/a.md",
        "--reason",
        "create",
        token=agent,
    ).returncode == 0

    suspended = _json(
        _run(root, "--json", "token", "suspend", "--name", "agent-1", token=owner)
    )
    assert suspended["suspended"] == 1
    denied = _run(root, "read", "docs/a.md", token=agent)
    assert denied.returncode != 0
    assert "приостановлен" in denied.stderr

    resumed = _json(
        _run(root, "--json", "token", "resume", "--name", "agent-1", token=owner)
    )
    assert resumed["resumed"] == 1
    assert _run(root, "read", "docs/a.md", token=agent).returncode == 0


def test_cli_anomaly_scan_suspends_spike(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    owner = _init(root)
    agent = _token(root, owner, "agent-1", "agent", "docs:write")

    with Service(root) as service:
        for index in range(20):
            service.brain.write(
                agent,
                "docs/spam.md",
                f"# версия {index}\n",
                "быстро",
                base_version=index or None,
            )

    scanned = _json(
        _run(root, "--json", "anomaly", "scan", "--auto-suspend", token=owner)
    )
    incidents = scanned["incidents"]
    assert any(
        item["kind"] == "version_spike" and item["actor"] == "agent-1"
        for item in incidents
    )
    assert any(item["suspended"] for item in incidents)

    listed = _json(_run(root, "--json", "anomaly", "list", token=owner))
    assert any(item["kind"] == "version_spike" for item in listed)

    denied = _run(root, "read", "docs/spam.md", token=agent)
    assert denied.returncode != 0
    assert "приостановлен" in denied.stderr

    _json(_run(root, "--json", "token", "resume", "--name", "agent-1", token=owner))
    assert _run(root, "read", "docs/spam.md", token=agent).returncode == 0
