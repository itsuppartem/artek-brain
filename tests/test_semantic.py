"""Семантический слой: HashEmbedder, индекс, гибрид, права и Ollama."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from artek_brain import (
    Access,
    Brain,
    HashEmbedder,
    OllamaEmbedder,
    PermissionDenied,
    SemanticDisabledError,
    SemanticIndex,
    SemanticIndexStaleError,
    SemanticNotConfiguredError,
    Store,
    configure_semantic,
)
from artek_brain.embeddings import EmbeddingError
from artek_brain.mcp_server import BrainMCP
from artek_brain.service import Service


def _token(brain: Brain, name: str, scopes: Any = (("", "write"),)) -> str:
    return str(brain.access.create_token(name, "agent", scopes))


def _semantic_brain(tmp_path: Path, *, dim: int = 128) -> tuple[Store, Brain]:
    store = Store(tmp_path / "brain")
    configure_semantic(store, embedder="hash", dim=dim)
    brain = Brain(store, Access.for_store(store))
    return store, brain


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def test_hash_embedder_is_deterministic_and_normalized() -> None:
    embedder = HashEmbedder(64)
    first = embedder.embed(["деплой сервера"])[0]
    second = embedder.embed(["деплой сервера"])[0]
    other = embedder.embed(["рецепт пирога"])[0]
    assert embedder.name == "hash:64" and embedder.dim == 64
    assert first == second
    assert first != other
    assert math.isclose(sum(value * value for value in first), 1.0, rel_tol=1e-9)


def test_hash_embedder_ranks_close_words_and_misses_synonyms() -> None:
    embedder = HashEmbedder(256)
    query, close, far, synonym = embedder.embed(
        [
            "деплой сервер релиз",
            "деплой сервер инструкция",
            "кулинария рецепт пирог",
            "машина",
        ]
    )
    car = embedder.embed(["автомобиль"])[0]
    assert _cosine(query, close) > _cosine(query, far)
    assert _cosine(car, synonym) == pytest.approx(0.0)


def test_semantic_search_ranks_by_word_overlap(tmp_path: Path) -> None:
    store, brain = _semantic_brain(tmp_path)
    token = _token(brain, "writer")
    try:
        brain.write(token, "docs/close.md", "# Close\n\nдеплой сервер инструкция\n", "c")
        brain.write(token, "docs/other.md", "# Other\n\nкулинария рецепт пирог\n", "c")
        brain.write(token, "docs/mixed.md", "# Mixed\n\nсервер деплой\n", "c")
        page = brain.search(token, "деплой сервер", mode="semantic")
        assert page.total == 3
        assert {item.path for item in page.items[:2]} == {
            "docs/close.md",
            "docs/mixed.md",
        }
        assert page.items[-1].path == "docs/other.md"
        assert page.items[-1].score < page.items[0].score
    finally:
        brain.close()
        store.close()


def test_semantic_and_hybrid_respect_read_scope(tmp_path: Path) -> None:
    store, brain = _semantic_brain(tmp_path)
    writer = _token(brain, "writer")
    reader = _token(brain, "reader", scopes=[("docs/public", "read")])
    try:
        content = "# Общее\n\nдеплой сервер релиз\n"
        brain.write(writer, "docs/public/a.md", content, "c")
        brain.write(writer, "docs/secret/b.md", content, "c")

        semantic = brain.search(reader, "деплой сервер", mode="semantic")
        assert [item.path for item in semantic.items] == ["docs/public/a.md"]
        assert semantic.total == 1

        hybrid = brain.search(reader, "деплой сервер", mode="hybrid")
        assert [item.path for item in hybrid.items] == ["docs/public/a.md"]
        assert hybrid.total == 1
        assert hybrid.items[0].score == pytest.approx(2 / 61)

        with pytest.raises(PermissionDenied):
            brain.search(reader, "деплой", mode="semantic", path_prefix="docs/secret")
    finally:
        brain.close()
        store.close()


def test_semantic_index_follows_write_rollback_delete_restore(tmp_path: Path) -> None:
    store, brain = _semantic_brain(tmp_path)
    token = _token(brain, "writer")
    try:
        brain.write(token, "docs/a.md", "# A\n\nalpha deploy\n", "v1")
        assert brain.search(token, "alpha", mode="semantic").total == 1

        brain.write(token, "docs/a.md", "# A\n\nbeta kitchen\n", "v2", base_version=1)
        assert brain.search(token, "alpha", mode="semantic").total == 0
        assert brain.search(token, "beta", mode="semantic").total == 1

        brain.rollback_version(token, "docs/a.md", 1, "вернул")
        assert brain.search(token, "alpha", mode="semantic").total == 1
        assert brain.search(token, "beta", mode="semantic").total == 0

        brain.delete(token, "docs/a.md", "удалил")
        assert brain.search(token, "alpha", mode="semantic").total == 0
        brain.restore(token, "docs/a.md", "вернул")
        assert brain.search(token, "alpha", mode="semantic").total == 1
    finally:
        brain.close()
        store.close()


def test_model_change_marks_index_stale_and_requires_reindex(tmp_path: Path) -> None:
    store = Store(tmp_path / "brain")
    first = SemanticIndex(store, HashEmbedder(64))
    try:
        store.write("docs/a.md", "# A\n\nalpha deploy\n", "alice", "c")
        first.reindex()
        assert first.chunk_count() >= 1 and first.stale is False
        assert first.search("alpha").total == 1
        db_path = first.db_path
    finally:
        first.close()
    changed = SemanticIndex(store, HashEmbedder(128), path=db_path)
    try:
        assert changed.stale is True
        with pytest.raises(SemanticIndexStaleError):
            changed.search("alpha")
        assert changed.reindex() >= 1
        assert changed.stale is False
        assert changed.search("alpha").total == 1
    finally:
        changed.close()
        store.close()


def test_semantic_is_off_by_default_and_needs_embedder(tmp_path: Path) -> None:
    store = Store(tmp_path / "brain")
    brain = Brain(store, Access.for_store(store))
    try:
        token = _token(brain, "writer")
        brain.write(token, "docs/a.md", "# A\n\nтекст\n", "c")
        with pytest.raises(SemanticDisabledError):
            brain.search(token, "текст", mode="semantic")
        with pytest.raises(SemanticDisabledError):
            brain.search(token, "текст", mode="hybrid")
    finally:
        brain.close()
        store.close()

    store = Store(tmp_path / "brain-no-embedder")
    configure_semantic(store, embedder=None)
    brain = Brain(store, Access.for_store(store))
    try:
        token = _token(brain, "writer")
        with pytest.raises(SemanticNotConfiguredError) as caught:
            brain.search(token, "текст", mode="semantic")
        assert "settings.json" in str(caught.value)
    finally:
        brain.close()
        store.close()


class _OllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/embed":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400)
            return
        if payload.get("model") != "test-model":
            self.send_error(400)
            return
        texts = payload.get("input")
        if not isinstance(texts, list):
            self.send_error(400)
            return
        vectors = [[float(len(text)), float(index + 1)] for index, text in enumerate(texts)]
        body = json.dumps({"embeddings": vectors}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_ollama_embedder_protocol_and_errors() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        embedder = OllamaEmbedder(
            "test-model", url=f"http://127.0.0.1:{port}", timeout=2
        )
        vectors = embedder.embed(["a", "bb"])
        assert embedder.dim == 2
        assert embedder.name == "ollama:test-model"
        assert vectors == [[1.0, 1.0], [2.0, 2.0]]

        wrong = OllamaEmbedder("other", url=f"http://127.0.0.1:{port}", timeout=2)
        with pytest.raises(EmbeddingError) as caught:
            wrong.embed(["a"])
        assert "ollama" in str(caught.value).lower()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    unreachable = OllamaEmbedder(
        "test-model", url="http://127.0.0.1:1", timeout=0.2
    )
    with pytest.raises(EmbeddingError) as caught:
        unreachable.embed(["a"])
    assert "Ollama" in str(caught.value)


def test_hybrid_rrf_puts_document_found_by_both_first(tmp_path: Path) -> None:
    store, brain = _semantic_brain(tmp_path)
    token = _token(brain, "writer")
    try:
        brain.write(token, "docs/fts.md", "# FTS\n\ndeploy\n", "c")
        brain.write(token, "docs/both.md", "# Both\n\ndeploy server release\n", "c")
        brain.write(token, "docs/semantic.md", "# Semantic\n\nserver release deployment\n", "c")
        hybrid = brain.search(token, "deploy server", mode="hybrid")
        assert hybrid.items[0].path == "docs/both.md"
        assert hybrid.total == 3
    finally:
        brain.close()
        store.close()


def _cli(
    root: Path,
    *args: str,
    token: str | None = None,
    stdin: str = "",
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "artek_brain.cli", "--root", str(root)]
    if token is not None:
        command += ["--token", token]
    command += list(args)
    env = os.environ.copy()
    env.pop("BRAIN_TOKEN", None)
    env.pop("BRAIN_HOME", None)
    return subprocess.run(
        command, input=stdin, text=True, capture_output=True, env=env, timeout=60
    )


def test_cli_semantic_flag_and_mcp_mode(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    root.mkdir()
    (root / "settings.json").write_text(
        json.dumps({"semantic": {"enabled": True, "embedder": "hash", "dim": 128}}),
        encoding="utf-8",
    )
    owner = json.loads(_cli(root, "--json", "init").stdout)["token"]
    agent = json.loads(
        _cli(
            root,
            "--json",
            "token",
            "create",
            "--name",
            "agent",
            "--role",
            "agent",
            "--scope",
            "docs:write",
            token=owner,
        ).stdout
    )["token"]
    written = _cli(
        root,
        "--json",
        "write",
        "docs/a.md",
        "--reason",
        "c",
        token=agent,
        stdin="# A\n\ndeploy server\n",
    )
    assert json.loads(written.stdout)["version"] == 1

    found = _cli(root, "--json", "search", "deploy server", "--semantic", token=agent)
    assert found.returncode == 0, found.stderr
    assert json.loads(found.stdout)["items"][0]["path"] == "docs/a.md"

    service = Service(root)
    try:
        app = BrainMCP(service, agent)
        text, error = app.call("brain_search", {"query": "deploy server"})
        assert error is False and json.loads(text)["total"] == 1
        text, error = app.call(
            "brain_search", {"query": "deploy server", "mode": "semantic"}
        )
        assert error is False and json.loads(text)["items"][0]["path"] == "docs/a.md"
        text, error = app.call(
            "brain_search", {"query": "deploy server", "mode": "hybrid"}
        )
        assert error is False and json.loads(text)["total"] == 1
        text, error = app.call(
            "brain_search", {"query": "deploy", "mode": "нет-такого"}
        )
        assert error is True and "режим" in text.lower()
    finally:
        service.close()


def test_cli_semantic_status_enable_reindex_disable(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    root.mkdir()
    owner = json.loads(_cli(root, "--json", "init").stdout)["token"]
    agent = json.loads(
        _cli(
            root, "--json", "token", "create", "--name", "agent", "--role", "agent",
            "--scope", "docs:write", token=owner,
        ).stdout
    )["token"]

    status = json.loads(_cli(root, "--json", "semantic", "status", token=owner).stdout)
    assert status["enabled"] is False
    assert status["chunks"] == 0

    denied = _cli(root, "--json", "semantic", "enable", token=agent)
    assert denied.returncode != 0

    enabled = _cli(
        root, "--json", "semantic", "enable", "--embedder", "hash", "--dim", "64",
        token=owner,
    )
    assert enabled.returncode == 0, enabled.stderr
    data = json.loads(enabled.stdout)
    assert data["enabled"] is True
    assert data["embedder"] == "hash"
    assert data["dim"] == 64
    assert data["chunks"] == 0
    assert data["stale"] is False

    written = _cli(
        root, "--json", "write", "docs/a.md", "--reason", "c", token=agent,
        stdin="# A\n\ndeploy server release\n",
    )
    assert written.returncode == 0, written.stderr

    denied_reindex = _cli(root, "--json", "semantic", "reindex", token=agent)
    assert denied_reindex.returncode != 0

    reindex = _cli(root, "--json", "semantic", "reindex", token=owner)
    assert reindex.returncode == 0, reindex.stderr
    assert json.loads(reindex.stdout)["chunks"] >= 1

    status = json.loads(_cli(root, "--json", "semantic", "status", token=owner).stdout)
    assert status["chunks"] >= 1
    assert status["stale"] is False

    hybrid = _cli(
        root, "--json", "search", "deploy server", "--mode", "hybrid", token=agent
    )
    assert hybrid.returncode == 0, hybrid.stderr
    assert json.loads(hybrid.stdout)["items"][0]["path"] == "docs/a.md"

    disabled = _cli(root, "--json", "semantic", "disable", token=owner)
    assert disabled.returncode == 0, disabled.stderr
    assert json.loads(disabled.stdout)["enabled"] is False


def test_mcp_hybrid_without_semantics_returns_fts_with_warning(tmp_path: Path) -> None:
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    agent = str(service.access.create_token("agent", "agent", [("docs", "write")]))
    service.write(agent, "docs/a.md", "# A\n\ndeploy server\n", "c")
    try:
        app = BrainMCP(service, agent)
        text, error = app.call("brain_search", {"query": "deploy", "mode": "hybrid"})
        assert error is False
        data = json.loads(text)
        assert data["total"] == 1
        assert "warning" in data and "semantic" in data["warning"]

        text, error = app.call(
            "brain_search", {"query": "deploy", "mode": "semantic"}
        )
        assert error is True
        assert "semantic" in text.lower() or "семант" in text.lower()
    finally:
        service.close()
