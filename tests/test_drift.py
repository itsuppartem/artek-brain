"""Drift: привязка документов к файлам git-репозитория."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from artek_brain import Brain, Drift, ReviewQueue, check_drift, mark_stale, pin
from artek_brain.access import Access


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _doc(digest: str, *, status: str = "approved") -> str:
    return (
        "---\n"
        "type: decision\n"
        f"status: {status}\n"
        "owner: owner\n"
        "review_by: 2030-01-01\n"
        f"refs: [app.py@{digest}]\n"
        "---\n"
        "# API\n\nОписание.\n"
    )


@pytest.fixture()
def owner(access: Access) -> str:
    return str(access.create_token("owner", "owner"))


def test_drift_sha256_change_marks_stale_and_queues(
    tmp_path: Path, brain: Brain, access: Access, owner: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "app.py"
    target.write_text("print(1)\n", encoding="utf-8")
    old = _sha(target)
    brain.write(owner, "docs/api.md", _doc(old), "create")

    assert check_drift(brain, owner, repo) == []

    target.write_text("print(2)\n", encoding="utf-8")
    items = check_drift(brain, owner, repo)
    assert len(items) == 1
    item = items[0]
    assert item.doc == "docs/api.md"
    assert item.ref.path == "app.py"
    assert item.expected == old
    assert item.actual == _sha(target)
    assert item.expected != item.actual
    assert item.reason == "changed"

    written = mark_stale(brain, owner, repo, items=items)
    assert len(written) == 1
    assert written[0].version == 2

    read = brain.read(owner, "docs/api.md")
    assert "status: stale" in read.content
    history = brain.history(owner, "docs/api.md")
    assert history[0].author == "system"

    queue = ReviewQueue(brain.store, access)
    queued = queue.list(owner)
    assert any(
        entry.path == "docs/api.md" and entry.reason == "drift" and entry.version == 2
        for entry in queued
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git не установлен")
def test_drift_git_hash_and_pin(
    tmp_path: Path, brain: Brain, access: Access, owner: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    target = repo / "app.py"
    target.write_text("print(1)\n", encoding="utf-8")
    digest = subprocess.check_output(
        ["git", "-C", str(repo), "hash-object", "--", "app.py"], text=True
    ).strip()
    brain.write(owner, "docs/api.md", _doc(digest), "create")
    assert Drift(brain, repo).check(owner) == []

    target.write_text("print(2)\n", encoding="utf-8")
    items = Drift(brain, repo).check(owner)
    assert len(items) == 1
    assert items[0].reason == "changed"

    pin(brain, owner, repo, "docs/api.md", f"app.py@{digest}")
    assert Drift(brain, repo).check(owner) == []


def test_drift_missing_file(tmp_path: Path, brain: Brain, owner: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    missing = "a" * 64
    brain.write(owner, "docs/api.md", _doc(missing), "create")
    items = check_drift(brain, owner, repo)
    assert len(items) == 1
    assert items[0].reason == "missing"
    assert items[0].actual is None


def test_drift_symbol_ref(tmp_path: Path, brain: Brain, owner: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "app.py"
    target.write_text("def serve():\n    pass\n", encoding="utf-8")
    digest = _sha(target)
    content = _doc(digest).replace("refs: [app.py@", "refs: [app.py#serve@")
    brain.write(owner, "docs/api.md", content, "create")
    assert check_drift(brain, owner, repo) == []
    target.write_text("def serve():\n    return 1\n", encoding="utf-8")
    items = check_drift(brain, owner, repo)
    assert len(items) == 1
    assert items[0].ref.symbol == "serve"


def test_drift_ignores_doc_without_refs(
    tmp_path: Path, brain: Brain, owner: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    brain.write(owner, "docs/a.md", "---\ntype: note\n---\n# A\n", "create")
    assert check_drift(brain, owner, repo) == []
