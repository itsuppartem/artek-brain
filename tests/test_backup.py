"""Резервные копии: согласованность, проверка, ротация и права."""

from __future__ import annotations

import io
import os
import tarfile
import threading
import time
from pathlib import Path

import pytest

from artek_brain import PermissionDenied
from artek_brain.backup import create_backup, verify_backup
from artek_brain.errors import StoredDataError
from artek_brain.service import Service


def _service(tmp_path: Path) -> tuple[Service, str, str]:
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    agent = str(service.access.create_token("agent", "agent", [("", "write")]))
    return service, owner, agent


def _audit_actions(service: Service) -> list[str]:
    with service.store._lock:
        rows = service.store._conn.execute(
            "SELECT action FROM audit ORDER BY id"
        ).fetchall()
    return [str(row["action"]) for row in rows]


def test_backup_create_and_verify(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path)
    try:
        service.write(agent, "docs/a.md", "# A\n\nпервый текст\n", "создал")
        service.write(agent, "docs/b.md", "# B\n\nвторой текст\n", "создал")
        (service.root / "settings.json").write_text('{"x": 1}', encoding="utf-8")
        output = tmp_path / "backups"
        result = service.backup_create(owner, output)
        archive = Path(result["archive"])
        manifest = Path(result["manifest"])
        assert archive.is_file() and manifest.is_file()
        assert os.stat(archive).st_mode & 0o777 == 0o600
        assert os.stat(manifest).st_mode & 0o777 == 0o600
        assert archive.name.startswith("brain-backup-")

        verified = service.backup_verify(owner, archive)
        assert verified["ok"] is True
        assert verified["documents"] == 2
        actions = _audit_actions(service)
        assert "backup_create" in actions
        assert "backup_verify" in actions
    finally:
        service.close()


def test_backup_during_parallel_writes_is_consistent(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path)
    try:
        service.write(agent, "docs/base.md", "# Base\n\nоснова\n", "создал")
        stop = threading.Event()

        def writer() -> None:
            index = 0
            while not stop.is_set() and index < 300:
                index += 1
                service.store.write(
                    f"docs/hot-{index}.md",
                    f"# Hot {index}\n\nтекст {index}\n",
                    "bob",
                    "нагрузка",
                )

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        time.sleep(0.02)
        result = create_backup(service.root, tmp_path / "backups")
        stop.set()
        thread.join(timeout=10)
        assert result["files"] >= 1
        verified = verify_backup(result["archive"])
        assert verified["ok"] is True
        assert verified["documents"] >= 1
    finally:
        service.close()


def test_verify_detects_corrupted_archive(tmp_path: Path) -> None:
    service, owner, _agent = _service(tmp_path)
    try:
        service.write(_agent, "docs/a.md", "# A\n\nцелый текст\n", "создал")
        result = create_backup(service.root, tmp_path / "backups")
        archive = Path(result["archive"])
        broken = tmp_path / "broken.tar.gz"
        with tarfile.open(archive, "r:gz") as source, tarfile.open(
            broken, "w:gz"
        ) as target:
            for member in source.getmembers():
                payload = source.extractfile(member) if member.isfile() else None
                if payload is not None and member.name.startswith("blobs/"):
                    data = payload.read()
                    data = b"Z" + data[1:]
                    info = tarfile.TarInfo(member.name)
                    info.size = len(data)
                    target.addfile(info, io.BytesIO(data))
                else:
                    target.addfile(member, payload)
        os.replace(broken, archive)
        with pytest.raises(StoredDataError):
            verify_backup(archive)
    finally:
        service.close()


def test_backup_rotation_keeps_last(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path)
    try:
        service.write(agent, "docs/a.md", "# A\n\nтекст\n", "создал")
        output = tmp_path / "backups"
        created = [
            Path(service.backup_create(owner, output, keep=1)["archive"])
            for _ in range(3)
        ]
        archives = sorted(output.glob("brain-backup-*.tar.gz"))
        manifests = sorted(output.glob("brain-backup-*.tar.gz.manifest.json"))
        assert len(archives) == 1
        assert len(manifests) == 1
        assert archives[0] == created[-1]
    finally:
        service.close()


def test_backup_requires_owner(tmp_path: Path) -> None:
    service, owner, agent = _service(tmp_path)
    try:
        service.write(agent, "docs/a.md", "# A\n\nтекст\n", "создал")
        with pytest.raises(PermissionDenied):
            service.backup_create(agent, tmp_path / "backups")
        result = service.backup_create(owner, tmp_path / "backups")
        with pytest.raises(PermissionDenied):
            service.backup_verify(agent, result["archive"])
    finally:
        service.close()
