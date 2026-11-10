"""Интерфейсы песочницы: CLI, HTTP и MCP без нового стартового инструмента."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from artek_brain.http_api import create_server
from artek_brain.mcp_server import BrainMCP
from artek_brain.service import Service


def _cli(
    root: Path,
    *args: str,
    token: str | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
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
        timeout=90,
    )


def _json(result: subprocess.CompletedProcess[str]) -> Any:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cli_sandbox_run_and_status(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    owner = _json(_cli(root, "--json", "init"))["token"]
    agent = _json(
        _cli(
            root,
            "--json",
            "token",
            "create",
            "--name",
            "agent-1",
            "--role",
            "agent",
            "--scope",
            "docs:read",
            token=owner,
        )
    )["token"]
    _json(
        _cli(
            root,
            "--json",
            "write",
            "docs/a.md",
            "--content",
            "# A\n\ncli slice\n",
            "--reason",
            "создал",
            token=owner,
        )
    )
    script = tmp_path / "script.py"
    script.write_text(
        "import brainlib\nprint(brainlib.read('docs/a.md'))\n", encoding="utf-8"
    )
    run = _json(
        _cli(
            root,
            "--json",
            "sandbox",
            "run",
            "--lang",
            "python",
            "--paths",
            "docs/",
            str(script),
            token=agent,
        )
    )
    assert run["exit_code"] == 0, run["stderr"]
    assert "cli slice" in run["stdout"]
    assert run["slice"]["paths"] == ["docs/a.md"]

    status = _json(
        _cli(root, "--json", "sandbox", "status", run["id"], token=agent)
    )
    assert status["status"] == "done"


class _Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


def test_http_sandbox_run_status_and_denials(tmp_path: Path) -> None:
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    agent = str(service.access.create_token("agent", "agent", [("docs", "read")]))
    auditor = str(service.access.create_token("aud", "auditor"))
    service.brain.write(owner, "docs/a.md", "# A\n\nhttp slice\n", "создал")
    server = create_server(service, "127.0.0.1", 0, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    client = _Client(f"http://{host}:{port}")
    try:
        status, _ = client.call(
            "POST", "/sandbox/run", {"script": "print(1)\n", "language": "python"}
        )
        assert status == 401

        status, _ = client.call(
            "POST",
            "/sandbox/run",
            {"script": "print(1)\n", "language": "python"},
            token=auditor,
        )
        assert status == 403

        status, result = client.call(
            "POST",
            "/sandbox/run",
            {
                "script": "import brainlib\nprint(brainlib.read('docs/a.md'))\n",
                "language": "python",
                "inputs": {"prefix": "docs"},
            },
            token=agent,
        )
        assert status == 200, result
        assert result["exit_code"] == 0
        assert "http slice" in result["stdout"]

        status, state = client.call(
            "GET", f"/sandbox/status/{result['id']}", token=agent
        )
        assert status == 200 and state["status"] == "done"
    finally:
        server.shutdown()
        server.server_close()
        service.close()


def test_mcp_help_topic_and_taskAction(tmp_path: Path) -> None:
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    agent = str(service.access.create_token("agent", "agent", [("docs", "read")]))
    service.brain.write(owner, "docs/a.md", "# A\n\nmcp slice\n", "создал")
    app = BrainMCP(service, agent)
    try:
        names = [item["name"] for item in app.list_tools()]
        assert len(names) == 6
        assert "brain_sandbox" not in names

        help_text, error = app.call("brain_help", {"topic": "sandbox"})
        assert error is False
        assert "brain sandbox run" in help_text
        assert "POST /sandbox/run" in help_text

        text, error = app.call(
            "brain_task",
            {
                "action": "sandbox_run",
                "script": "import brainlib\nprint(brainlib.read('docs/a.md'))\n",
                "language": "python",
                "inputs": {"prefix": "docs"},
            },
        )
        assert error is False, text
        result = json.loads(text)
        assert result["exit_code"] == 0
        assert "mcp slice" in result["stdout"]
    finally:
        service.close()
