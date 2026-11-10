"""MCP stdio сервер Artek Brain с progressive disclosure.

При старте клиент видит только шесть инструментов. Остальные операции
доступны через brain_task(action=...) и описаны в brain_help(topic).
Ответы ограничены по размеру, для продолжения отдаётся cursor.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from typing import Any, Callable

from .errors import ArtekBrainError, ValidationError
from .service import Service

DEFAULT_MAX_CHARS = 8000

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "brain_help",
        "description": (
            "Справка по памяти. Без темы — обзор и список действий; с темой "
            "(например read, history, rollback, task_claim) — схема и пример."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Тема справки или имя действия.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "brain_read",
        "description": "Прочитать документ, версию, раздел или диапазон строк.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Логический путь документа."},
                "version": {"type": "integer", "description": "Номер версии."},
                "section": {"type": "string", "description": "Заголовок раздела."},
                "line_start": {"type": "integer", "description": "Первая строка, с 1."},
                "line_end": {"type": "integer", "description": "Последняя строка."},
                "max_chars": {"type": "integer", "description": "Лимит символов содержимого."},
                "cursor": {"type": "string", "description": "Курсор продолжения ответа."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "brain_write",
        "description": "Записать новую версию документа с причиной и base_version.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "reason": {"type": "string", "description": "Зачем меняем документ."},
                "base_version": {"type": "integer"},
                "expected_sha256": {"type": "string"},
                "batch_id": {"type": "string"},
                "cursor": {"type": "string"},
            },
            "required": ["path", "content", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "brain_search",
        "description": "Полнотекстовый поиск с фильтрами и пагинацией.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path_prefix": {"type": "string"},
                "type": {"type": "string"},
                "status": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "author": {"type": "string"},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
                "history": {"type": "boolean"},
                "cursor": {"type": "string"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "brain_ls",
        "description": "Список каталога или дерево памяти.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string"},
                "tree": {"type": "boolean"},
                "max_depth": {"type": "integer"},
                "cursor": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "brain_task",
        "description": (
            "Мультиплексор: action=history, diff, rollback, audit, stats, "
            "task_create, task_claim, inbox, send и другие. "
            "Схемы — в brain_help(\"<action>\")."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Имя действия; список в brain_help().",
                },
                "cursor": {"type": "string", "description": "Курсор продолжения ответа."},
            },
            "required": ["action"],
            "additionalProperties": True,
        },
    },
]

_TOOL_NAMES = [item["name"] for item in _TOOL_DEFS]

# Схемы и примеры для brain_help. Ключи — канонические имена действий.
_ACTION_HELP: dict[str, tuple[str, str, str]] = {
    "read": (
        "Прочитать документ. Параметры: path (обязателен), version, section, "
        "line_start, line_end, max_chars, cursor.",
        "brain_read(path=\"docs/a.md\", version=2)",
        "Возвращает path, version, content, truncated, total_chars.",
    ),
    "write": (
        "Записать версию. Параметры: path, content, reason (обязательны), "
        "base_version, expected_sha256, batch_id.",
        "brain_write(path=\"docs/a.md\", content=\"# A\\n\", reason=\"правка\", base_version=1)",
        "Возвращает version, sha256, op, merged; при устаревшей базе — конфликт.",
    ),
    "search": (
        "Поиск. Параметры: query (обязателен), path_prefix, type, status, tags, "
        "author, since, until, limit, offset, history.",
        "brain_search(query=\"деплой\", path_prefix=\"docs\")",
        "Возвращает items (path, version, title, snippet), total, offset, has_more.",
    ),
    "ls": (
        "Список каталога. Параметры: prefix, tree, max_depth.",
        "brain_ls(prefix=\"docs\", tree=True, max_depth=2)",
        "Возвращает nodes с path, kind, size, versions.",
    ),
    "help": (
        "Справка. Параметр: topic. Без темы — обзор и все действия.",
        "brain_help(topic=\"history\")",
        "Возвращает текст справки.",
    ),
    "task": (
        "Мультиплексор brain_task. Параметр action плюс поля нужного действия.",
        "brain_task(action=\"history\", path=\"docs/a.md\")",
        "Схемы отдельных action — в brain_help(\"<action>\").",
    ),
    "history": (
        "История версий. Параметры: path (обязателен), limit=50, offset=0.",
        "brain_task(action=\"history\", path=\"docs/a.md\", limit=10)",
        "Возвращает список версий: n, parent_n, sha256, author, ts, op, reason.",
    ),
    "diff": (
        "Сравнить версии. Параметры: path (обязателен), a, b.",
        "brain_task(action=\"diff\", path=\"docs/a.md\", a=1, b=2)",
        "Возвращает unified diff.",
    ),
    "blame": (
        "Авторы разделов. Параметр: path.",
        "brain_task(action=\"blame\", path=\"docs/a.md\")",
        "Возвращает title, level, version, author, ts по разделам.",
    ),
    "toc": (
        "Оглавление документа. Параметры: path, version.",
        "brain_task(action=\"toc\", path=\"docs/a.md\")",
        "Возвращает разделы с chars и lines.",
    ),
    "links": (
        "Исходящие ссылки. Параметры: path, version.",
        "brain_task(action=\"links\", path=\"docs/a.md\")",
        "Возвращает source, target, exists, external.",
    ),
    "backlinks": (
        "Обратные ссылки. Параметр: path.",
        "brain_task(action=\"backlinks\", path=\"docs/a.md\")",
        "Возвращает документы, которые ссылаются на path.",
    ),
    "broken_links": (
        "Битые ссылки. Параметр: prefix.",
        "brain_task(action=\"broken_links\", prefix=\"docs\")",
        "Возвращает ссылки с exists=false.",
    ),
    "changes": (
        "Лента изменений после курсора. Параметры: cursor, limit=100.",
        "brain_task(action=\"changes\", cursor=0, limit=50)",
        "Возвращает items, cursor, next_cursor, has_more.",
    ),
    "readme": (
        "Карта памяти из README и верхнего уровня. Параметров нет.",
        "brain_task(action=\"readme\")",
        "Возвращает readme, sections, links, top_level.",
    ),
    "delete": (
        "Мягко удалить документ. Параметры: path, reason (обязательны), "
        "base_version, batch_id.",
        "brain_task(action=\"delete\", path=\"docs/a.md\", reason=\"устарело\")",
        "Возвращает version и op=delete; история сохраняется.",
    ),
    "restore": (
        "Восстановить документ. Параметры: path, reason (обязательны), "
        "base_version, batch_id.",
        "brain_task(action=\"restore\", path=\"docs/a.md\", reason=\"вернуть\")",
        "Возвращает version и op=restore.",
    ),
    "rollback_version": (
        "Откат к версии. Параметры: path, to_version, reason.",
        "brain_task(action=\"rollback_version\", path=\"docs/a.md\", to_version=1, reason=\"ошибочная правка\")",
        "Создаёт новую версию со старым содержимым.",
    ),
    "rollback_author": (
        "Откат правок автора за период. Параметры: author, since, until, reason.",
        "brain_task(action=\"rollback_author\", author=\"agent-1\", since=\"2026-01-01\", reason=\"пачка\")",
        "Возвращает rolled_back и skipped с причинами.",
    ),
    "rollback_batch": (
        "Откат пачки. Параметры: batch_id, reason.",
        "brain_task(action=\"rollback_batch\", batch_id=\"b-1\", reason=\"ошибочная пачка\")",
        "Возвращает rolled_back и skipped.",
    ),
    "export": (
        "Экспорт Markdown. Параметры: path, version.",
        "brain_task(action=\"export\", path=\"docs/a.md\")",
        "Возвращает content.",
    ),
    "audit": (
        "Журнал аудита (owner/auditor). Параметры: limit, actor, action, since.",
        "brain_task(action=\"audit\", limit=20, action=\"write\")",
        "Возвращает записи ts, actor, action, path, detail.",
    ),
    "stats": (
        "Статистика (owner/auditor). Параметров нет.",
        "brain_task(action=\"stats\")",
        "Возвращает documents, versions, bytes, top_authors, errors.",
    ),
    "who_knows": (
        "Кто последним менял путь. Параметр: path.",
        "brain_task(action=\"who_knows\", path=\"docs/a.md\")",
        "Возвращает author, version, ts, reason.",
    ),
    "brief": (
        "Бриф агента. Параметр: limit=2000.",
        "brain_task(action=\"brief\")",
        "Возвращает короткий текст: задачи, области, handoff, inbox.",
    ),
    "task_create": (
        "Создать задачу. Параметры: title, body, area (обязательны area), "
        "priority, depends_on, key.",
        "brain_task(action=\"task_create\", title=\"Fix API\", body=\"...\", area=[\"docs\"])",
        "Возвращает задачу с id вида AREA-NNN.",
    ),
    "task_list": (
        "Список задач. Параметры: status, assignee, area, priority, limit, offset.",
        "brain_task(action=\"task_list\", status=\"todo\")",
        "Возвращает задачи по приоритету и id.",
    ),
    "task_show": (
        "Показать задачу. Параметр: task_id.",
        "brain_task(action=\"task_show\", task_id=\"DOCS-001\")",
        "Возвращает задачу целиком.",
    ),
    "task_claim": (
        "Взять задачу в аренду. Параметры: task_id, revision (обязательны), minutes=30.",
        "brain_task(action=\"task_claim\", task_id=\"DOCS-001\", revision=1, minutes=30)",
        "Возвращает задачу и claim_token — сохраните его.",
    ),
    "task_start": (
        "Начать взятую задачу. Параметры: task_id, revision, claim_token.",
        "brain_task(action=\"task_start\", task_id=\"DOCS-001\", revision=2, claim_token=\"...\")",
        "Переводит задачу в in_progress.",
    ),
    "task_heartbeat": (
        "Продлить аренду. Параметры: claim_token, minutes=30, revision.",
        "brain_task(action=\"task_heartbeat\", claim_token=\"...\", minutes=30)",
        "Возвращает новый срок аренды.",
    ),
    "task_release": (
        "Освободить задачу. Параметры: task_id, revision или claim_token.",
        "brain_task(action=\"task_release\", task_id=\"DOCS-001\", claim_token=\"...\")",
        "Возвращает задачу в todo.",
    ),
    "task_submit": (
        "Отправить на ревью. Параметры: task_id, revision, evidence (обязательны), claim_token.",
        "brain_task(action=\"task_submit\", task_id=\"DOCS-001\", revision=3, evidence=\"pytest -q: 100 passed\")",
        "Переводит задачу в review.",
    ),
    "task_accept": (
        "Принять задачу. Параметры: task_id, revision, note. Нужна роль reviewer/owner.",
        "brain_task(action=\"task_accept\", task_id=\"DOCS-001\", revision=4)",
        "Переводит задачу в done.",
    ),
    "task_rework": (
        "Вернуть на доработку. Параметры: task_id, revision, reason.",
        "brain_task(action=\"task_rework\", task_id=\"DOCS-001\", revision=4, reason=\"нет тестов\")",
        "Переводит задачу в rework.",
    ),
    "task_block": (
        "Заблокировать задачу. Параметры: task_id, revision, reason.",
        "brain_task(action=\"task_block\", task_id=\"DOCS-001\", revision=2, reason=\"ждём API\")",
        "Переводит задачу в blocked.",
    ),
    "task_cancel": (
        "Отменить задачу. Параметры: task_id, revision, reason.",
        "brain_task(action=\"task_cancel\", task_id=\"DOCS-001\", revision=2, reason=\"не нужна\")",
        "Переводит задачу в cancelled.",
    ),
    "task_grant_role": (
        "Выдать роль на доске (owner). Параметры: actor, role.",
        "brain_task(action=\"task_grant_role\", actor=\"human-1\", role=\"reviewer\")",
        "Роли: reviewer, owner, admin, agent, human, auditor.",
    ),
    "task_revoke_role": (
        "Забрать роль на доске (owner). Параметры: actor, role.",
        "brain_task(action=\"task_revoke_role\", actor=\"human-1\", role=\"reviewer\")",
        "Удаляет роль у участника.",
    ),
    "task_roles": (
        "Роли участника. Параметр: actor (по умолчанию вы).",
        "brain_task(action=\"task_roles\", actor=\"agent-1\")",
        "Возвращает список ролей.",
    ),
    "presence": (
        "Отметить агента онлайн. Параметр: activity.",
        "brain_task(action=\"presence\", activity=\"пишу тесты\")",
        "Возвращает name, last_seen, activity.",
    ),
    "agents": (
        "Кто онлайн. Параметр: ttl_seconds=300.",
        "brain_task(action=\"agents\", ttl_seconds=600)",
        "Возвращает список присутствующих.",
    ),
    "handoff": (
        "Сдать смену. Параметры: task, done, left, important.",
        "brain_task(action=\"handoff\", task=\"DOCS-001\", done=\"сделал API\", left=\"тесты\")",
        "Пишет handoffs/<task>.md в память.",
    ),
    "inbox": (
        "Входящие. Параметр: unread=true/false.",
        "brain_task(action=\"inbox\", unread=True)",
        "Возвращает письма: id, sender, subject, body, read.",
    ),
    "send": (
        "Отправить письмо. Параметры: to, subject, body.",
        "brain_task(action=\"send\", to=\"human-1\", subject=\"Готово\", body=\"...\")",
        "Возвращает письмо; отправитель берётся из токена.",
    ),
    "mark_read": (
        "Отметить письма прочитанными. Параметр: message_id (число или список).",
        "brain_task(action=\"mark_read\", message_id=[1, 2])",
        "Возвращает changed — сколько писем изменено.",
    ),
    "token_create": (
        "Создать токен (owner). Параметры: name, role, scopes, ttl.",
        "brain_task(action=\"token_create\", name=\"agent-1\", role=\"agent\", scopes=[\"docs:write\"])",
        "Возвращает token один раз и метаданные.",
    ),
    "token_list": (
        "Список токенов (owner). Параметров нет.",
        "brain_task(action=\"token_list\")",
        "Возвращает токены без секретов.",
    ),
    "token_revoke": (
        "Отозвать токен (owner). Параметры: token_id, value или name.",
        "brain_task(action=\"token_revoke\", token_id=3)",
        "Возвращает число отозванных.",
    ),
}

_ALIASES = {
    "get": "read",
    "show": "task_show",
    "list": "task_list",
    "ls": "ls",
    "tree": "ls",
    "rollback": "rollback_version",
    "rollback-to": "rollback_version",
    "rollback_by_author": "rollback_author",
    "rollback_by_batch": "rollback_batch",
    "who-knows": "who_knows",
    "inbox_list": "inbox",
    "inbox_send": "send",
    "inbox_read": "mark_read",
    "task_claim": "task_claim",
    "claim": "task_claim",
    "start": "task_start",
    "heartbeat": "task_heartbeat",
    "release": "task_release",
    "submit": "task_submit",
    "accept": "task_accept",
    "rework": "task_rework",
    "block": "task_block",
    "cancel": "task_cancel",
    "grant_role": "task_grant_role",
    "grant-role": "task_grant_role",
    "revoke_role": "task_revoke_role",
    "revoke-role": "task_revoke_role",
    "roles": "task_roles",
    "create": "task_create",
    "send": "send",
    "mark_read": "mark_read",
    "mark-read": "mark_read",
}

_MEMORY_ACTIONS = {
    "read", "write", "search", "history", "diff", "blame", "toc", "links",
    "backlinks", "broken_links", "changes", "readme", "delete", "restore",
    "rollback_version", "rollback_author", "rollback_batch", "export",
    "audit", "stats", "who_knows", "brief", "token_create", "token_list",
    "token_revoke",
}

_TASK_ACTIONS = {
    "task_create", "task_list", "task_show", "task_claim", "task_start",
    "task_heartbeat", "task_release", "task_submit", "task_accept",
    "task_rework", "task_block", "task_cancel", "task_grant_role",
    "task_revoke_role", "task_roles", "presence", "agents", "handoff",
    "inbox", "send", "mark_read",
}


def _canonical(action: str) -> str:
    value = str(action or "").strip().lower()
    if value.startswith("brain_"):
        value = value[6:]
    return _ALIASES.get(value, value)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _without(params: dict[str, Any], *names: str) -> dict[str, Any]:
    return {key: value for key, value in params.items() if key not in names}


class BrainMCP:
    """Обработчики MCP без привязки к транспорту (удобно для тестов)."""

    def __init__(
        self,
        service: Service,
        token: str | None,
        *,
        max_response_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        if max_response_chars < 200:
            raise ValidationError(
                "max_response_chars слишком мал.",
                hint="Задайте не меньше 200 символов.",
            )
        self.service = service
        self.token = token
        self.max_response_chars = int(max_response_chars)

    # ------------------------------------------------------------------
    # Каталог и справка
    # ------------------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        """Шесть стартовых инструментов MCP."""
        return [dict(item) for item in _TOOL_DEFS]

    def help(self, topic: str = "") -> str:
        """Текст справки по обзору или конкретному действию."""
        raw = str(topic or "").strip()
        key = _canonical(raw)
        if not raw:
            lines = [
                "# Artek Brain: справка",
                "",
                "Стартовые инструменты (progressive disclosure):",
                "- brain_help(topic) — эта справка;",
                "- brain_read(path, ...) — чтение;",
                "- brain_write(path, content, reason, ...) — запись;",
                "- brain_search(query, ...) — поиск;",
                "- brain_ls(prefix, tree) — навигация;",
                "- brain_task(action, ...) — все остальные операции.",
                "",
                "Остальное вызывается через brain_task(action=\"...\"):",
                ", ".join(sorted(_MEMORY_ACTIONS | _TASK_ACTIONS)),
                "",
                "Пример: brain_help(topic=\"history\") покажет схему и пример.",
                "Ответы ограничены по размеру; продолжение — по cursor.",
            ]
            return "\n".join(lines)
        if key in _ACTION_HELP:
            description, example, returns = _ACTION_HELP[key]
            return (
                f"# {key}\n\n{description}\n\n"
                f"Пример:\n{example}\n\n"
                f"Результат: {returns}"
            )
        if key in ("tools", "tool"):
            return self._tools_help()
        if key in ("overview", "start"):
            return self.help("")
        known = sorted(set(_ACTION_HELP) | set(_ALIASES))
        return (
            f"Неизвестная тема «{raw}».\n"
            "Доступные темы: " + ", ".join(known) + ".\n"
            "Без темы — обзор."
        )

    def _tools_help(self) -> str:
        """Показать схемы и примеры шести стартовых инструментов."""
        examples = {
            "brain_help": 'brain_help(topic="history")',
            "brain_read": 'brain_read(path="docs/a.md", version=1)',
            "brain_write": 'brain_write(path="docs/a.md", content="# A", reason="правка", base_version=1)',
            "brain_search": 'brain_search(query="деплой", path_prefix="docs")',
            "brain_ls": 'brain_ls(prefix="docs", tree=True)',
            "brain_task": 'brain_task(action="diff", path="docs/a.md", a=1, b=2)',
        }
        lines = ["# Инструменты MCP", ""]
        for item in _TOOL_DEFS:
            schema = json.dumps(item["inputSchema"], ensure_ascii=False)
            lines += [
                f"## {item['name']}",
                item["description"],
                f"Схема: {schema}",
                f"Пример: {examples.get(item['name'], '')}",
                "",
            ]
        lines += [
            "Остальные действия — через brain_task(action=...):",
            ", ".join(sorted(_MEMORY_ACTIONS | _TASK_ACTIONS)),
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Прямые инструменты
    # ------------------------------------------------------------------
    def read(self, path: str, **params: Any) -> dict[str, Any]:
        """Прочитать документ."""
        cursor = params.pop("cursor", None)
        data = self.service.read(self.token, path, **params)
        if cursor:
            data["cursor_hint"] = cursor
        return data

    def write(
        self,
        path: str,
        content: str,
        reason: str,
        **params: Any,
    ) -> dict[str, Any]:
        """Записать новую версию."""
        params.pop("cursor", None)
        return self.service.write(self.token, path, content, reason, **params)

    def search(self, query: str, **params: Any) -> dict[str, Any]:
        """Поиск по памяти."""
        params.pop("cursor", None)
        return self.service.search(self.token, query, **params)

    def ls(
        self,
        prefix: str = "",
        tree: bool = False,
        max_depth: int | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Список или дерево."""
        params.pop("cursor", None)
        return self.service.ls(
            self.token, prefix, tree=bool(tree), max_depth=max_depth
        )

    def task(self, action: str, **params: Any) -> Any:
        """Мультиплексор действий памяти, задач, inbox и токенов."""
        params.pop("cursor", None)
        key = _canonical(action)
        if key in _MEMORY_ACTIONS:
            method = getattr(self.service, key)
            return self._invoke(method, params)
        if key.startswith("task_") and len(key) > 5:
            key = key[5:]
        return self.service.task(self.token, key, **params)

    def _invoke(self, method: Callable[..., Any], params: dict[str, Any]) -> Any:
        """Вызвать метод Service, отбросив лишние kwargs."""
        signature = inspect.signature(method)
        if any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in signature.parameters.values()
        ):
            return method(self.token, **params)
        allowed = {
            name
            for name, item in signature.parameters.items()
            if name not in ("self", "token")
            and item.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        clean = {key: value for key, value in params.items() if key in allowed}
        return method(self.token, **clean)

    # ------------------------------------------------------------------
    # Вызов инструмента по имени и лимит ответа
    # ------------------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any] | None) -> tuple[str, bool]:
        """Вызвать инструмент; вернуть (текст, признак ошибки)."""
        args = dict(arguments or {})
        cursor = args.pop("cursor", None)
        try:
            if name == "brain_help":
                result: Any = self.help(args.get("topic", ""))
            elif name == "brain_read":
                result = self.read(**args)
            elif name == "brain_write":
                result = self.write(**args)
            elif name == "brain_search":
                result = self.search(**args)
            elif name == "brain_ls":
                result = self.ls(**args)
            elif name == "brain_task":
                action = args.pop("action", "")
                if not action:
                    raise ValidationError(
                        "Не указан action.",
                        hint="Список действий: brain_help().",
                    )
                result = self.task(action, **args)
            else:
                raise ValidationError(
                    f"Неизвестный инструмент «{name}».",
                    hint="Доступны: " + ", ".join(_TOOL_NAMES) + ".",
                )
            text = result if isinstance(result, str) else _json(result)
            return self._limit(text, cursor), False
        except ArtekBrainError as exc:
            return f"Ошибка: {exc}", True
        except (TypeError, ValueError) as exc:
            return (
                f"Ошибка вызова: {exc}. Проверьте аргументы через brain_help().",
                True,
            )

    def _limit(self, text: str, cursor: Any = None) -> str:
        """Ограничить ответ; при усечении вернуть chunk и cursor."""
        limit = self.max_response_chars
        try:
            start = max(0, int(cursor)) if cursor not in (None, "") else 0
        except (TypeError, ValueError):
            start = 0
        if start == 0 and len(text) <= limit:
            return text
        total = len(text)
        if start >= total:
            return _json(
                {"truncated": True, "cursor": None, "total_chars": total, "chunk": ""}
            )

        def build(size: int) -> str:
            chunk = text[start : start + size]
            end = start + len(chunk)
            return _json(
                {
                    "truncated": True,
                    "cursor": str(end) if end < total else None,
                    "total_chars": total,
                    "chunk": chunk,
                }
            )

        size = max(1, limit - 160)
        out = build(size)
        while len(out) > limit and size > 1:
            size = max(1, size - (len(out) - limit) - 1)
            out = build(size)
        return out


# ----------------------------------------------------------------------
# MCP-транспорт
# ----------------------------------------------------------------------
def run_stdio(
    service: Service,
    token: str | None,
    *,
    max_response_chars: int = DEFAULT_MAX_CHARS,
) -> None:
    """Запустить MCP stdio; при отсутствии пакета — тонкий JSON-RPC адаптер."""
    app = BrainMCP(service, token, max_response_chars=max_response_chars)
    try:
        from mcp import types
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server
    except ImportError:  # pragma: no cover - зависит от окружения
        _run_fallback(app)
        return

    async def on_list_tools(context: Any, params: Any) -> Any:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=item["name"],
                    description=item["description"],
                    inputSchema=item["inputSchema"],
                )
                for item in app.list_tools()
            ]
        )

    async def on_call_tool(context: Any, params: Any) -> Any:
        text, is_error = app.call(params.name, dict(params.arguments or {}))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            isError=is_error,
        )

    server = Server(
        "artek-brain",
        version="0.1.0",
        instructions=app.help(),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )

    async def main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(main())


def _run_fallback(app: BrainMCP) -> None:  # pragma: no cover - резервный путь
    """Минимальный JSON-RPC MCP stdio без пакета mcp."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}
        if request_id is None:
            continue
        if method == "initialize":
            result: Any = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "artek-brain", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": app.list_tools()}
        elif method == "tools/call":
            text, is_error = app.call(
                params.get("name", ""), params.get("arguments") or {}
            )
            result = {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            }
        elif method == "ping":
            result = {}
        else:
            error = {"code": -32601, "message": f"method not found: {method}"}
            print(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "error": error}),
                flush=True,
            )
            continue
        print(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "result": result},
                ensure_ascii=False,
            ),
            flush=True,
        )
