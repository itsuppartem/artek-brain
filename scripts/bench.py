"""Простой замер: запись, чтение и поиск на N документах (локально, SQLite).

Запуск: uv run python scripts/bench.py [N]
"""

from __future__ import annotations

import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

from artek_brain.service import Service

WORDS = (
    "касса смена выкладка сервер образ миграция бэкап расширение заказ платёж "
    "депозит штраф график задача бот админка поиск память версия решение"
).split()


def _text(rng: random.Random, index: int) -> str:
    body = "\n".join(
        " ".join(rng.choice(WORDS) for _ in range(12)) for _ in range(20)
    )
    return f"# Документ {index}\n\n## Раздел\n{body}\n"


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    rng = random.Random(42)
    with tempfile.TemporaryDirectory() as folder:
        service = Service(Path(folder) / ".brain")
        token = str(service.access.create_token("bench", "agent", [("", "write")]))
        writes, reads, searches = [], [], []
        for index in range(count):
            started = time.perf_counter()
            service.write(token, f"notes/n{index}.md", _text(rng, index), "замер")
            writes.append(time.perf_counter() - started)
        for index in range(200):
            path = f"notes/n{rng.randrange(count)}.md"
            started = time.perf_counter()
            service.read(token, path)
            reads.append(time.perf_counter() - started)
        for _ in range(200):
            query = " ".join(rng.sample(WORDS, 2))
            started = time.perf_counter()
            service.search(token, query, limit=10)
            searches.append(time.perf_counter() - started)
        disk = sum(item.stat().st_size for item in Path(folder).rglob("*") if item.is_file())
        service.close()

    def ms(values: list[float]) -> str:
        ordered = sorted(values)
        p95 = ordered[int(len(ordered) * 0.95) - 1]
        return f"median {statistics.median(values) * 1000:.1f} ms, p95 {p95 * 1000:.1f} ms"

    print(f"documents: {count}")
    print(f"write:  {ms(writes)}")
    print(f"read:   {ms(reads)}")
    print(f"search: {ms(searches)}")
    print(f"disk:   {disk / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
