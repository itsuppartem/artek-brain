"""brain setup: развёртывание мозга внутри проекта."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from artek_brain.cli import main
from artek_brain.project import setup_project
from artek_brain.service import Service


def test_setup_creates_instance_and_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    project.mkdir()
    (project / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8"
    )

    first = setup_project(project, name="Магазин")

    root = project / ".brain"
    agent_file = root / "agent.token"
    owner_file = root / "owner.token"
    assert stat.S_IMODE(agent_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(owner_file.stat().st_mode) == 0o600
    assert ".brain/" in (project / ".gitignore").read_text(encoding="utf-8")
    servers = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "other" in servers
    assert servers["brain"]["args"][-1] == "mcp"
    opencode = json.loads((project / "opencode.json").read_text(encoding="utf-8"))
    assert opencode["mcp"]["brain"]["command"][-1] == "mcp"
    assert "BEGIN:artek-brain" in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert (project / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    assert "память:README.md" in first["created"]

    agent_token = agent_file.read_text(encoding="utf-8").strip()
    service = Service(root)
    try:
        assert service.principal(agent_token).role == "agent"
        readme = service.store.read("README.md")
        assert "Магазин" in str(getattr(readme, "content", readme))
    finally:
        service.close()

    second = setup_project(project)
    assert second["created"] == []
    assert agent_file.read_text(encoding="utf-8").strip() == agent_token
    assert (project / "AGENTS.md").read_text(encoding="utf-8").count("BEGIN:artek-brain") == 1


def test_cli_uses_agent_token_file(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("BRAIN_TOKEN", raising=False)
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    project = tmp_path / "p"
    project.mkdir()
    assert main(["setup", str(project)]) == 0
    capsys.readouterr()

    monkeypatch.chdir(project)
    assert main(["read", "backlog.md"]) == 0
    assert "Бэклог" in capsys.readouterr().out

    # агент не может менять README.md — это область владельца
    assert main(["write", "README.md", "--content", "x", "--reason", "проба"]) != 0


def test_root_is_found_from_nested_worktree(tmp_path: Path, monkeypatch) -> None:
    from artek_brain.service import resolve_root

    monkeypatch.delenv("BRAIN_HOME", raising=False)
    (tmp_path / ".brain").mkdir()
    nested = tmp_path / "repo" / ".agents-wt" / "TASK-1"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert resolve_root() == tmp_path / ".brain"
