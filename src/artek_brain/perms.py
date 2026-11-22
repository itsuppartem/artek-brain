"""Права на файлы хранилища: содержимое памяти читает только владелец."""

from __future__ import annotations

from pathlib import Path


def restrict_db(db_path: str | Path) -> None:
    """Права 600 на файл SQLite и его -wal/-shm."""
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(db_path) + suffix)
        try:
            if target.exists():
                target.chmod(0o600)
        except OSError:
            continue


def restrict_dir(path: str | Path) -> None:
    """Права 700 на папку (например, blobs/ с содержимым документов)."""
    try:
        target = Path(path)
        if target.is_dir():
            target.chmod(0o700)
    except OSError:
        pass
