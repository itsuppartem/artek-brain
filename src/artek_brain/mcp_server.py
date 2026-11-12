"""MCP stdio сервер Artek Brain с progressive disclosure.

При старте клиент видит только шесть инструментов. Остальные операции
доступны через brain_op(action=...) и описаны в brain_help(topic).
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
            "(например read, history, rollback, inbox) — схема и пример."
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
        "description": (
            "Поиск с фильтрами и пагинацией: mode=fts (по умолчанию), "
            "semantic или hybrid (FTS + семантика через RRF)."
        ),
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
                "mode": {
                    "type": "string",
                    "enum": ["fts", "semantic", "hybrid"],
                    "description": "Режим поиска, по умолчанию fts.",
                },
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
        "name": "brain_op",
        "description": (
            "Мультиплексор: action=history, diff, rollback, audit, stats, "
            "brief, inbox, send и другие. "
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
        "author, since, until, limit, offset, history, mode=fts|semantic|hybrid.",
        "brain_search(query=\"деплой\", path_prefix=\"docs\", mode=\"hybrid\")",
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
    "op": (
        "Мультиплексор brain_op. Параметр action плюс поля нужного действия.",
        "brain_op(action=\"history\", path=\"docs/a.md\")",
        "Схемы отдельных action — в brain_help(\"<action>\").",
    ),
    "sandbox": (
        "Песочница скриптов: python/shell выполняется изолированно (bwrap), "
        "сеть выключена, данные — read-only срез по inputs.prefix/paths, "
        "результат из stdout/stderr и /out. Новый стартовый MCP-инструмент "
        "не добавлен: вызывайте через brain_op(action=\"sandbox_run\"). "
        "CLI: brain sandbox run --lang python --paths docs/ script.py. "
        "HTTP: POST /sandbox/run (script, language, inputs, limits, cache).",
        "brain_op(action=\"sandbox_run\", script=\"import brainlib; print(brainlib.documents())\", language=\"python\", inputs={\"prefix\": \"docs\"})",
        "Возвращает exit_code, stdout, stderr, out, duration_ms, peak_memory_bytes, slice.",
    ),
    "sandbox_run": (
        "Запустить скрипт в песочнице. Параметры: script (текст), language, "
        "inputs (prefix/paths), limits, cache. Нужна роль agent и выше.",
        "brain_op(action=\"sandbox_run\", script=\"print(1)\", language=\"python\")",
        "Возвращает stdout/stderr, out, exit_code, время и пиковую память.",
    ),
    "sandbox_status": (
        "Статус запуска песочницы по id.",
        "brain_op(action=\"sandbox_status\", job_id=\"...\")",
        "Возвращает status и результат, если запуск завершён.",
    ),
    "sandbox_cancel": (
        "Отменить запуск песочницы по id.",
        "brain_op(action=\"sandbox_cancel\", job_id=\"...\")",
        "Убивает группу процессов и возвращает cancelled.",
    ),
    "history": (
        "История версий. Параметры: path (обязателен), limit=50, offset=0.",
        "brain_op(action=\"history\", path=\"docs/a.md\", limit=10)",
        "Возвращает список версий: n, parent_n, sha256, author, ts, op, reason.",
    ),
    "diff": (
        "Сравнить версии. Параметры: path (обязателен), a, b.",
        "brain_op(action=\"diff\", path=\"docs/a.md\", a=1, b=2)",
        "Возвращает unified diff.",
    ),
    "blame": (
        "Авторы разделов. Параметр: path.",
        "brain_op(action=\"blame\", path=\"docs/a.md\")",
        "Возвращает title, level, version, author, ts по разделам.",
    ),
    "toc": (
        "Оглавление документа. Параметры: path, version.",
        "brain_op(action=\"toc\", path=\"docs/a.md\")",
        "Возвращает разделы с chars и lines.",
    ),
    "links": (
        "Исходящие ссылки. Параметры: path, version.",
        "brain_op(action=\"links\", path=\"docs/a.md\")",
        "Возвращает source, target, exists, external.",
    ),
    "backlinks": (
        "Обратные ссылки. Параметр: path.",
        "brain_op(action=\"backlinks\", path=\"docs/a.md\")",
        "Возвращает документы, которые ссылаются на path.",
    ),
    "broken_links": (
        "Битые ссылки. Параметр: prefix.",
        "brain_op(action=\"broken_links\", prefix=\"docs\")",
        "Возвращает ссылки с exists=false.",
    ),
    "changes": (
        "Лента изменений после курсора. Параметры: cursor, limit=100.",
        "brain_op(action=\"changes\", cursor=0, limit=50)",
        "Возвращает items, cursor, next_cursor, has_more.",
    ),
    "readme": (
        "Карта памяти из README и верхнего уровня. Параметров нет.",
        "brain_op(action=\"readme\")",
        "Возвращает readme, sections, links, top_level.",
    ),
    "delete": (
        "Мягко удалить документ. Параметры: path, reason (обязательны), "
        "base_version, batch_id.",
        "brain_op(action=\"delete\", path=\"docs/a.md\", reason=\"устарело\")",
        "Возвращает version и op=delete; история сохраняется.",
    ),
    "restore": (
        "Восстановить документ. Параметры: path, reason (обязательны), "
        "base_version, batch_id.",
        "brain_op(action=\"restore\", path=\"docs/a.md\", reason=\"вернуть\")",
        "Возвращает version и op=restore.",
    ),
    "rollback_version": (
        "Откат к версии. Параметры: path, to_version, reason.",
        "brain_op(action=\"rollback_version\", path=\"docs/a.md\", to_version=1, reason=\"ошибочная правка\")",
        "Создаёт новую версию со старым содержимым.",
    ),
    "rollback_author": (
        "Откат правок автора за период. Параметры: author, since, until, reason.",
        "brain_op(action=\"rollback_author\", author=\"agent-1\", since=\"2026-01-01\", reason=\"пачка\")",
        "Возвращает rolled_back и skipped с причинами.",
    ),
    "rollback_batch": (
        "Откат пачки. Параметры: batch_id, reason.",
        "brain_op(action=\"rollback_batch\", batch_id=\"b-1\", reason=\"ошибочная пачка\")",
        "Возвращает rolled_back и skipped.",
    ),
    "export": (
        "Экспорт Markdown. Параметры: path, version.",
        "brain_op(action=\"export\", path=\"docs/a.md\")",
        "Возвращает content.",
    ),
    "audit": (
        "Журнал аудита (owner/auditor). Параметры: limit, actor, action, since.",
        "brain_op(action=\"audit\", limit=20, action=\"write\")",
        "Возвращает записи ts, actor, action, path, detail.",
    ),
    "stats": (
        "Статистика (owner/auditor). Параметров нет.",
        "brain_op(action=\"stats\")",
        "Возвращает documents, versions, bytes, top_authors, errors.",
    ),
    "who_knows": (
        "Кто последним менял путь. Параметр: path.",
        "brain_op(action=\"who_knows\", path=\"docs/a.md\")",
        "Возвращает author, version, ts, reason.",
    ),
    "brief": (
        "Бриф агента. Параметр: limit=2000.",
        "brain_op(action=\"brief\")",
        "Возвращает короткий текст: где ориентир, кто работает, последние правки, inbox.",
    ),
    "presence": (
        "Отметить агента онлайн. Параметр: activity.",
        "brain_op(action=\"presence\", activity=\"пишу тесты\")",
        "Возвращает name, last_seen, activity.",
    ),
    "agents": (
        "Кто онлайн. Параметр: ttl_seconds=300.",
        "brain_op(action=\"agents\", ttl_seconds=600)",
        "Возвращает список присутствующих.",
    ),
    "inbox": (
        "Входящие. Параметр: unread=true/false.",
        "brain_op(action=\"inbox\", unread=True)",
        "Возвращает письма: id, sender, subject, body, read.",
    ),
    "send": (
        "Отправить письмо. Параметры: to, subject, body.",
        "brain_op(action=\"send\", to=\"human-1\", subject=\"Готово\", body=\"...\")",
        "Возвращает письмо; отправитель берётся из токена.",
    ),
    "mark_read": (
        "Отметить письма прочитанными. Параметр: message_id (число или список).",
        "brain_op(action=\"mark_read\", message_id=[1, 2])",
        "Возвращает changed — сколько писем изменено.",
    ),
    "token_create": (
        "Создать токен (owner). Параметры: name, role, scopes, ttl.",
        "brain_op(action=\"token_create\", name=\"agent-1\", role=\"agent\", scopes=[\"docs:write\"])",
        "Возвращает token один раз и метаданные.",
    ),
    "token_list": (
        "Список токенов (owner). Параметров нет.",
        "brain_op(action=\"token_list\")",
        "Возвращает токены без секретов.",
    ),
    "token_revoke": (
        "Отозвать токен (owner). Параметры: token_id, value или name.",
        "brain_op(action=\"token_revoke\", token_id=3)",
        "Возвращает число отозванных.",
    ),
    "quality": (
        "Качество знаний: report, drift, dups, contradictions, queue. "
        "В MCP это только справка; запускайте через CLI: "
        "brain quality report|drift|dups|contradictions|queue.",
        "brain quality report --json",
        "Метрики: documents, coverage, freshness_overdue, stale, review_queue_open.",
    ),
}

_ALIASES = {
    "get": "read",
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
    "send": "send",
    "mark_read": "mark_read",
    "mark-read": "mark_read",
}

_MEMORY_ACTIONS = {
    "read", "write", "search", "history", "diff", "blame", "toc", "links",
    "backlinks", "broken_links", "changes", "readme", "delete", "restore",
    "rollback_version", "rollback_author", "rollback_batch", "export",
    "audit", "stats", "who_knows", "brief", "token_create", "token_list",
    "token_revoke", "sandbox_run", "sandbox_status", "sandbox_cancel",
}

_COORD_ACTIONS = {"presence", "agents", "inbox", "send", "mark_read"}


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
                "- brain_op(action, ...) — все остальные операции.",
                "",
                "Остальное вызывается через brain_op(action=\"...\"):",
                ", ".join(sorted(_MEMORY_ACTIONS | _COORD_ACTIONS)),
                "",
                "Качество знаний: brain_help(topic=\"quality\") и CLI "
                "brain quality report|drift|dups|contradictions|queue.",
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
            "brain_op": 'brain_op(action="diff", path="docs/a.md", a=1, b=2)',
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
            "Остальные действия — через brain_op(action=...):",
            ", ".join(sorted(_MEMORY_ACTIONS | _COORD_ACTIONS)),
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

    def op(self, action: str, **params: Any) -> Any:
        """Мультиплексор действий памяти, писем, присутствия и токенов."""
        params.pop("cursor", None)
        key = _canonical(action)
        if key in _MEMORY_ACTIONS:
            method = getattr(self.service, key)
            return self._invoke(method, params)
        return self.service.coord_action(self.token, key, **params)

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
            elif name == "brain_op":
                action = args.pop("action", "")
                if not action:
                    raise ValidationError(
                        "Не указан action.",
                        hint="Список действий: brain_help().",
                    )
                result = self.op(action, **args)
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
