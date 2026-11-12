"""MCP-обработчики Artek Brain: каталог, help, лимит ответа и отказы."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from artek_brain.mcp_server import BrainMCP
from artek_brain.service import Service


def _app(tmp_path: Path, *, max_chars: int = 8000) -> tuple[Service, BrainMCP, str]:
    service = Service(tmp_path / "brain")
    agent = str(service.access.create_token("agent", "agent", [("docs", "write")]))
    return service, BrainMCP(service, agent, max_response_chars=max_chars), agent


def test_mcp_catalog_and_help(tmp_path: Path) -> None:
    service, app, _ = _app(tmp_path)
    try:
        names = [item["name"] for item in app.list_tools()]
        assert names == [
            "brain_help",
            "brain_read",
            "brain_write",
            "brain_search",
            "brain_ls",
            "brain_op",
        ]

        overview, error = app.call("brain_help", {})
        assert error is False
        assert "progressive disclosure" in overview
        assert "brain_op" in overview

        tools_help, error = app.call("brain_help", {"topic": "tool"})
        assert error is False
        assert "Схема:" in tools_help and "Пример:" in tools_help

        history, error = app.call("brain_help", {"topic": "history"})
        assert error is False
        assert "Пример:" in history and "brain_op(action=\"history\"" in history

        prefixed, error = app.call("brain_help", {"topic": "brain_diff"})
        assert error is False and "unified diff" in prefixed

        unknown, error = app.call("brain_help", {"topic": "нет-такой"})
        assert error is False and "Неизвестная тема" in unknown
    finally:
        service.close()


def test_mcp_write_read_search_and_multiplexer(tmp_path: Path) -> None:
    service, app, _ = _app(tmp_path)
    try:
        text, error = app.call(
            "brain_write",
            {
                "path": "docs/a.md",
                "content": "# A\n\nдеплой памяти\n",
                "reason": "создал",
            },
        )
        assert error is False
        assert json.loads(text)["version"] == 1

        text, error = app.call("brain_read", {"path": "docs/a.md"})
        assert error is False
        assert json.loads(text)["content"] == "# A\n\nдеплой памяти\n"

        text, error = app.call("brain_search", {"query": "деплой"})
        assert error is False
        page = json.loads(text)
        assert page["total"] == 1 and page["items"][0]["path"] == "docs/a.md"

        text, error = app.call("brain_ls", {"prefix": "docs", "tree": True})
        assert error is False
        assert json.loads(text)["nodes"][0]["path"] == "docs/a.md"

        text, error = app.call(
            "brain_op", {"action": "history", "path": "docs/a.md"}
        )
        assert error is False
        assert json.loads(text)[0]["n"] == 1

        text, error = app.call("brain_op", {"action": "task_create", "title": "x"})
        assert error is True and "Доски задач нет" in text

        text, error = app.call("brain_op", {"action": "brief"})
        assert error is False and "доски задач нет" in json.loads(text)["brief"]
    finally:
        service.close()


def test_mcp_response_limit_and_cursor(tmp_path: Path) -> None:
    service, _, _ = _app(tmp_path)
    # Токен нужен настоящий: создадим отдельный широкий экземпляр с тем же сервисом.
    agent = str(service.access.create_token("agent-2", "agent", [("docs", "write")]))
    full_app = BrainMCP(service, agent, max_response_chars=100_000)
    narrow = BrainMCP(service, agent, max_response_chars=500)
    try:
        content = "# Длинный\n\n" + ("строка памяти\n" * 300)
        text, error = full_app.call(
            "brain_write",
            {"path": "docs/long.md", "content": content, "reason": "длинно"},
        )
        assert error is False

        full, error = full_app.call("brain_read", {"path": "docs/long.md"})
        assert error is False and len(full) > 500

        chunks: list[str] = []
        cursor: Any = None
        first_len = None
        for _ in range(50):
            args: dict[str, Any] = {"path": "docs/long.md"}
            if cursor is not None:
                args["cursor"] = cursor
            piece, error = narrow.call("brain_read", args)
            assert error is False
            assert len(piece) <= 500
            if first_len is None:
                first_len = len(piece)
            parsed = json.loads(piece)
            assert parsed["truncated"] is True
            chunks.append(parsed["chunk"])
            cursor = parsed["cursor"]
            if cursor is None:
                break
        assert first_len is not None
        assert "".join(chunks) == full
    finally:
        service.close()


def test_mcp_errors_are_actionable_text(tmp_path: Path) -> None:
    service, app, agent = _app(tmp_path)
    try:
        text, error = app.call(
            "brain_write",
            {"path": "docs/a.md", "content": "# A\n", "reason": "создал"},
        )
        assert error is False

        text, error = app.call(
            "brain_write",
            {"path": "docs/a.md", "content": "# B\n", "reason": "без базы"},
        )
        assert error is True
        assert "base_version" in text

        text, error = app.call("brain_read", {"path": "docs/missing.md"})
        assert error is True
        assert "не найден" in text.lower()

        text, error = app.call("brain_op", {})
        assert error is True and "action" in text

        anonymous = BrainMCP(service, None)
        text, error = anonymous.call("brain_read", {"path": "docs/a.md"})
        assert error is True and "токен" in text.lower()
        help_text, help_error = anonymous.call("brain_help", {})
        assert help_error is False and "brain_read" in help_text
    finally:
        service.close()
