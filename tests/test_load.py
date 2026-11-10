"""Небольшой нагрузочный тест: 2000 версий и размер на диске."""

from __future__ import annotations

import time

from artek_brain import Store


def test_two_thousand_versions(store: Store) -> None:
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
        f"\nload: 2000 versions in {elapsed:.2f}s; "
        f"disk={stats['disk_bytes']} bytes, blobs={stats['blob_bytes']} bytes, "
        f"db={stats['db_bytes']} bytes"
    )
