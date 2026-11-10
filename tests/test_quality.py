"""Качество знаний: дубли, противоречия, схемы, ссылки и метрики."""

from __future__ import annotations

from pathlib import Path

import pytest

from artek_brain import Brain, Quality, ReviewQueue, Store
from artek_brain.access import Access
from artek_brain.errors import ValidationError
from artek_brain.service import Service


@pytest.fixture()
def owner(access: Access) -> str:
    return str(access.create_token("owner", "owner"))


def _write(
    brain: Brain,
    token: str,
    path: str,
    body: str,
    *,
    status: str = "approved",
    doc_type: str = "note",
    extra: str = "",
    review_by: str = "2030-01-01",
) -> None:
    content = (
        "---\n"
        f"type: {doc_type}\n"
        f"status: {status}\n"
        "owner: owner\n"
        f"review_by: {review_by}\n"
        f"{extra}"
        "---\n"
        f"{body}"
    )
    brain.write(token, path, content, "create")


def test_duplicates_by_jaccard(brain: Brain, owner: str) -> None:
    text = "# API\n\nсервер слушает порт восемь тысяч один и пишет лог в файл\n"
    _write(brain, owner, "docs/a.md", text)
    _write(brain, owner, "docs/b.md", text)
    _write(brain, owner, "docs/c.md", "# Другое\n\nсовсем другой текст про квоты и лимиты памяти\n")

    quality = Quality(brain, threshold=0.8)
    pairs = quality.duplicates(owner)
    assert len(pairs) == 1
    pair = pairs[0]
    assert {pair.path_a, pair.path_b} == {"docs/a.md", "docs/b.md"}
    assert pair.score == pytest.approx(1.0)

    assert Quality(brain, threshold=0.99).duplicates(owner)[0].score == pytest.approx(1.0)


def test_contradictions_by_facts(brain: Brain, owner: str) -> None:
    _write(brain, owner, "docs/a.md", "# A\n", extra="facts: [port = 8001]\n")
    _write(brain, owner, "docs/b.md", "# B\n", extra="facts: [port = 8002]\n")
    _write(brain, owner, "docs/c.md", "# C\n", extra="facts: [port = 8003]\n", status="draft")

    items = Quality(brain).contradictions(owner)
    assert len(items) == 1
    item = items[0]
    assert item.kind == "facts"
    assert item.key == "port"
    assert {item.path_a, item.path_b} == {"docs/a.md", "docs/b.md"}
    assert {item.value_a, item.value_b} == {"8001", "8002"}


def test_same_fact_value_is_not_contradiction(brain: Brain, owner: str) -> None:
    _write(brain, owner, "docs/a.md", "# A\n", extra="facts: [port = 8001]\n")
    _write(brain, owner, "docs/b.md", "# B\n", extra="facts: [port = 8001]\n")
    assert Quality(brain).contradictions(owner) == []


def test_table_number_heuristic(brain: Brain, owner: str) -> None:
    _write(brain, owner, "docs/a.md", "# A\n\n| порт | 8001 |\n")
    _write(brain, owner, "docs/b.md", "# B\n\n| порт | 8002 |\n")
    items = Quality(brain).contradictions(owner)
    assert len(items) == 1
    item = items[0]
    assert item.kind == "table"
    assert item.key == "порт"
    assert item.line_a == 9
    assert item.line_b == 9


def test_broken_links(brain: Brain, owner: str) -> None:
    _write(brain, owner, "docs/a.md", "# A\n\n[нет](missing.md)\n")
    links = Quality(brain).broken_links(owner)
    assert len(links) == 1
    assert links[0].source == "docs/a.md"
    assert links[0].target == "docs/missing.md"


def test_report_metrics(brain: Brain, owner: str) -> None:
    _write(
        brain,
        owner,
        "docs/a.md",
        "# A\n",
        extra="refs: [a" + "b" * 63 + "]\n",
        review_by="2000-01-01",
    )
    _write(brain, owner, "docs/b.md", "# B\n", review_by="2030-01-01", status="stale")
    _write(brain, owner, "docs/c.md", "# C\n", review_by="2030-01-01")

    report = Quality(brain).report(owner)
    assert report["documents"] == 3
    assert report["with_refs"] == 1
    assert report["coverage"] == pytest.approx(1 / 3)
    assert report["overdue"] == 1
    assert report["freshness_overdue"] == pytest.approx(1 / 3)
    assert report["stale"] == 1
    assert report["stale_paths"] == ["docs/b.md"]
    assert report["review_queue_open"] == 0


def test_schemas_validate_and_strict_check(brain: Brain, owner: str) -> None:
    brain.write(
        owner,
        "rules/schemas.md",
        "---\ntype: rules\n---\n# Схемы\ndecision: owner, status, review_by\n",
        "create",
    )
    _write(
        brain,
        owner,
        "docs/good.md",
        "# Good\n",
        doc_type="decision",
        extra="",
    )
    _write(
        brain,
        owner,
        "approved/bad.md",
        "# Bad\n",
        doc_type="decision",
        review_by="",
    )

    quality = Quality(brain)
    schemas = quality.load_schemas(owner)
    assert schemas["decision"] == ("owner", "status", "review_by")

    good = quality.validate(owner, "docs/good.md", schemas=schemas)
    assert good.valid is True
    assert good.missing == ()

    bad = quality.validate(owner, "approved/bad.md", schemas=schemas)
    assert bad.valid is False
    assert bad.missing == ("review_by",)

    with pytest.raises(ValidationError):
        quality.strict_check(
            owner,
            "approved/bad.md",
            "---\ntype: decision\nstatus: approved\nowner: owner\n---\n# Bad\n",
        )


def test_service_write_enforces_strict_schema(tmp_path: Path) -> None:
    with Service(tmp_path / "brain") as service:
        owner = str(service.access.create_token("owner", "owner"))
        service.write(
            owner,
            "rules/schemas.md",
            "---\ntype: rules\n---\n# Схемы\ndecision: owner, status, review_by\n",
            "create",
        )
        with pytest.raises(ValidationError):
            service.write(
                owner,
                "approved/bad.md",
                "---\ntype: decision\nstatus: approved\nowner: owner\n---\n# Bad\n",
                "create",
            )
        written = service.write(
            owner,
            "approved/good.md",
            "---\ntype: decision\nstatus: approved\nowner: owner\n"
            "review_by: 2030-01-01\n---\n# Good\n",
            "create",
        )
        assert written["version"] == 1


def test_quality_requires_token(brain: Brain, owner: str) -> None:
    _write(brain, owner, "docs/a.md", "# A\n")
    with pytest.raises(Exception):
        Quality(brain).duplicates("")


def test_quality_can_store_token(brain: Brain, owner: str) -> None:
    _write(brain, owner, "docs/a.md", "# A\n")
    quality = Quality(brain, token=owner)
    assert quality.report()["documents"] == 1
    assert quality.duplicates() == []
    assert quality.validate("docs/a.md").valid is True
