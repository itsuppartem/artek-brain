"""CLI качества знаний: report, contradictions и очередь ревью."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _run(root: Path, *args: str, token: str | None = None, stdin: str | None = None):
    command = [sys.executable, "-m", "artek_brain.cli", "--root", str(root)]
    if token:
        command += ["--token", token]
    command += list(args)
    env = os.environ.copy()
    env.pop("BRAIN_TOKEN", None)
    env.pop("BRAIN_HOME", None)
    return subprocess.run(
        command,
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )


def _json(result: subprocess.CompletedProcess[str]) -> Any:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _doc(fact: str, review_by: str = "2030-01-01") -> str:
    return (
        "---\n"
        "type: decision\n"
        "status: approved\n"
        "owner: owner\n"
        f"review_by: {review_by}\n"
        f"facts: [{fact}]\n"
        "---\n"
        "# Документ\n"
    )


def test_cli_quality_report_contradictions_queue(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    token = _json(_run(root, "--json", "init"))["token"]
    assert _run(
        root, "write", "docs/a.md", "--reason", "a", token=token,
        stdin=_doc("port = 8001"),
    ).returncode == 0
    assert _run(
        root, "write", "docs/b.md", "--reason", "b", token=token,
        stdin=_doc("port = 8002", "2000-01-01"),
    ).returncode == 0

    report = _json(_run(root, "--json", "quality", "report", token=token))
    assert report["documents"] == 2
    assert report["overdue"] == 1
    assert report["freshness_overdue"] == 0.5

    contradictions = _json(
        _run(root, "--json", "quality", "contradictions", token=token)
    )
    assert len(contradictions["items"]) == 1
    assert contradictions["items"][0]["key"] == "port"

    queue = _json(_run(root, "--json", "quality", "queue", "list", token=token))
    assert len(queue["items"]) == 1
    assert queue["items"][0]["reason"] == "overdue_review"

    item_id = str(queue["items"][0]["id"])
    approved = _json(
        _run(
            root, "--json", "quality", "queue", "approve", "--id", item_id,
            token=token,
        )
    )
    assert approved["item"]["status"] == "approved"
