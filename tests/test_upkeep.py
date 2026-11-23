"""Решения, зависимости, расхождения, задачи на обновление и предупреждения."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from artek_brain.cli import main
from artek_brain.errors import PermissionDenied, ValidationError
from artek_brain.mcp_server import BrainMCP
from artek_brain.service import Service
from artek_brain.upkeep import (
    UpkeepSettings,
    decision_links,
    extract_statements,
    find_rule_conflicts,
    slugify,
    split_sections,
)

DECISION = "# Решения\n\n## Релиз\n- Держим релиз 0.24 до большого запуска.\n\n## Цены\n- Без скидок.\n"


def _service(tmp_path: Path, settings: dict | None = None) -> tuple[Service, str, str]:
    root = tmp_path / "brain"
    root.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    service = Service(root)
    owner = str(service.access.create_token("owner", "owner"))
    agent = str(service.access.create_token("agent", "agent", [("", "write")]))
    return service, owner, agent


OPEN = {"decisions": {"require_owner": False}}


def _age(service: Service, path: str, days: int) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with service.store._conn:
        service.store._conn.execute("UPDATE versions SET ts=? WHERE path=?", (old, path))


def _version(service: Service, token: str, path: str) -> int:
    return int(service.read(token, path)["version"])


# Требование 1: решения утверждает владелец
def test_agent_cannot_change_decisions_by_default(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path)
    try:
        with pytest.raises(PermissionDenied):
            service.write(agent, "docs/decisions.md", DECISION, "решение")
        service.write(owner, "docs/decisions.md", DECISION, "решение")
        with pytest.raises(PermissionDenied):
            service.write(agent, "docs/decisions.md", DECISION + "x\n", "правка", base_version=1)
        with pytest.raises(PermissionDenied):
            service.delete(agent, "docs/decisions.md", "убрать")
    finally:
        service.close()


def test_agent_proposal_reaches_decisions_after_owner_approval(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path)
    try:
        service.write(owner, "docs/decisions.md", DECISION, "решение")
        proposal = service.propose(
            agent, "docs/decisions.md", DECISION + "\n## Новое\n- Правило.\n", "предлагаю"
        )
        assert "Новое" not in service.read(owner, "docs/decisions.md")["content"]
        assert service.coord_action(owner, "inbox", unread=True)
        service.proposals.approve(owner, proposal["path"], reason="ок")
        assert "Новое" in service.read(owner, "docs/decisions.md")["content"]
    finally:
        service.close()


def test_require_owner_can_be_disabled_only_explicitly(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path / "off", OPEN)
    try:
        assert service.write(agent, "docs/decisions.md", DECISION, "решение")["version"] == 1
    finally:
        service.close()
    strict, _, agent = _service(tmp_path / "typo", {"decisions": {"require_owner": "no"}})
    try:
        with pytest.raises(PermissionDenied):
            strict.write(agent, "docs/decisions.md", DECISION, "решение")
    finally:
        strict.close()


# Требование 2: зависимости и расхождения
def test_links_and_anchors() -> None:
    assert slugify("2026-10-19. Правки заказа") == "2026-10-19-правки-заказа"
    assert "релиз" in split_sections(DECISION)
    text = "см. [правило](../docs/decisions.md#релиз) и docs/decisions.md без якоря"
    assert decision_links("docs/flow.md", text, {"docs/decisions.md"}) == {
        ("docs/decisions.md", "релиз")
    }


def test_changed_decision_section_opens_task_only_for_dependents(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path, OPEN)
    try:
        service.write(agent, "docs/decisions.md", DECISION, "старт")
        service.write(agent, "docs/deploy.md", "# Выкатка\n\nСм. docs/decisions.md#релиз\n", "d")
        service.write(agent, "docs/price.md", "# Цены\n\nСм. docs/decisions.md#цены\n", "p")
        service.write(agent, "docs/nav.md", "# Карта\n\nСм. docs/decisions.md\n", "n")
        service.upkeep.sync(force=True)
        assert service.upkeep.tasks() == []
        service.write(
            agent, "docs/decisions.md", DECISION.replace("Держим", "Выпускаем"), "смена",
            base_version=1,
        )
        service.upkeep.sync(force=True)
        tasks = service.upkeep.tasks()
        assert [(item["path"], item["kind"]) for item in tasks] == [
            ("docs/deploy.md", "decision_changed")
        ]
        service.upkeep.sync(force=True)
        assert len(service.upkeep.tasks()) == 1
    finally:
        service.close()


@pytest.mark.parametrize(
    ("rule", "other", "kind"),
    [
        ("Держим выкатку кассы 0.24 до большого релиза.", "Шаг 3: выкатить кассу 0.24 на прод.", "stance"),
        ("Hold the 0.24 release until the big launch.", "Step 3: deploy the 0.24 release to production.", "stance"),
        ("Порог залога для кассира 50 000 RSD.", "Порог залога для кассира 40 000 RSD.", "value"),
        ("Нельзя выкатывать виджет оплаты без проверки бухгалтерии.", "Виджет оплаты можно выкатывать без проверки бухгалтерии.", "stance"),
    ],
)
def test_text_conflict_without_links(rule: str, other: str, kind: str) -> None:
    found = find_rule_conflicts(
        {"docs/decisions.md": f"# Р\n\n## А\n- {rule}\n", "docs/flow.md": f"# П\n\n- {other}\n"},
        UpkeepSettings(),
    )
    assert [(item["path"], item["kind"]) for item in found] == [("docs/flow.md", kind)]


@pytest.mark.parametrize(
    ("rule", "other"),
    [
        ("Держим релиз 0.24 до запуска.", "Выкатить релиз 0.25 на прод."),
        ("Держим релиз 0.24 до запуска.", "Релиз 0.24 описан в журнале изменений."),
        ("Держим релиз 0.24 (отменено 20.11).", "Выкатить релиз 0.24 на прод."),
        ("Порог залога 50 000 RSD.", "Лимит кассы вечером 40 000 RSD."),
    ],
)
def test_no_false_conflict(rule: str, other: str) -> None:
    found = find_rule_conflicts(
        {"docs/decisions.md": f"# Р\n\n- {rule}\n", "docs/flow.md": f"# П\n\n- {other}\n"},
        UpkeepSettings(),
    )
    assert found == []


def test_paraphrase_is_a_documented_miss() -> None:
    """Перефразирование детектор не ловит: это записано в документации."""
    found = find_rule_conflicts(
        {
            "docs/decisions.md": "# Р\n\n- Держим большой релиз до конца месяца.\n",
            "docs/flow.md": "# П\n\n- Новую сборку отправляем клиентам сегодня.\n",
        },
        UpkeepSettings(),
    )
    assert found == []


def test_cancelled_statement_is_skipped() -> None:
    assert extract_statements("d.md", "- Держим релиз 0.24 (отменено 16.11)\n") == []


def test_conflict_task_and_confirmation_is_remembered(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path, OPEN)
    try:
        service.write(agent, "docs/decisions.md", "# Р\n\n- Держим релиз 0.24 до запуска.\n", "р")
        service.write(agent, "docs/deploy.md", "# В\n\n- Выкатить релиз 0.24 на прод.\n", "в")
        service.upkeep.sync(force=True)
        task = service.upkeep.tasks(path="docs/deploy.md")[0]
        assert task["kind"] == "rule_conflict"
        service.confirm_fresh(agent, "docs/deploy.md", "это другой контур, сверил")
        service.upkeep.sync(force=True)
        assert service.upkeep.tasks(path="docs/deploy.md") == []
    finally:
        service.close()


# Требование 3: гарантия обновления
def test_stale_task_persists_until_update(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path, OPEN)
    try:
        service.write(agent, "docs/a.md", "# A\n", "старт")
        _age(service, "docs/a.md", 99)
        for _ in range(3):
            service.upkeep.sync(force=True)
        assert [item["kind"] for item in service.upkeep.tasks(path="docs/a.md")] == ["stale"]
        service.write(agent, "docs/a.md", "# A\n\nсвежее\n", "обновил", base_version=1)
        service.upkeep.sync(force=True)
        assert service.upkeep.tasks(path="docs/a.md") == []
        done = service.upkeep.tasks(path="docs/a.md", status="resolved")
        assert done[0]["resolution"] == "updated"
    finally:
        service.close()


def test_confirm_needs_reason_and_resets_clock(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path, OPEN)
    try:
        service.write(agent, "docs/a.md", "# A\n", "старт")
        _age(service, "docs/a.md", 99)
        service.upkeep.sync(force=True)
        with pytest.raises(ValidationError):
            service.confirm_fresh(agent, "docs/a.md", "ок")
        result = service.confirm_fresh(agent, "docs/a.md", "сверил с сервером, верно")
        assert result["confirmed_tasks"]
        assert "freshness" not in service.read(agent, "docs/a.md")
        service.upkeep.sync(force=True)
        assert service.upkeep.tasks(path="docs/a.md") == []
    finally:
        service.close()


def test_task_escalates_to_owner_once_and_does_not_block_writes(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path, {**OPEN, "upkeep": {"escalate_days": 2}})
    try:
        service.write(agent, "docs/a.md", "# A\n", "старт")
        _age(service, "docs/a.md", 99)
        start = datetime.now(timezone.utc)
        service.upkeep.sync(force=True, now=start)
        assert service.coord_action(owner, "inbox", unread=True) == []
        later = start + timedelta(days=3)
        assert service.upkeep.sync(force=True, now=later)["escalated"] == 1
        assert service.upkeep.sync(force=True, now=later)["escalated"] == 0
        mail = service.coord_action(owner, "inbox", unread=True)
        assert len(mail) == 1 and "docs/a.md" in mail[0]["body"]
        assert service.write(agent, "docs/b.md", "# B\n", "работа")["version"] == 1
    finally:
        service.close()


def test_brief_shows_open_tasks(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path, OPEN)
    try:
        service.write(agent, "docs/a.md", "# A\n", "старт")
        _age(service, "docs/a.md", 99)
        brief = service.coord_action(agent, "brief")["brief"]
        assert "Задачи на обновление памяти: 1" in brief and "docs/a.md" in brief
    finally:
        service.close()


def test_notes_do_not_get_persistent_tasks(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path, OPEN)
    try:
        service.write(agent, "notes/a.md", "# A\n", "старт")
        _age(service, "notes/a.md", 200)
        service.upkeep.sync(force=True)
        assert service.upkeep.tasks() == []
        assert service.read(agent, "notes/a.md")["freshness"]["status"] == "stale"
    finally:
        service.close()


# Требование 4: предупреждение при чтении и поиске
def test_warning_in_mcp_read_and_search_with_audit(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path, OPEN)
    app = BrainMCP(service, agent)
    try:
        service.write(agent, "docs/a.md", "# A\n\nуникальноеслово\n", "старт")
        fresh = json.loads(app.call("brain_read", {"path": "docs/a.md"})[0])
        assert "freshness" not in fresh
        _age(service, "docs/a.md", 99)
        data = json.loads(app.call("brain_read", {"path": "docs/a.md"})[0])
        assert data["freshness"]["status"] == "stale" and "confirm_fresh" in data["freshness"]["fix"]
        found = json.loads(app.call("brain_search", {"query": "уникальноеслово"})[0])
        assert found["items"][0]["stale"] is True and found["stale_paths"] == ["docs/a.md"]
        surfaces = [
            json.loads(row["detail_json"])["surface"]
            for row in service.store._conn.execute(
                "SELECT detail_json FROM audit WHERE action='stale_warning'"
            )
        ]
        assert {"read", "search"} <= set(surfaces)
    finally:
        service.close()


def test_warning_survives_truncated_mcp_read(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path, OPEN)
    app = BrainMCP(service, agent, max_response_chars=3000)
    try:
        body = "# A\n\n" + "".join(f"## Р{i}\n" + "строка\n" * 30 for i in range(12))
        service.write(agent, "docs/a.md", body, "старт")
        _age(service, "docs/a.md", 99)
        data = json.loads(app.call("brain_read", {"path": "docs/a.md"})[0])
        assert data["truncated"] is True and data["freshness"]["status"] == "stale"
    finally:
        service.close()


def test_old_version_read_has_no_warning(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path, OPEN)
    try:
        service.write(agent, "docs/a.md", "# A\n", "старт")
        _age(service, "docs/a.md", 99)
        assert "freshness" not in service.read(agent, "docs/a.md", version=1)
    finally:
        service.close()


def test_cli_warning_goes_to_stderr(tmp_path: Path, capsys, monkeypatch) -> None:
    service, _, agent = _service(tmp_path, OPEN)
    service.write(agent, "docs/a.md", "# A\n", "старт")
    _age(service, "docs/a.md", 99)
    root = service.root
    service.close()
    monkeypatch.setenv("BRAIN_TOKEN", agent)
    capsys.readouterr()
    assert main(["--root", str(root), "read", "docs/a.md"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "# A\n" and "[устарело]" in captured.err


def test_http_read_has_warning(tmp_path: Path) -> None:
    import threading
    import urllib.request

    from artek_brain.http_api import BrainHTTPServer

    service, _, agent = _service(tmp_path, OPEN)
    service.write(agent, "docs/a.md", "# A\n", "старт")
    _age(service, "docs/a.md", 99)
    server = BrainHTTPServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/documents/docs/a.md",
            headers={"Authorization": f"Bearer {agent}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert json.load(response)["freshness"]["status"] == "stale"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        service.close()


# Шум и авторство
def test_lagging_counts_only_notes_that_mention_doc(tmp_path: Path) -> None:
    from artek_brain.aging import freshness

    service, _, agent = _service(tmp_path, OPEN)
    try:
        service.write(agent, "docs/a.md", "# A\n", "старт")
        service.write(agent, "docs/b.md", "# B\n", "старт")
        _age(service, "docs/a.md", 2)
        _age(service, "docs/b.md", 2)
        service.write(agent, "notes/1.md", "# Н\n\nправил docs/a.md\n", "итог")
        service.write(agent, "notes/2.md", "# Н\n\nпро a.md\n", "итог")
        service.write(agent, "notes/3.md", "# Н\n\nсовсем другое\n", "итог")
        items = {item["path"]: item for item in freshness(service.store)}
        assert items["docs/a.md"]["notes_after"] == 2
        assert items["docs/b.md"]["notes_after"] == 0
    finally:
        service.close()


def test_authorship_report(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path, OPEN)
    try:
        service.write(owner, "README.md", "# R\n", "brain setup: старт")
        service.write(owner, "docs/o.md", "# O\n", "правка", session="s1")
        service.write(agent, "docs/a.md", "# A\n", "сам")
        service.write(agent, "docs/b.md", "# B\n", "по делу", on_behalf="owner")
        report = service.authorship(owner)
        assert report == {
            "total": 4, "agent": 1, "agent_on_request": 1, "owner_token_setup": 1,
            "owner_token_by_agent": 1, "owner_token_unknown": 0,
        }
    finally:
        service.close()
