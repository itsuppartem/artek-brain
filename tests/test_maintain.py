"""Обслуживание хранилища: checkpoint, gc, repack и busy-процессы."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from artek_brain import PermissionDenied
from artek_brain.service import Service


def _service(tmp_path: Path) -> tuple[Service, str, str]:
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    agent = str(service.access.create_token("agent", "agent", [("", "write")]))
    return service, owner, agent


def test_maintain_owner_cleans_and_reports(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path)
    try:
        service.write(agent, "docs/a.md", "# A\n\nтекст\n", "создал")
        orphan = service.root / "blobs" / "ff" / ("a" * 62)
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"x" * (1024 * 1024))

        report = service.maintain(owner)
        names = {item["database"] for item in report["checkpoints"]}
        assert {"brain.sqlite3", "access.sqlite3", "search.sqlite3"} <= names
        assert report["action"] == "maintain"
        assert report["gc"]["removed"] >= 1
        assert report["freed_bytes"] >= 1024 * 1024
        assert "maintain" in [
            str(row["action"])
            for row in service.store._conn.execute(
                "SELECT action FROM audit"
            ).fetchall()
        ]
        with pytest.raises(PermissionDenied):
            service.maintain(agent)
    finally:
        service.close()


def test_maintain_tolerates_busy_database(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path)
    reader = sqlite3.connect(str(service.root / "brain.sqlite3"))
    try:
        service.write(agent, "docs/a.md", "# A\n\nтекст\n", "создал")
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM versions").fetchone()
        # Запись после снимка читателя: эти кадры WAL нельзя перенести,
        # пока читатель держит снимок (после каждой записи идёт PASSIVE
        # checkpoint, поэтому без второй записи WAL уже пуст).
        service.write(agent, "docs/b.md", "# B\n\nещё\n", "вторая запись")
        report = service.maintain(owner)
        brain = next(
            item
            for item in report["checkpoints"]
            if item["database"] == "brain.sqlite3"
        )
        assert brain["busy"] is True
        assert brain["ok"] is True
        assert reader.execute("SELECT COUNT(*) FROM versions").fetchone()[0] >= 1
        assert service.read(owner, "docs/a.md")["content"].endswith("текст\n")
    finally:
        reader.rollback()
        reader.close()
        service.close()
