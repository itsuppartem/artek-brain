"""Развёртывание мозга внутри проекта: `brain setup`.

Один экземпляр на проект: хранилище в <проект>/.brain, токены в файлах
.brain/owner.token и .brain/agent.token (права 600), .mcp.json для
Claude Code, opencode.json для агентов opencode, правила в AGENTS.md,
хук брифа в .claude/settings.json. MCP ищет .brain вверх от текущего
каталога, поэтому git-копии агентов внутри проекта пишут в ту же память.
Повторный запуск безопасен: данные не перезаписываются, обновляются только блок правил в AGENTS.md между
маркерами и метки клиентов BRAIN_CLIENT; добавляется хук брифа.
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
У проекта есть общая память: MCP-сервер `brain`. Она общая для всех сессий,
субагентов и агентов opencode: что не записано в мозг, следующий агент не узнает.
- В начале работы и после сжатия контекста: `brain_op(action="brief")` —
  ориентиры, что устарело и что недавно менялось (хук может подложить его сам).
  Потом `brain_read` нужных документов (`README.md`, `backlog.md` и т. п.).
- Перед правкой незнакомой части ищи: `brain_search("одно-два слова")`.
- Ответ обрезан (`truncated`, `remaining_sections`) — дочитай по `next.line_start`
  или `section`, прежде чем опираться на документ.
- Ответ содержит `freshness` или `stale` — сверь факты с кодом и сервером. Потом
  обнови документ или подтверди: `brain_op(action="confirm_fresh", path=…, reason=…)`.
  Открытая задача не исчезнет сама и через несколько дней уйдёт владельцу.
- Опираешься на решение — ставь ссылку с якорем: `docs/decisions.md#раздел`.
  Изменится раздел, и документ получит задачу на проверку.
- Итог работы: заметка в `notes/` и правка справочного документа в `docs/`
  (или `backlog.md`), если поменялись факты, решения или грабли. Только заметка —
  это половина работы: docs остаются старыми.
- Даёшь задачу субагенту или другой сессии — пиши в промпте: «сначала
  `brain_op(action="brief")`, итог — в мозг».
- `README.md`, `rules/`, `approved/`, `owner/` и документы решений меняет только
  владелец (решения можно открыть агентам настройкой `decisions.require_owner`);
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
        entry = servers[MCP_SERVER_NAME]
        env = entry.setdefault("env", {}) if isinstance(entry, dict) else None
        if env is None or env.get("BRAIN_CLIENT"):
            return False
        env["BRAIN_CLIENT"] = "claude-code"
    else:
        servers[MCP_SERVER_NAME] = {
            "command": command[0],
            "args": [*command[1:], "mcp"],
            "env": {"BRAIN_CLIENT": "claude-code"},
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
        entry = servers[MCP_SERVER_NAME]
        env = entry.setdefault("environment", {}) if isinstance(entry, dict) else None
        if env is None or env.get("BRAIN_CLIENT"):
            return False
        env["BRAIN_CLIENT"] = "opencode"
    else:
        servers[MCP_SERVER_NAME] = {
            "type": "local",
            "command": [*command, "mcp"],
            "enabled": True,
            "environment": {"BRAIN_CLIENT": "opencode"},
        }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def _ensure_claude_hook(project: Path, command: list[str]) -> bool:
    """Хук Claude Code: бриф памяти в контекст на старте и после сжатия.

    Пишет .claude/settings.json проекта (SessionStart, matcher
    startup|resume|clear|compact). Существующие хуки не трогаются. Если
    такой хук уже есть в пользовательском ~/.claude/settings.json,
    проектный не ставится.
    """
    user_settings = Path(
        os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    ) / "settings.json"
    try:
        if "hook session-start" in user_settings.read_text(encoding="utf-8"):
            # Хук уже стоит у пользователя и работает во всех проектах и
            # git-копиях; второй дал бы бриф дважды.
            return False
    except OSError:
        pass
    path = project / ".claude" / "settings.json"
    data: dict[str, Any] = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    hook_command = " ".join([*(_quote(part) for part in command), "hook", "session-start"])
    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault("SessionStart", [])
    for group in groups:
        for item in group.get("hooks", []) if isinstance(group, dict) else []:
            if "hook session-start" in str(item.get("command", "")):
                return False
    groups.append(
        {
            "matcher": "startup|resume|clear|compact",
            "hooks": [{"type": "command", "command": hook_command, "timeout": 15}],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def _quote(part: str) -> str:
    import shlex

    return shlex.quote(part)


def _ensure_agents(project: Path) -> bool:
    """Дописать блок правил в AGENTS.md или обновить его между маркерами.

    Текст вне маркеров не трогается.
    """
    path = project / "AGENTS.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = _AGENTS_BLOCK.format(begin=MARK_BEGIN, end=MARK_END)
    if MARK_BEGIN in text and MARK_END in text:
        start = text.index(MARK_BEGIN)
        end = text.index(MARK_END, start) + len(MARK_END)
        if text[end : end + 1] == "\n":
            end += 1
        updated = text[:start] + block + text[end:]
        if updated == text:
            return False
        path.write_text(updated, encoding="utf-8")
        return True
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
    if _ensure_claude_hook(project, command):
        created.append(".claude/settings.json")
    if _ensure_agents(project):
        created.append("AGENTS.md")
    return {
        "project": str(project),
        "root": str(root),
        "created": created,
        "owner_token_file": str(root / OWNER_TOKEN_FILE),
        "agent_token_file": str(root / AGENT_TOKEN_FILE),
    }
