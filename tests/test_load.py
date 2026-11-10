"""Нагрузочные тесты дельта-хранилища (маркер load, не в быстром прогоне)."""

from __future__ import annotations

import os
import random
import time
from pathlib import Path

import pytest

from artek_brain import Store

WORDS = (
    "память версия документ хранилище снимок дельта блоб аудит откат автор "
    "раздел строка правка история доступ поиск задача агент человек правило "
    "данные размер диск квота архив проверка ссылка ссылки заголовок отчёт "
    "сервер команда рой контекст слой формат экспорт импорт журнал метрика"
).split()


@pytest.mark.load
def test_two_thousand_versions(store: Store) -> None:
    """Быстрый нагрузочный сценарий: 2000 версий одного документа."""
    started = time.perf_counter()
    store.write("load.md", "# Doc\n\nversion 0\n", "load-agent", "v0")
    for index in range(1, 2000):
        store.write(
            "load.md",
            f"# Doc\n\nversion {index}\n",
            "load-agent",
            f"v{index}",
            base_version=index,
        )
    elapsed = time.perf_counter() - started

    stats = store.stats()
    assert stats["versions"] == 2000
    assert stats["documents"] == 1
    assert store.read("load.md").content == "# Doc\n\nversion 1999\n"
    assert elapsed < 60, f"2000 версий заняли {elapsed:.1f} c"
    assert stats["disk_bytes"] < 10 * 1024 * 1024, stats
    print(
        f"\nload-small: 2000 versions in {elapsed:.2f}s; "
        f"disk={stats['disk_bytes']} bytes, blobs={stats['blob_bytes']} bytes, "
        f"db={stats['db_bytes']} bytes"
    )


def _base_lines(doc: int, rng: random.Random) -> list[str]:
    lines = [f"# Документ {doc}", "", "Вводная часть.", ""]
    for section in range(7):
        lines.append(f"## Раздел {section}")
        lines.append("")
        for paragraph in range(7):
            sentence = " ".join(rng.choice(WORDS) for _ in range(30))
            lines.append(sentence.capitalize() + ".")
        lines.append("")
    return lines


def _edit(lines: list[str], doc: int, version: int) -> None:
    rng = random.Random(doc * 1_000_003 + version)
    for _ in range(3):
        index = rng.randrange(len(lines))
        sentence = " ".join(rng.choice(WORDS) for _ in range(30))
        lines[index] = f"{sentence.capitalize()}. v{doc}-{version}."
    if version % 10 == 0:
        lines.append(f"Дополнение версии {version}.")


@pytest.mark.load
def test_load_many_versions(tmp_path: Path) -> None:
    """20 000 версий по умолчанию (100 000 через BRAIN_LOAD_VERSIONS)."""
    versions = int(os.environ.get("BRAIN_LOAD_VERSIONS", "20000"))
    documents = int(os.environ.get("BRAIN_LOAD_DOCS", "500"))
    per_document = max(2, versions // documents)
    total = documents * per_document

    store = Store(tmp_path / "brain")
    try:
        write_started = time.perf_counter()
        for doc in range(documents):
            rng = random.Random(doc)
            lines = _base_lines(doc, rng)
            path = f"docs/doc-{doc:04d}.md"
            for version in range(1, per_document + 1):
                _edit(lines, doc, version)
                store.write(
                    path,
                    "\n".join(lines) + "\n",
                    "load-agent",
                    f"v{version}",
                    base_version=None if version == 1 else version - 1,
                )
        write_elapsed = time.perf_counter() - write_started

        last_started = time.perf_counter()
        for doc in range(documents):
            store.read(f"docs/doc-{doc:04d}.md")
        last_elapsed = time.perf_counter() - last_started

        oldest_started = time.perf_counter()
        for doc in range(documents):
            store.read(f"docs/doc-{doc:04d}.md", version=1)
        oldest_elapsed = time.perf_counter() - oldest_started

        store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        report = store.disk_report(days=30, forecast_days=30)
        stats = store.stats()
        assert stats["versions"] == total
        assert stats["documents"] == documents

        content_bytes = int(report["content_bytes"])
        blob_ratio = report["blob_bytes"] / content_bytes
        disk_ratio = report["disk_bytes"] / content_bytes
        stored_per_version = report["blob_bytes"] / total
        db_per_version = report["db_bytes"] / total

        print(
            f"\nload: documents={documents} versions={total} "
            f"avg_text={content_bytes / total:.0f}B"
        )
        print(f"write_total={write_elapsed:.2f}s write_avg={write_elapsed / total * 1000:.3f}ms")
        print(
            f"read_last_avg={last_elapsed / documents * 1000:.3f}ms "
            f"read_oldest_avg={oldest_elapsed / documents * 1000:.3f}ms"
        )
        print(
            f"disk={report['disk_bytes']} blob={report['blob_bytes']} "
            f"db={report['db_bytes']} full_copies={content_bytes}"
        )
        print(
            f"ratio_blob/full={blob_ratio:.4f} ratio_disk/full={disk_ratio:.4f} "
            f"bytes_per_version={stored_per_version:.1f} "
            f"db_per_version={db_per_version:.1f}"
        )
        print(
            "extrapolate_1M: "
            f"blob={stored_per_version * 1_000_000 / 2**30:.2f}GiB "
            f"db={db_per_version * 1_000_000 / 2**30:.2f}GiB "
            f"write={write_elapsed / total * 1_000_000 / 3600:.2f}h"
        )

        assert blob_ratio < 0.5, f"дельты не экономят место: {blob_ratio:.3f}"
        assert report["blob_bytes"] < content_bytes
    finally:
        store.close()
