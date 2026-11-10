"""HTTP API Artek Brain: авторизация, операции и лимиты."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from urllib.parse import quote
from pathlib import Path
from typing import Any

from artek_brain.http_api import create_server
from artek_brain.service import Service


class Client:
    """Клиент HTTP API для тестов."""

    def __init__(self, base: str) -> None:
        self.base = base

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base + quote(path, safe="/?&=%:"), data=data, method=method
        )
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


def _start(service: Service, **kwargs: Any) -> tuple[Any, Client]:
    server = create_server(service, "127.0.0.1", 0, quiet=True, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, Client(f"http://{host}:{port}")


def _stop(server: Any) -> None:
    server.shutdown()
    server.server_close()


def test_http_auth_scope_and_roundtrip(tmp_path: Path) -> None:
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    agent = str(service.access.create_token("agent", "agent", [("docs", "write")]))
    server, client = _start(service)
    try:
        status, _ = client.call("GET", "/documents/docs/a.md")
        assert status == 401

        status, _ = client.call("GET", "/documents/docs/a.md", token="invalid-token")
        assert status == 401

        status, written = client.call(
            "POST",
            "/documents/docs/a.md",
            {"content": "# A\n\nпривет\n", "reason": "создал"},
            token=agent,
        )
        assert status == 200 and written["version"] == 1

        status, read = client.call("GET", "/documents/docs/a.md", token=agent)
        assert status == 200 and read["content"] == "# A\n\nпривет\n"

        status, _ = client.call(
            "POST",
            "/documents/other/a.md",
            {"content": "x", "reason": "мимо"},
            token=agent,
        )
        assert status == 403

        status, _ = client.call("GET", "/stats", token=agent)
        assert status == 403
        status, stats = client.call("GET", "/stats", token=owner)
        assert status == 200 and stats["documents"] == 1

        status, found = client.call(
            "GET", "/search?q=привет&prefix=docs", token=agent
        )
        assert status == 200 and found["total"] == 1

        status, tree = client.call("GET", "/tree?prefix=docs", token=agent)
        assert status == 200 and tree["nodes"]
    finally:
        _stop(server)
        service.close()


def test_http_openapi_and_rpc(tmp_path: Path) -> None:
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    server, client = _start(service)
    try:
        status, spec = client.call("GET", "/openapi.json")
        assert status == 200 and spec["openapi"].startswith("3.")
        assert "/documents/{path}" in spec["paths"]

        status, written = client.call(
            "POST",
            "/rpc",
            {
                "op": "write",
                "path": "docs/rpc.md",
                "content": "# RPC\n",
                "reason": "через rpc",
            },
            token=owner,
        )
        assert status == 200 and written["version"] == 1

        status, read = client.call(
            "GET", "/rpc?op=read&path=docs/rpc.md", token=owner
        )
        # GET /rpc не маршрутизируется, но POST /rpc с op=read работает.
        assert status == 404
        status, read = client.call(
            "POST", "/rpc", {"op": "read", "path": "docs/rpc.md"}, token=owner
        )
        assert status == 200 and read["content"] == "# RPC\n"
    finally:
        _stop(server)
        service.close()


def test_http_body_limit_and_token_endpoints(tmp_path: Path) -> None:
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    server, client = _start(service, max_body=200)
    try:
        status, error = client.call(
            "POST",
            "/documents/docs/big.md",
            {"content": "x" * 500, "reason": "слишком много"},
            token=owner,
        )
        assert status == 413
        assert error["error"]["type"] == "HttpError"

        status, issued = client.call(
            "POST",
            "/tokens",
            {"name": "new-agent", "role": "agent", "scopes": ["docs:read"]},
            token=owner,
        )
        assert status == 200 and issued["token"].startswith("ab_")

        status, listed = client.call("GET", "/tokens", token=owner)
        assert status == 200
        assert any(item["name"] == "new-agent" for item in listed)

        token_id = issued["id"]
        status, revoked = client.call(
            "DELETE", f"/tokens/{token_id}", token=owner
        )
        assert status == 200 and revoked["revoked"] == 1

        status, _ = client.call(
            "GET", "/documents/docs/a.md", token=issued["token"]
        )
        assert status == 401
    finally:
        _stop(server)
        service.close()


def test_http_tasks_inbox_and_brief(tmp_path: Path) -> None:
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    agent = str(service.access.create_token("agent", "agent", [("docs", "write")]))
    server, client = _start(service)
    try:
        status, task = client.call(
            "POST",
            "/tasks",
            {"title": "HTTP task", "body": "body", "area": ["docs"]},
            token=owner,
        )
        assert status == 200 and task["id"].startswith("DOCS-")

        status, claimed = client.call(
            "POST",
            f"/tasks/{task['id']}/claim",
            {"revision": 1},
            token=agent,
        )
        assert status == 200 and claimed["status"] == "claimed"

        status, listed = client.call("GET", "/tasks", token=agent)
        assert status == 200 and listed[0]["id"] == task["id"]

        status, sent = client.call(
            "POST",
            "/inbox",
            {"to": "agent", "subject": "hi", "body": "there"},
            token=owner,
        )
        assert status == 200 and sent["subject"] == "hi"

        status, inbox = client.call("GET", "/inbox?unread=1", token=agent)
        assert status == 200 and len(inbox) == 1

        status, brief = client.call("GET", "/brief", token=agent)
        assert status == 200 and "Бриф" in brief["brief"]
    finally:
        _stop(server)
        service.close()
