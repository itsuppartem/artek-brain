"""Песочница скриптов: реальные запуски bwrap и проверка изоляции."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from artek_brain import (
    PermissionDenied,
    Sandbox,
    SandboxConfig,
    SandboxUnavailable,
)
from artek_brain.service import Service


@pytest.fixture()
def svc(tmp_path: Path):
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    agent = str(
        service.access.create_token(
            "agent-1", "agent", [("docs", "read"), ("docs", "write")]
        )
    )
    service.brain.write(owner, "docs/a.md", "# A\n\nhello slice\n", "создал")
    service.brain.write(owner, "secret/plan.md", "TOP SECRET PLAN\n", "создал")
    try:
        yield service, owner, agent
    finally:
        service.close()


def test_python_run_uses_slice_stores_script_and_audits(svc) -> None:
    service, owner, agent = svc
    script = (
        "import brainlib\n"
        "print(brainlib.read('docs/a.md'))\n"
        "open('/out/result.txt', 'w').write('ready')\n"
    )
    result = service.sandbox_run(
        agent, script, "python", inputs={"prefix": "docs"}
    )

    assert result["exit_code"] == 0, result["stderr"]
    assert "hello slice" in result["stdout"]
    assert result["slice"]["paths"] == ["docs/a.md"]
    assert result["out"][0]["name"] == "result.txt"
    assert result["out"][0]["content"] == "ready"
    assert result["peak_memory_bytes"] >= 0
    assert result["duration_ms"] >= 0

    stored = service.brain.read(owner, result["script_path"])
    assert stored.content == script

    status = service.sandbox_status(agent, result["id"])
    assert status["status"] == "done"

    audit = service.brain.audit(owner, action="sandbox_run", limit=5)
    assert audit and audit[0].detail["script_sha256"] == result["script_sha256"]
    assert audit[0].detail["slice"]["paths"] == ["docs/a.md"]
    calls = service.registry.calls("sandbox.run")
    assert calls and calls[0]["ok"] is True


def test_network_is_disabled(svc) -> None:
    service, _, agent = svc
    script = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=2)\n"
        "    print('CONNECTED')\n"
        "except OSError:\n"
        "    print('BLOCKED')\n"
    )
    result = service.sandbox_run(agent, script, "python")
    assert result["exit_code"] == 0
    assert "BLOCKED" in result["stdout"]
    assert "CONNECTED" not in result["stdout"]


def test_no_host_files_and_no_writes_outside_allowed(svc) -> None:
    service, _, agent = svc
    host_db = str(service.store.db_path)
    script = f"""
import os
checks = []
for path in ['/root/.ssh/id_rsa', '/etc/shadow', {host_db!r}]:
    try:
        open(path).read()
        checks.append('LEAK ' + path)
    except OSError:
        checks.append('blocked ' + path)
for path in ['/x', '/data/new.md', '/usr/local/x']:
    try:
        open(path, 'w').write('x')
        checks.append('WRITE ' + path)
    except OSError:
        checks.append('ro ' + path)
