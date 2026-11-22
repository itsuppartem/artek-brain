"""Дельта-хранение: снимки K, прозрачность API, repack, GC, архив, квоты."""

from __future__ import annotations

import hashlib
import zlib
from datetime import timedelta
from pathlib import Path

import pytest

from artek_brain import Quota, QuotaError, Store, StoredDataError


WORDS = (
    "память версия документ хранилище снимок дельта блоб аудит откат автор "
    "раздел строка правка история доступ поиск задача агент человек правило "
    "данные размер диск квота архив проверка ссылка заголовок отчёт сервер "
    "команда рой контекст слой формат экспорт импорт журнал метрика"
).split()


def _text(n: int, *, sections: int = 4, lines_per_section: int = 15) -> str:
    parts = ["# Документ\n", f"\nВерсия {n}.\n"]
    for index in range(sections):
        parts.append(f"\n## Раздел {index}\n\n")
        for line in range(lines_per_section):
            words = [
                WORDS[(index * 7 + line * 3 + offset) % len(WORDS)]
                for offset in range(16)
            ]
            parts.append(f"- {line}. " + " ".join(words) + "\n")
    parts.append(f"\n## Меняется\n\nзначение {n}\n")
    return "".join(parts)


def _write_versions(store: Store, path: str, count: int, *, author: str = "alice") -> str:
    latest = ""
    for n in range(1, count + 1):
        latest = _text(n)
        store.write(
            path,
            latest,
            author if n % 4 else "bob",
            f"v{n}",
            base_version=None if n == 1 else n - 1,
        )
    return latest


def _rows(store: Store, path: str):
    return store.storage.conn.execute(
        "SELECT * FROM version_blobs WHERE path=? ORDER BY n", (path,)
    ).fetchall()


def test_default_snapshot_every_20(store: Store) -> None:
    """Полный снимок каждые 20 версий, цепочка не длиннее K."""
    _write_versions(store, "docs/big.md", 46)
    rows = _rows(store, "docs/big.md")
    fulls = [int(row["n"]) for row in rows if row["kind"] == "full"]
    assert fulls == [1, 21, 41]
    assert all(int(row["depth"]) <= 20 for row in rows)
    for row in rows:
        if row["kind"] == "delta":
            assert int(row["base_n"]) == int(row["n"]) - 1
    for n in (1, 2, 20, 21, 22, 46):
        assert store.read("docs/big.md", version=n).content == _text(n)


def test_snapshot_every_is_configurable(tmp_path: Path) -> None:
    """Настраиваемый K задаёт границу полных снимков."""
    store = Store(tmp_path / "brain", snapshot_every=5)
    try:
        _write_versions(store, "docs/a.md", 12)
        rows = _rows(store, "docs/a.md")
        fulls = [int(row["n"]) for row in rows if row["kind"] == "full"]
        assert fulls == [1, 6, 11]
        assert all(int(row["depth"]) <= 5 for row in rows)
    finally:
        store.close()


def test_read_diff_blame_rollback_export_are_transparent(store: Store) -> None:
    """Чтение, diff, blame, откат и экспорт не знают о дельтах."""
    _write_versions(store, "docs/a.md", 30)
    assert any(row["kind"] == "delta" for row in _rows(store, "docs/a.md"))

    assert store.read("docs/a.md").content == _text(30)
    assert store.read("docs/a.md", version=7).content == _text(7)

    difference = store.diff("docs/a.md", 7, 30)
    assert "-значение 7" in difference
    assert "+значение 30" in difference

    blame = {item.title: item for item in store.blame("docs/a.md")}
    assert "Меняется" in blame
    assert blame["Меняется"].version == 30
    assert blame["Раздел 0"].version == 1

    rollback = store.rollback_version("docs/a.md", 7, "carol", "вернуть")
    assert rollback.version == 31
    assert store.read("docs/a.md").content == _text(7)
    assert store.diff("docs/a.md", 7, 31) == ""

    assert store.export_markdown("docs/a.md", version=7) == _text(7)


def test_repack_is_idempotent_and_verifies_every_version(tmp_path: Path) -> None:
    """Repack превращает полные копии в дельты и проверяет sha256 каждой версии."""
    store = Store(tmp_path / "brain", snapshot_every=1)
    try:
        _write_versions(store, "docs/a.md", 25)
        before_rows = _rows(store, "docs/a.md")
        assert [row["kind"] for row in before_rows] == ["full"] * 25

        first = store.repack(snapshot_every=10)
        assert first.versions == 25
        assert first.rewritten == 22
        assert first.deltas > 0 and first.fulls > 0
        rows = _rows(store, "docs/a.md")
        fulls = [int(row["n"]) for row in rows if row["kind"] == "full"]
        assert fulls == [1, 11, 21]
        assert all(int(row["depth"]) <= 10 for row in rows)

        history = {item.n: item for item in store.history("docs/a.md", limit=100)}
        for n, info in history.items():
            content = store.read("docs/a.md", version=n).content
            assert hashlib.sha256(content.encode("utf-8")).hexdigest() == info.sha256

        again = store.repack(snapshot_every=10)
        assert again.rewritten == 0
        assert again.deltas == first.deltas
        assert again.fulls == first.fulls
        assert again.blob_bytes_after == first.blob_bytes_after
        for n in range(1, 26):
            assert store.read("docs/a.md", version=n).content == _text(n)
    finally:
        store.close()


