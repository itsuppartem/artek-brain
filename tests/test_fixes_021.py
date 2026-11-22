"""Исправления 0.2.1: курсор changes, diff конфликта, права, gc, overwrite, review_by."""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from artek_brain.aging import freshness
from artek_brain.cli import main
from artek_brain.errors import ConflictError
from artek_brain.mcp_server import BrainMCP
from artek_brain.quota import Quota
from artek_brain.service import Service
from artek_brain.store import Store


def _service(tmp_path: Path) -> tuple[Service, str, str]:
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    agent = str(service.access.create_token("agent", "agent", [("", "write")]))
    return service, owner, agent


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_changes_cursor_reaches_the_feed(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path)
    app = BrainMCP(service, agent)
    try:
        for index in range(5):
            service.write(agent, f"notes/n{index}.md", f"# N{index}\n", "итог")
        first, error = app.call("brain_op", {"action": "changes", "cursor": 0, "limit": 2})
        assert error is False
        data = json.loads(first)
        assert len(data["items"]) == 2
        nxt = data["next_cursor"]
        second, error = app.call("brain_op", {"action": "changes", "cursor": nxt, "limit": 2})
        assert error is False
        page = json.loads(second)
        assert "items" in page, page
        assert {item["path"] for item in page["items"]}.isdisjoint(
            {item["path"] for item in data["items"]}
        )
    finally:
        service.close()


def test_changes_long_answer_uses_response_cursor(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path)
    app = BrainMCP(service, agent, max_response_chars=600)
    try:
        for index in range(10):
            service.write(agent, f"notes/n{index}.md", f"# N{index}\n", "итог " * 5)
        text, error = app.call("brain_op", {"action": "changes", "cursor": 0})
        assert error is False
        head = json.loads(text)
        assert head["truncated"] is True and head["response_cursor"]
        rest, _ = app.call(
            "brain_op",
            {"action": "changes", "cursor": 0, "response_cursor": head["response_cursor"]},
        )
        assert json.loads(rest)["chunk"]
    finally:
        service.close()


def test_conflict_returns_diff_everywhere(tmp_path: Path, capsys, monkeypatch) -> None:
    service, _, agent = _service(tmp_path)
    app = BrainMCP(service, agent)
    try:
        service.write(agent, "docs/a.md", "# A\n\n## Раздел\nстарое\n", "старт")
        service.write(agent, "docs/a.md", "# A\n\n## Раздел\nправка один\n", "1", base_version=1)
        text, error = app.call(
            "brain_write",
            {"path": "docs/a.md", "content": "# A\n\n## Раздел\nправка два\n",
             "reason": "2", "base_version": 1},
        )
        assert error is True
        data = json.loads(text)
        assert data["conflict"]["current_version"] == 2
        assert "правка два" in data["conflict"]["diff"]
        try:
            service.write(agent, "docs/a.md", "# A\n\n## Раздел\nтри\n", "3", base_version=1)
        except ConflictError as exc:
            assert exc.details()["diff"]
    finally:
        service.close()


def test_cli_conflict_json_has_diff(tmp_path: Path, capsys, monkeypatch) -> None:
    root = tmp_path / "brain"
    service, owner, _ = _service(tmp_path)
    service.write(owner, "docs/a.md", "# A\n\n## Р\nстарое\n", "старт")
    service.write(owner, "docs/a.md", "# A\n\n## Р\nодин\n", "1", base_version=1)
    service.close()
    source = tmp_path / "new.md"
    source.write_text("# A\n\n## Р\nдва\n", encoding="utf-8")
    monkeypatch.setenv("BRAIN_TOKEN", owner)
    capsys.readouterr()
    code = main(["--root", str(root), "write", "docs/a.md", "--file", str(source),
                 "--reason", "2", "--base-version", "1", "--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert "два" in payload["conflict"]["diff"]


def test_index_and_access_files_are_private(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path)
    try:
        service.write(agent, "docs/a.md", "# A\n\nтекст\n", "старт")
        service.search(agent, "текст")
        root = service.root
        for name in ("brain.sqlite3", "search.sqlite3", "access.sqlite3"):
            assert _mode(root / name) == 0o600, name
        assert _mode(root / "blobs") == 0o700
    finally:
        service.close()


def test_gc_keeps_fresh_unreferenced_blob(tmp_path: Path) -> None:
    store = Store(tmp_path / "brain")
    try:
        orphan = store.blobs_dir / "ab" / ("c" * 62)
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"x")
        assert store.gc().removed == 0
        assert orphan.exists()
        old = time.time() - 7200
        os.utime(orphan, (old, old))
        assert store.gc().removed == 1
    finally:
        store.close()


def test_owner_overwrite_leaves_audit_row(tmp_path: Path) -> None:
    service, owner, _ = _service(tmp_path)
    try:
        service.write(owner, "docs/a.md", "# A\n\nбыло\n", "старт")
        service.overwrite(owner, "docs/a.md", "# A\n\nстало\n")
        rows = service.store._conn.execute(
            "SELECT * FROM audit WHERE action='overwrite'"
        ).fetchall()
        assert len(rows) == 1
        detail = json.loads(rows[0]["detail_json"])
        assert detail["old_sha256"] != detail["new_sha256"]
        assert rows[0]["path"] == "docs/a.md"
    finally:
        service.close()


def test_review_by_beats_ttl(tmp_path: Path) -> None:
    service, _, agent = _service(tmp_path)
    try:
        soon = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        service.write(agent, "docs/a.md", f"---\nreview_by: {soon}\n---\n# A\n", "старт")
        service.write(agent, "docs/b.md", "# B\n", "старт")
        later = datetime.now(timezone.utc) + timedelta(days=3)
        items = {item["path"]: item for item in freshness(service.store, now=later)}
        assert items["docs/a.md"]["overdue"] is True  # review_by прошёл, TTL docs/ ещё нет
        assert items["docs/a.md"]["review_by"]
        assert items["docs/b.md"]["overdue"] is False
    finally:
        service.close()


def test_quota_from_settings(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    root.mkdir()
    (root / "settings.json").write_text(
        json.dumps({"quota": {"max_document_bytes": 2048, "max_disk_bytes": "x"}}),
        encoding="utf-8",
    )
    quota = Quota.from_settings(root)
    assert quota.max_document_bytes == 2048
    assert quota.max_disk_bytes is None
    store = Store(root)
    try:
        assert store.quota.max_document_bytes == 2048
    finally:
        store.close()


def test_token_revoke_prints_text(tmp_path: Path, capsys, monkeypatch) -> None:
    root = tmp_path / "brain"
    service, owner, _ = _service(tmp_path)
    service.close()
    monkeypatch.setenv("BRAIN_TOKEN", owner)
    capsys.readouterr()
    assert main(["--root", str(root), "token", "revoke", "--name", "agent"]) == 0
    assert "отозвано токенов: 1" in capsys.readouterr().out