open('/tmp/ok.txt', 'w').write('ok')
open('/out/ok.txt', 'w').write('ok')
print('\\n'.join(checks))
"""
    result = service.sandbox_run(agent, script, "python", inputs={"prefix": "docs"})
    assert result["exit_code"] == 0, result["stderr"]
    assert "LEAK" not in result["stdout"]
    assert "WRITE" not in result["stdout"]
    assert result["stdout"].count("blocked") == 3
    assert result["stdout"].count("ro") == 3
    assert [item["name"] for item in result["out"]] == ["ok.txt"]


def test_fork_bomb_hits_process_limit(svc) -> None:
    service, _, agent = svc
    script = (
        "import os, time\n"
        "while True:\n"
        "    try:\n"
        "        os.fork()\n"
        "    except OSError:\n"
        "        pass\n"
        "    time.sleep(0.005)\n"
    )
    result = service.sandbox_run(
        agent,
        script,
        "python",
        limits={"max_processes": 8, "wall_seconds": 4},
    )
    assert result["limit_reason"] == "processes"
    assert result["exit_code"] != 0
    assert result["duration_ms"] < 4000


def test_infinite_loop_is_killed_by_wall_time(svc) -> None:
    service, _, agent = svc
    result = service.sandbox_run(
        agent, "while True:\n    pass\n", "python", limits={"wall_seconds": 1}
    )
    assert result["timed_out"] is True
    assert result["cancelled"] is False
    assert result["exit_code"] != 0
    assert result["duration_ms"] < 4000


def test_memory_over_limit_is_killed(svc) -> None:
    service, _, agent = svc
    result = service.sandbox_run(
        agent,
        "x = bytearray(400 * 1024 * 1024)\nprint(len(x))\n",
        "python",
        limits={"memory_bytes": 64 * 1024 * 1024, "wall_seconds": 5},
    )
    assert result["exit_code"] != 0
    assert "419430400" not in result["stdout"]


def test_huge_output_is_truncated(svc) -> None:
    service, _, agent = svc
    result = service.sandbox_run(
        agent,
        "print('x' * 200000)\n",
        "python",
        limits={"max_output_bytes": 2048},
    )
    assert result["exit_code"] == 0
    assert result["stdout_truncated"] is True
    assert len(result["stdout"]) == 2048
    assert result["stdout_bytes"] > 200000


def test_host_environment_secrets_are_not_visible(svc, monkeypatch) -> None:
    service, _, agent = svc
    monkeypatch.setenv("BRAIN_HOST_SECRET", "topsecret-value")
    monkeypatch.setenv("BRAIN_TOKEN", "host-token")
    script = (
        "import os\n"
        "print(repr(os.environ.get('BRAIN_HOST_SECRET')))\n"
        "print('HAS_TOKEN' if os.environ.get('BRAIN_TOKEN') else 'NO_TOKEN')\n"
        "print(sorted(os.environ))\n"
    )
    result = service.sandbox_run(agent, script, "python")
    assert result["exit_code"] == 0
    assert "topsecret-value" not in result["stdout"]
    assert "host-token" not in result["stdout"]
    assert "None" in result["stdout"]
    assert "NO_TOKEN" in result["stdout"]
    assert "BRAIN_TOKEN" not in result["stdout"].split("NO_TOKEN", 1)[1]


def test_documents_outside_token_rights_are_absent_and_denied(svc) -> None:
    service, _, agent = svc
    script = (
        "import brainlib\n"
        "print(brainlib.documents())\n"
        "try:\n"
        "    print(brainlib.read('secret/plan.md'))\n"
        "except Exception:\n"
        "    print('DENIED')\n"
    )
    result = service.sandbox_run(agent, script, "python", inputs={"prefix": ""})
    assert result["exit_code"] == 0, result["stderr"]
    assert "docs/a.md" in result["stdout"]
    assert "secret/plan.md" not in result["stdout"]
    assert "DENIED" in result["stdout"]
    assert result["slice"]["paths"] == ["docs/a.md"]

    with pytest.raises(PermissionDenied):
        service.sandbox_run(
            agent, "print(1)\n", "python", inputs={"paths": ["secret/plan.md"]}
        )
    with pytest.raises(PermissionDenied):
        service.sandbox_run(
            agent, "print(1)\n", "python", inputs={"prefix": "secret"}
        )


def test_host_symlink_and_out_symlink_do_not_leak(svc) -> None:
    service, _, agent = svc
    script = (
        "import os\n"
        "os.symlink('/root/host-secret', '/tmp/link')\n"
        "try:\n"
        "    open('/tmp/link').read()\n"
        "    print('LEAK')\n"
        "except OSError:\n"
        "    print('BLOCKED')\n"
        "os.symlink('/root/host-secret', '/out/leak.txt')\n"
    )
    result = service.sandbox_run(agent, script, "python")
    assert result["exit_code"] == 0, result["stderr"]
    assert "BLOCKED" in result["stdout"]
    assert "LEAK" not in result["stdout"]
    assert all(item["name"] != "leak.txt" for item in result["out"])


def test_bwrap_missing_refuses_to_run(svc) -> None:
    service, _, agent = svc
    sandbox = Sandbox(
        SandboxConfig(bwrap="/nonexistent/bwrap"), service.store, service.access
    )
    with pytest.raises(SandboxUnavailable):
        sandbox.run("print('must not run')", "python", agent)


def test_shell_language_and_output_file(svc) -> None:
    service, _, agent = svc
    result = service.sandbox_run(
        agent,
        "echo hello\necho problem >&2\necho data > /out/x.txt\n",
        "shell",
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert result["stdout"].strip() == "hello"
    assert result["stderr"].strip() == "problem"
    assert result["out"][0]["content"].strip() == "data"


def test_status_cancel_and_cache(svc) -> None:
    service, _, agent = svc
    sandbox = service.sandbox
    job_id = sandbox.submit(
        "import time\nwhile True:\n    time.sleep(0.05)\n",
        "python",
        agent,
        limits={"wall_seconds": 10},
    )
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            status = sandbox.status(agent, job_id)
            if status["status"] == "running":
                break
            time.sleep(0.02)
        assert status["status"] == "running"
        cancelled = sandbox.cancel(agent, job_id)
        assert cancelled["cancelled"] is True
        result = sandbox.wait(job_id)
        assert result["cancelled"] is True
        assert result["status"] == "cancelled"
        assert sandbox.status(agent, job_id)["status"] == "cancelled"
    finally:
        sandbox.cancel(agent, job_id)

    first = service.sandbox_run(agent, "print('cache-me')\n", "python", cache=True)
    second = service.sandbox_run(agent, "print('cache-me')\n", "python", cache=True)
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["stdout"] == first["stdout"]
