"""Обслуживание хранилища: WAL checkpoint, gc, repack и отчёт о месте."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .store import Store


def checkpoint_databases(
    root: str | Path, *, busy_timeout_ms: int = 500
) -> list[dict[str, Any]]:
    """Сделать wal_checkpoint(TRUNCATE) во всех базах; busy — не ошибка."""
    report: list[dict[str, Any]] = []
    for database in sorted(Path(root).glob("*.sqlite3")):
        if not database.is_file():
            continue
        item: dict[str, Any] = {"database": database.name, "ok": True, "busy": False}
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(database), timeout=30.0)
            connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row is not None:
                item["busy"] = bool(row[0])
                item["log_frames"] = int(row[1]) if len(row) > 1 else 0
                item["checkpointed"] = int(row[2]) if len(row) > 2 else 0
        except sqlite3.Error as exc:
            item["ok"] = False
            item["error"] = str(exc)
        finally:
            if connection is not None:
                connection.close()
        report.append(item)
    return report


def maintain(store: Store, actor: str | None = None) -> dict[str, Any]:
    """Почистить WAL, перепаковать, собрать мусор и вернуть отчёт."""
    root = Path(store.root)
    checkpoints = checkpoint_databases(root)
    before = store.disk_report()
    # Сначала repack: он освобождает блобы, которые затем убирает gc в том же запуске.
    repack_result = store.repack(actor=actor).as_dict()
    gc_result = store.gc(actor=actor).as_dict()
    after = store.disk_report()
    repack_saved = max(
        0,
        int(repack_result["blob_bytes_before"])
        - int(repack_result["blob_bytes_after"]),
    )
    freed = int(gc_result["freed_bytes"]) + repack_saved
    result = {
        "action": "maintain",
        "checkpoints": checkpoints,
        "busy": sum(1 for item in checkpoints if item.get("busy")),
        "gc": gc_result,
        "repack": repack_result,
        "freed_bytes": freed,
        "disk_bytes_before": int(before["disk_bytes"]),
        "disk_bytes_after": int(after["disk_bytes"]),
        "disk_report": after,
    }
    with store._lock:
        store._ensure_open()
        with store._conn:
            store._audit_ok(actor, "maintain", None, result)
    return result
