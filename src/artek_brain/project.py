"""Развёртывание мозга внутри проекта: `brain setup`.

Один экземпляр на проект: хранилище в <проект>/.brain, токены в файлах
.brain/owner.token и .brain/agent.token (права 600), .mcp.json для
Claude Code, opencode.json для агентов opencode, правила в AGENTS.md.\nMCP ищет .brain вверх от текущего каталога, поэтому git-копии агентов\nвнутри проекта пишут в ту же память. Повторный запуск безопасен:
ничего существующего не перезаписывается.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .errors import DeletedError, NotFoundError
from .service import Service

ROOT_DIR = ".brain"
OWNER_TOKEN_FILE = "owner.token"
AGENT_TOKEN_FILE = "agent.token"
MCP_SERVER_NAME = "brain"
MARK_BEGIN = "<!-- BEGIN:artek-brain -->"
MARK_END = "<!-- END:artek-brain -->"

_README = """# {name}: память проекта

Общая память людей и агентов проекта «{name}». Пишется через brain_write,
каждая правка — версия с автором и причиной.

## Карта
- README.md — эта страница (меняет только владелец).
- backlog.md — что делаем сейчас и дальше.
- docs/ — устройство проекта, решения, договорённости.
- notes/ — заметки агентов по итогам работы.
- owner/inbox/ — предложения агентов владельцу.
- rules/, approved/ — правила и утверждённое знание (только владелец).
"""

_BACKLOG = """# Бэклог «{name}»

## Сейчас
- (пусто)

## Дальше
- (пусто)
"""

_AGENTS_BLOCK = """{begin}
## Память проекта (artek-brain)
У проекта есть общая память: MCP-сервер `brain`.
- В начале работы: `brain_read("README.md")` и `brain_read("backlog.md")`.
- Перед изменением незнакомой части ищи: `brain_search("...")`.
- Итог работы, решения и найденные грабли записывай: `brain_write` в
  `notes/` или `docs/` с понятной причиной (reason).
- `README.md`, `rules/`, `approved/`, `owner/` меняет только владелец;
  предложения клади в `owner/inbox/<имя>.md`.
- Секреты в память не пиши: запись с ключом будет отклонена.
{end}
"""


def read_token_file(root: Path, name: str = AGENT_TOKEN_FILE) -> str | None:
    """Прочитать токен из файла в каталоге хранилища, если он есть."""
    path = Path(root) / name
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _write_secret(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    path.chmod(0o600)


def _brain_command() -> list[str]:
    """Команда запуска MCP: абсолютный путь к brain или python -m."""
    found = shutil.which("brain")
    if found:
        return [found]
    return [sys.executable, "-m", "artek_brain"]


def _ensure_gitignore(project: Path) -> bool:
    path = project / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = {line.strip() for line in text.splitlines()}
    if {".brain", ".brain/", "/.brain", "/.brain/"} & lines:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + ".brain/\n", encoding="utf-8")
    return True


def _ensure_mcp(project: Path, command: list[str]) -> bool:
    path = project / ".mcp.json"
    data: dict[str, Any] = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    servers = data.setdefault("mcpServers", {})
    if MCP_SERVER_NAME in servers:
        return False
    servers[MCP_SERVER_NAME] = {
        "command": command[0],
        "args": [*command[1:], "mcp"],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def _ensure_opencode(project: Path, command: list[str]) -> bool:
    """Подключить мозг к агентам opencode (opencode.json проекта)."""
    path = project / "opencode.json"
    data: dict[str, Any] = {"$schema": "https://opencode.ai/config.json"}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    servers = data.setdefault("mcp", {})
    if MCP_SERVER_NAME in servers:
        return False
    servers[MCP_SERVER_NAME] = {
        "type": "local",
        "command": [*command, "mcp"],
        "enabled": True,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def _ensure_agents(project: Path) -> bool:
    path = project / "AGENTS.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if MARK_BEGIN in text:
        return False
    block = _AGENTS_BLOCK.format(begin=MARK_BEGIN, end=MARK_END)
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + ("\n" if text else "") + block, encoding="utf-8")
    claude = project / "CLAUDE.md"
    if claude.exists():
        claude_text = claude.read_text(encoding="utf-8")
        if "AGENTS.md" not in claude_text:
            sep = "" if claude_text.endswith("\n") or not claude_text else "\n"
            claude.write_text(claude_text + sep + "@AGENTS.md\n", encoding="utf-8")
    else:
        claude.write_text("@AGENTS.md\n", encoding="utf-8")
    return True


def setup_project(project: str | Path, name: str | None = None) -> dict[str, Any]:
    """Развернуть мозг в каталоге проекта. Идемпотентно.

    Возвращает, что создано, и пути к файлам токенов (сами токены не печатаем).
    """
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise NotFoundError(f"Каталог проекта не найден: {project}.")
    title = name or project.name
    root = project / ROOT_DIR
    created: list[str] = []
    service = Service(root)
    try:
        owners = [t for t in service.access.list_tokens() if t.role == "owner"]
        owner_file = root / OWNER_TOKEN_FILE
        agent_file = root / AGENT_TOKEN_FILE
        if not owners:
            owner = service.access.create_token("owner", "owner")
            _write_secret(owner_file, str(owner))
            created.append(f"{ROOT_DIR}/{OWNER_TOKEN_FILE}")
        if not agent_file.exists():
            agent = service.access.create_token("agent", "agent", scopes=[("", "write")])
            _write_secret(agent_file, str(agent))
            created.append(f"{ROOT_DIR}/{AGENT_TOKEN_FILE}")
        for path, template in (("README.md", _README), ("backlog.md", _BACKLOG)):
            try:
                service.store.read(path)
            except DeletedError:
                continue
            except NotFoundError:
                service.store.write(
                    path, template.format(name=title), "owner", "brain setup: старт памяти"
                )
                created.append(f"память:{path}")
    finally:
        service.close()
    if _ensure_gitignore(project):
        created.append(".gitignore")
    command = _brain_command()
    if _ensure_mcp(project, command):
        created.append(".mcp.json")
    if _ensure_opencode(project, command):
        created.append("opencode.json")
    if _ensure_agents(project):
        created.append("AGENTS.md")
    return {
        "project": str(project),
        "root": str(root),
        "created": created,
        "owner_token_file": str(root / OWNER_TOKEN_FILE),
        "agent_token_file": str(root / AGENT_TOKEN_FILE),
    }
