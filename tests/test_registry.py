"""Реестр инструментов: права, лимиты, секреты и аудит."""

from __future__ import annotations

from pathlib import Path

import pytest

from artek_brain import PermissionDenied, ToolDisabled, ToolLimitError, ToolNotFound
from artek_brain.service import Service


def _service(tmp_path: Path) -> tuple[Service, str, str, str]:
    service = Service(tmp_path / "brain")
    owner = str(service.access.create_token("owner", "owner"))
    agent = str(service.access.create_token("agent-1", "agent", [("docs", "read")]))
    auditor = str(service.access.create_token("auditor-1", "auditor"))
    return service, owner, agent, auditor


def test_sandbox_is_first_registered_tool(tmp_path: Path) -> None:
    service, _, agent, _ = _service(tmp_path)
    try:
        tools = service.registry.list_tools()
        assert tools and tools[0].name == "sandbox.run"
        assert tools[0].min_role == "agent"
        assert tools[0].enabled is True
        assert tools[0].input_schema["required"] == ["script"]
    finally:
        service.close()


def test_registry_limits_and_permissions(tmp_path: Path) -> None:
    service, owner, agent, auditor = _service(tmp_path)
    try:
        service.registry.register(
            "demo.echo",
            "Тестовый инструмент",
            {"type": "object", "properties": {"x": {"type": "string"}}},
            min_role="agent",
            daily_limit=2,
            handler=lambda token, args: {"echo": args.get("x")},
        )
        assert service.registry.call("demo.echo", {"x": "one"}, agent) == {
            "echo": "one"
        }
        assert service.registry.call("demo.echo", {"x": "two"}, agent) == {
            "echo": "two"
        }
        with pytest.raises(ToolLimitError):
            service.registry.call("demo.echo", {"x": "three"}, agent)

        with pytest.raises(PermissionDenied):
            service.registry.call("demo.echo", {"x": "no"}, auditor)

        with pytest.raises(ToolNotFound):
            service.registry.call("no.such.tool", {}, owner)

        calls = service.registry.calls("demo.echo")
        assert len(calls) == 2 and all(call["ok"] for call in calls)
        audit = service.brain.audit(owner, action="tool_call", limit=20)
        assert any(item.path == "demo.echo" for item in audit)
    finally:
        service.close()


def test_disabled_tool_and_secrets_are_hidden(tmp_path: Path) -> None:
    service, owner, agent, _ = _service(tmp_path)
    try:
        service.registry.register(
            "demo.secret",
            "Инструмент с секретом",
            {"type": "object"},
            min_role="agent",
            enabled=False,
            handler=lambda token, args: {"ok": True},
        )
        service.registry.set_secret("demo.secret", "api_token", "secret-value")
        with pytest.raises(ToolDisabled):
            service.registry.call("demo.secret", {}, agent)

        public = service.registry.list_tools()
        dumped = repr([item.as_dict() for item in public])
        assert "secret-value" not in dumped
        assert service.registry.get("demo.secret").as_dict().get("secret") is None

        service.registry.register(
            "demo.secret",
            "Инструмент с секретом",
            {"type": "object"},
            min_role="agent",
            enabled=True,
            handler=lambda token, args: {"value": service.registry._get_secret("demo.secret", "api_token")},
        )
        # Секрет доступен только обработчику, не интерфейсу.
        result = service.registry.call("demo.secret", {}, agent)
        assert result == {"value": "secret-value"}
    finally:
        service.close()


def test_auditor_cannot_use_sandbox_via_registry(tmp_path: Path) -> None:
    service, _, _, auditor = _service(tmp_path)
    try:
        with pytest.raises(PermissionDenied):
            service.registry.call(
                "sandbox.run", {"script": "print(1)\n", "language": "python"}, auditor
            )
    finally:
        service.close()
