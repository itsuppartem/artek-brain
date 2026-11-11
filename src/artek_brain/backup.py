"""Резервные копии хранилища Artek Brain: tar.gz, манифест и проверка.

Копия снимается на работающем хранилище: SQLite-базы копируются через
Connection.backup, блобы — по неизменяемым хеш-именам. Модуль без сети и LLM.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import NotFoundError, StoredDataError, ValidationError
from .store import Store

_ARCHIVE_PREFIX = "brain-backup-"
_MANIFEST_SUFFIX = ".manifest.json"
_CHUNK = 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _snapshot_sqlite(source: Path, target: Path) -> None:
    """Согласованно скопировать SQLite-базу через backup API."""
    src = sqlite3.connect(str(source), timeout=30.0)
    try:
        src.execute("PRAGMA busy_timeout=30000")
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _copy_tree(source: Path, target: Path) -> list[Path]:
    """Скопировать каталог, пропуская исчезнувшие файлы (например, после gc)."""
    copied: list[Path] = []
    if not source.exists():
        return copied
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(item, destination)
        except FileNotFoundError:
            continue
        copied.append(destination)
    return copied


def _stage_store(root: Path, stage: Path) -> list[str]:
    """Разложить копию хранилища во временном каталоге, вернуть имена баз."""
    databases: list[str] = []
    for source in sorted(root.glob("*.sqlite3")):
        if not source.is_file():
            continue
        _snapshot_sqlite(source, stage / source.name)
        databases.append(source.name)
    _copy_tree(root / "blobs", stage / "blobs")
    settings = root / "settings.json"
    if settings.is_file():
        shutil.copy2(settings, stage / "settings.json")
    _copy_tree(root / "tokens", stage / "tokens")
    return databases


def _archive_files(stage: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for item in sorted(stage.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(stage).as_posix()
        files[relative] = {
            "sha256": _sha256_file(item),
            "size": item.stat().st_size,
        }
    return files


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _rotate(output: Path, keep: int) -> list[str]:
    archives = sorted(
        output.glob(f"{_ARCHIVE_PREFIX}*.tar.gz"), key=lambda item: item.name
    )
    removed: list[str] = []
    for old in archives[:-keep]:
        try:
            old.unlink()
        except FileNotFoundError:
            continue
        removed.append(old.name)
        try:
            old.with_name(old.name + _MANIFEST_SUFFIX).unlink()
        except FileNotFoundError:
            pass
    return removed


def create_backup(
    root: str | Path,
    output: str | Path,
    *,
    keep: int | None = None,
) -> dict[str, Any]:
    """Создать согласованный tar.gz с базой, блобами и настройками."""
    root_path = Path(root)
    if not root_path.is_dir():
        raise ValidationError(
            f"Хранилище «{root_path}» не найдено.",
            hint="Передайте каталог существующего хранилища.",
        )
    if keep is not None and (
        not isinstance(keep, int) or isinstance(keep, bool) or keep < 1
    ):
        raise ValidationError(
            "keep должен быть целым числом >= 1.",
            hint="Передайте --keep 5, чтобы хранить пять последних архивов.",
        )
    output_path = Path(output)
    if output_path.exists() and not output_path.is_dir():
        raise ValidationError(
            f"Путь вывода «{output_path}» не является каталогом.",
            hint="Передайте каталог для архивов.",
        )
    output_path.mkdir(parents=True, exist_ok=True)

    stamp = _utc_stamp()
    archive = output_path / f"{_ARCHIVE_PREFIX}{stamp}.tar.gz"
    counter = 1
    while archive.exists():
        archive = output_path / f"{_ARCHIVE_PREFIX}{stamp}-{counter}.tar.gz"
        counter += 1
    manifest_path = archive.with_name(archive.name + _MANIFEST_SUFFIX)

    with tempfile.TemporaryDirectory(prefix="artek-brain-backup-") as temp:
        stage = Path(temp)
        databases = _stage_store(root_path, stage)
        files = _archive_files(stage)
        with tarfile.open(archive, "w:gz") as tar:
            for item in sorted(stage.rglob("*")):
                if item.is_file():
                    tar.add(item, arcname=item.relative_to(stage).as_posix())
    os.chmod(archive, 0o600)
    _write_manifest(
        manifest_path,
        {
            "format": 1,
            "archive": archive.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "databases": databases,
            "files": files,
        },
    )
    removed = _rotate(output_path, keep) if keep is not None else []
    return {
        "archive": str(archive),
        "manifest": str(manifest_path),
        "databases": databases,
        "files": len(files),
        "bytes": archive.stat().st_size,
        "sha256": _sha256_file(archive),
        "removed": removed,
    }


def _safe_extract(archive: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                target = (destination / member.name).resolve()
                if target != destination_root and not target.is_relative_to(
                    destination_root
                ):
                    raise StoredDataError(
                        "Архив содержит небезопасный путь.",
                        hint="Не распаковывайте недоверенные архивы.",
                    )
                if member.issym() or member.islnk():
                    raise StoredDataError(
                        "Архив содержит ссылки вместо файлов.",
                        hint="Пересоздайте резервную копию.",
                    )
            tar.extractall(destination, filter="data")
    except StoredDataError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise StoredDataError(
            f"Архив повреждён: {exc}.",
            hint="Возьмите другую резервную копию или проверьте диск.",
        ) from exc


def _verify_store(root: Path) -> int:
    """Прочитать текущую версию каждого документа и сверить sha256."""
    store = Store(root)
    try:
        with store._lock:
            store._ensure_open()
            rows = store._conn.execute(
                "SELECT d.path AS path, d.current_version AS n, v.sha256 AS sha "
                "FROM documents d "
                "LEFT JOIN versions v ON v.path=d.path AND v.n=d.current_version "
                "ORDER BY d.path"
            ).fetchall()
            verified = 0
            for row in rows:
                path = str(row["path"])
                sha = row["sha"]
                if sha is None:
                    raise StoredDataError(
                        f"Документ «{path}» без текущей версии.",
                        hint="Восстановите хранилище из резервной копии.",
                    )
                try:
                    text = store._read_blob(str(sha))
                except StoredDataError as exc:
                    raise StoredDataError(
                        f"Не читается текущая версия «{path}»: {exc.message}",
                        hint="Архив неполный или блоб повреждён.",
                    ) from exc
                actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if actual != str(sha):
                    raise StoredDataError(
                        f"sha256 не совпал для текущей версии «{path}».",
                        hint="Архив повреждён; возьмите другую копию.",
                    )
                verified += 1
            return verified
    finally:
        store.close()


def verify_backup(archive: str | Path) -> dict[str, Any]:
    """Проверить архив: распаковать, сверить манифест и версии документов."""
    archive_path = Path(archive)
    if not archive_path.is_file():
        raise NotFoundError(
            f"Архив «{archive_path}» не найден.",
            hint="Передайте путь к файлу brain-backup-*.tar.gz.",
        )
    manifest_path = archive_path.with_name(archive_path.name + _MANIFEST_SUFFIX)
    if not manifest_path.is_file():
        raise NotFoundError(
            f"Манифест «{manifest_path}» не найден.",
            hint="Проверка требует манифест, созданный вместе с архивом.",
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StoredDataError(
            f"Манифест повреждён: {exc}.",
            hint="Пересоздайте резервную копию.",
        ) from exc
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise StoredDataError(
            "Манифест не содержит списка файлов.",
            hint="Пересоздайте резервную копию.",
        )
    with tempfile.TemporaryDirectory(prefix="artek-brain-verify-") as temp:
        destination = Path(temp)
        _safe_extract(archive_path, destination)
        actual_files = {
            item.relative_to(destination).as_posix()
            for item in destination.rglob("*")
            if item.is_file()
        }
        expected = set(files)
        missing = expected - actual_files
        extra = actual_files - expected
        if missing:
            raise StoredDataError(
                "В архиве нет файлов из манифеста: "
                + ", ".join(sorted(missing)[:5])
                + ".",
                hint="Архив неполный; возьмите другую копию.",
            )
        if extra:
            raise StoredDataError(
                "В архиве есть файлы вне манифеста: "
                + ", ".join(sorted(extra)[:5])
                + ".",
                hint="Архив изменяли после создания.",
            )
        for relative, meta in sorted(files.items()):
            target = destination / relative
            expected_sha = str((meta or {}).get("sha256") or "")
            actual_sha = _sha256_file(target)
            if not expected_sha or actual_sha != expected_sha:
                raise StoredDataError(
                    f"sha256 не совпал для файла «{relative}».",
                    hint="Архив повреждён; возьмите другую копию.",
                )
        documents = _verify_store(destination)
    return {
        "archive": str(archive_path),
        "manifest": str(manifest_path),
        "files": len(files),
        "documents": documents,
        "ok": True,
    }
