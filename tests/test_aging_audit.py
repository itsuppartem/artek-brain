"""Старение знаний, журнал сессий в MCP, поиск, обрезка, setup и хук."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from artek_brain.aging import freshness, lagging_docs, load_aging
from artek_brain.cli import main
from artek_brain.errors import ValidationError
from artek_brain.mcp_server import BrainMCP
from artek_brain.project import MARK_BEGIN, MARK_END, setup_project
from artek_brain.quality import Quality
from artek_brain.service import Service
from artek_brain.store import SCHEMA_VERSION, Store


def _service(tmp_path: Path) -> tuple[Service, str, str]:
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    agent = str(service.access.create_token("agent", "agent", [("", "write")]))
    return service, owner, agent


def _audit(service: Service, action: str) -> list[sqlite3.Row]:
    return service.store._conn.execute(
        "SELECT * FROM audit WHERE action=? ORDER BY id", (action,)
    ).fetchall()


def test_mcp_logs_session_and_client_and_search(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BRAIN_SESSION", "s-123")
    monkeypatch.setenv("BRAIN_CLIENT", "opencode")
    service, _, agent = _service(tmp_path)
    app = BrainMCP(service, agent)
    try:
        _, error = app.call(
            "brain_write",
            {"path": "docs/prod.md", "content": "# Прод\n\nвыкладка образа\n", "reason": "старт"},
        )
        assert error is False
        app.call("brain_read", {"path": "docs/prod.md"})
        text, error = app.call("brain_search", {"query": "выкладка", "type": "doc"})
        assert error is False, text
        for action in ("write", "read", "search"):
            rows = _audit(service, action)
            assert rows, action
            assert rows[-1]["session"] == "s-123"
            assert "opencode" in rows[-1]["model"]
        detail = json.loads(_audit(service, "search")[-1]["detail_json"])
        assert detail["query"] == "выкладка"
        assert "hits" in detail
    finally:
        service.close()


def test_client_from_initialize_when_env_is_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BRAIN_CLIENT", raising=False)
    monkeypatch.delenv("BRAIN_MODEL", raising=False)
    service, _, agent = _service(tmp_path)
    app = BrainMCP(service, agent)
    try:
        app.set_client("claude-code", "2.1")
        app.call(
            "brain_write",
            {"path": "notes/a.md", "content": "# A\n", "reason": "проверка"},
        )
        assert _audit(service, "write")[-1]["model"] == "claude-code/2.1"
        assert _audit(service, "write")[-1]["session"]
    finally:
        service.close()


def test_search_falls_back_to_any_word(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path)
    try:
        service.write(agent, "docs/prod.md", "# Прод\n\nВыкатка образа на сервер.\n", "старт")
        strict = service.search(agent, "выкатка")
        assert strict["total"] == 1 and strict["match"] == "all"
        loose = service.search(agent, "выкатки деплой докер прод")
        assert loose["total"] >= 1
        assert loose["match"] == "any"
        assert "note" in loose
        empty = service.search(agent, "совсемнетслова")
        assert empty["total"] == 0
    finally:
        service.close()


def test_long_read_shows_remaining_sections(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path)
    app = BrainMCP(service, agent, max_response_chars=3000)
    try:
        body = "# Грабли\n\n" + "".join(
            f"## Раздел {index}\n" + ("строка граблей\n" * 20) for index in range(10)
        )
        service.write(agent, "docs/gotchas.md", body, "длинно")
        text, error = app.call("brain_read", {"path": "docs/gotchas.md"})
        assert error is False
        assert len(text) <= 3000
        data = json.loads(text)
        assert data["truncated"] is True
        assert data["next"]["line_start"] > 1
        titles = [item["title"] for item in data["remaining_sections"]]
        assert "Раздел 9" in titles
        tail, error = app.call(
            "brain_read",
            {"path": "docs/gotchas.md", "line_start": data["next"]["line_start"]},
        )
        assert error is False
        assert "строка граблей" in json.loads(tail)["content"]
    finally:
        service.close()


def test_ttl_settings_and_lagging(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path)
    try:
        (service.root / "settings.json").write_text(
            json.dumps({"aging": {"ttl_days": {"docs/": 5}}, "brief": {"paths": ["status.md"]}}),
            encoding="utf-8",
        )
        settings = load_aging(service.root)
        assert settings.ttl_for("docs/prod.md") == 7  # точный ключ длиннее папки
        assert settings.ttl_for("docs/arch.md") == 5
        assert settings.ttl_for("notes/x.md") == 90
        assert settings.ttl_for("other.md") == 60
        assert settings.brief_paths == ("status.md",)

        service.write(agent, "docs/arch.md", "# Арх\n", "старт")
        for index in range(3):
            service.write(agent, f"notes/n{index}.md", f"# Заметка {index}\n", "итог")
        later = datetime.now(timezone.utc) + timedelta(days=6)
        items = {item["path"]: item for item in freshness(service.store, now=later)}
        assert items["docs/arch.md"]["overdue"] is True
        assert items["docs/arch.md"]["notes_after"] == 3
        assert items["notes/n0.md"]["overdue"] is False
        assert [item["path"] for item in lagging_docs(list(items.values()))] == ["docs/arch.md"]

        report = Quality(service.brain).report(owner)
        assert report["freshness_source"] == "ttl"
        assert {"path": "docs/arch.md", "notes_after": 3} in report["docs_lagging"] or (
            report["docs_lagging"] == []  # сейчас возраст < 1 дня
        )
    finally:
        service.close()


def test_brief_warns_about_stale_docs(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path)
    try:
        service.write(owner, "README.md", "# Проект\n", "старт")
        service.write(agent, "docs/prod.md", "# Прод\n", "старт")
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        with service.store._conn:
            service.store._conn.execute(
                "UPDATE versions SET ts=? WHERE path IN ('docs/prod.md', 'README.md')",
                (old,),
            )
        for index in range(4):
            service.write(agent, f"notes/n{index}.md", f"# Итог {index}\n", "итог")
        brief = service.coord_action(agent, "brief")["brief"]
        assert "Ориентир: README.md" in brief
        assert "Проверь перед опорой" in brief
        assert "docs/prod.md" in brief
        assert "после неё 4 заметок" in brief
        assert len(brief) <= 3000
    finally:
        service.close()


def test_sentence_with_number_is_not_a_fact(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path)
    try:
        service.write(agent, "docs/a.md", "# A\n\n- Прод: порт 1703 → 3100 в каталоге\n", "a")
        service.write(agent, "notes/b.md", "# B\n\n- Прод: сервер `node-36`, папка\n", "b")
        service.write(agent, "docs/c.md", "# C\n\n- Порог: 50 000 RSD\n", "c")
        service.write(agent, "notes/d.md", "# D\n\n- Порог: 40 000 RSD\n", "d")
        found = Quality(service.brain).contradictions(owner)
        keys = [item.key for item in found]
        assert "Порог" in keys
        assert not any(key.startswith("Прод") for key in keys)
    finally:
        service.close()


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    store = Store(root)
    store.close()
    conn = sqlite3.connect(str(root / "brain.sqlite3"))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(ValidationError):
        Store(root)


def test_cli_refuses_missing_root_and_hook_is_silent(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nope" / ".brain"
    assert main(["--root", str(missing), "stats"]) == 1
    assert not missing.exists()
    assert main(["--root", str(missing), "hook", "session-start"]) == 0
    assert capsys.readouterr().out == ""


def test_setup_upgrades_rules_clients_and_hook(tmp_path: Path, capsys, monkeypatch) -> None:
    project = tmp_path / "shop"
    project.mkdir()
    (project / "AGENTS.md").write_text(
        f"# Свои правила\n\n{MARK_BEGIN}\nстарый блок\n{MARK_END}\n\nхвост\n",
        encoding="utf-8",
    )
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"brain": {"command": "brain", "args": ["mcp"]}}}),
        encoding="utf-8",
    )
    result = setup_project(project, name="Магазин")
    agents = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.startswith("# Свои правила")
    assert agents.rstrip().endswith("хвост")
    assert "старый блок" not in agents
    assert 'brain_op(action="brief")' in agents
    assert agents.count(MARK_BEGIN) == 1
    mcp = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["brain"]["env"]["BRAIN_CLIENT"] == "claude-code"
    opencode = json.loads((project / "opencode.json").read_text(encoding="utf-8"))
    assert opencode["mcp"]["brain"]["environment"]["BRAIN_CLIENT"] == "opencode"
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    group = settings["hooks"]["SessionStart"][0]
    assert "compact" in group["matcher"]
    assert group["hooks"][0]["command"].endswith("hook session-start")
    assert ".claude/settings.json" in result["created"]

    again = setup_project(project)
    assert "AGENTS.md" not in again["created"]
    assert ".claude/settings.json" not in again["created"]

    monkeypatch.chdir(project)
    monkeypatch.delenv("BRAIN_TOKEN", raising=False)
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    capsys.readouterr()
    assert main(["hook", "session-start"]) == 0
    out = capsys.readouterr().out
    assert "Бриф" in out and "README.md" in out