def test_repack_reads_legacy_full_blobs(tmp_path: Path) -> None:
    """Старые блобы без контейнера читаются и переупаковываются."""
    store = Store(tmp_path / "brain")
    try:
        store.write("docs/a.md", _text(1), "alice", "v1")
        store.write("docs/a.md", _text(2), "alice", "v2", base_version=1)
        first = next(item for item in store.history("docs/a.md", limit=10) if item.n == 1)
        legacy = store.blobs_dir / first.sha256[:2] / first.sha256[2:]
        legacy.write_bytes(zlib.compress(_text(1).encode("utf-8"), 6))
        with store.storage.conn:
            store.storage.conn.execute(
                "DELETE FROM version_blobs WHERE path=? AND n=1", ("docs/a.md",)
            )

        assert store.read("docs/a.md", version=1).content == _text(1)
        result = store.repack()
        assert result.versions == 2
        assert store.read("docs/a.md", version=1).content == _text(1)
        assert store.read("docs/a.md", version=2).content == _text(2)
    finally:
        store.close()


def test_gc_keeps_delta_bases_and_removes_orphans(tmp_path: Path) -> None:
    """GC не трогает базовые блобы цепочек и удаляет мусор."""
    store = Store(tmp_path / "brain", snapshot_every=10)
    try:
        _write_versions(store, "docs/a.md", 30)
        orphan = store.blobs_dir / ("f" * 2) / ("f" * 62)
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan")

        result = store.gc(min_age_seconds=0)
        assert result.removed >= 1
        assert not orphan.exists()

        for n in (1, 11, 21):
            sha = next(
                item.sha256
                for item in store.history("docs/a.md", limit=100)
                if item.n == n
            )
            assert (store.blobs_dir / sha[:2] / sha[2:]).exists()
        for n in range(1, 31):
            assert store.read("docs/a.md", version=n).content == _text(n)

        store.repack(snapshot_every=10)
        store.gc(min_age_seconds=0)
        for n in range(1, 31):
            assert store.read("docs/a.md", version=n).content == _text(n)
    finally:
        store.close()


def test_archive_old_versions_compresses_cold_versions(tmp_path: Path) -> None:
    """Архив сжимает lzma, хранит последние N и не теряет данные."""
    store = Store(tmp_path / "brain", snapshot_every=5)
    try:
        _write_versions(store, "docs/a.md", 8)
        result = store.archive_old_versions(timedelta(0), keep_last=2)
        assert result.archived == 6
        assert result.skipped == 2
        assert result.bytes_after > 0

        rows = {int(row["n"]): row for row in _rows(store, "docs/a.md")}
        for n in range(1, 7):
            assert int(rows[n]["archived"]) == 1
            assert rows[n]["codec"] == "lzma"
        for n in (7, 8):
            assert int(rows[n]["archived"]) == 0

        for n in range(1, 9):
            assert store.read("docs/a.md", version=n).content == _text(n)

        again = store.archive_old_versions(timedelta(0), keep_last=2)
        assert again.archived == 0
        assert again.skipped == 8
        assert any(item.action == "archive" for item in store.audit(limit=20))

        store.repack(snapshot_every=5)
        for n in range(1, 9):
            assert store.read("docs/a.md", version=n).content == _text(n)
    finally:
        store.close()


def test_disk_report_and_hard_limit(tmp_path: Path) -> None:
    """Отчёт о диске считает метрики, жёсткий лимит отклоняет запись."""
    store = Store(tmp_path / "brain")
    try:
        _write_versions(store, "docs/a.md", 4)
        report = store.disk_report(days=7, forecast_days=14)
        assert report["versions"] == 4
        assert report["blob_bytes"] > 0
        assert report["disk_bytes"] >= report["blob_bytes"]
        assert report["bytes_per_version"] > 0
        assert report["growth"]["window_days"] == 7
        assert report["growth"]["forecast_days"] == 14
        assert report["quota_bytes"] is None

        store.quota = Quota(max_disk_bytes=1)
        with pytest.raises(QuotaError) as error:
            store.write(
                "docs/a.md", _text(5), "alice", "не влезет", base_version=4
            )
        assert error.value.kind == "диск"
        assert store.read("docs/a.md").content == _text(4)
        assert len(store.history("docs/a.md", limit=100)) == 4

        limit = store.disk_report()["disk_bytes"] * 10
        store.quota = Quota(max_disk_bytes=limit)
        report = store.disk_report()
        assert report["quota_bytes"] == limit
        assert 0 < report["quota_percent"] < 100
    finally:
        store.close()


def test_corrupted_delta_is_detected(store: Store) -> None:
    """Порча дельта-блоба не отдаёт неверные данные."""
    _write_versions(store, "docs/a.md", 10)
    row = next(
        item for item in _rows(store, "docs/a.md") if item["kind"] == "delta"
    )
    target = (
        store.blobs_dir / row["blob_sha256"][:2] / row["blob_sha256"][2:]
    )
    target.write_bytes(b"broken")

    with pytest.raises(StoredDataError):
        store.read("docs/a.md", version=int(row["n"]))


def test_identical_content_shares_blob(store: Store) -> None:
    """Дедупликация по sha256 содержимого сохраняется между документами."""
    _write_versions(store, "docs/a.md", 2)
    before = store.disk_report()["blob_bytes"]
    store.write("docs/b.md", _text(1), "alice", "копия")

    after = store.disk_report()["blob_bytes"]
    assert after == before
    left = next(item for item in store.history("docs/a.md", limit=10) if item.n == 1)
    right = next(item for item in store.history("docs/b.md", limit=10) if item.n == 1)
    assert left.sha256 == right.sha256
    rows = _rows(store, "docs/b.md")
    assert rows[0]["kind"] == "full"
    assert rows[0]["blob_sha256"] == right.sha256
