# BRAIN-3: интерфейсы CLI, HTTP и MCP

## Что сделано
- `service.py`: общий слой поверх фасада `Brain`, `Access` и `TaskBoard`; автор/агент/отправитель всегда берутся из токена. Ядро не менялось.
- `cli.py` + entry point `brain`: init, token create/revoke/list, ls/tree, read, write (файл/stdin, --base-version/--reason), diff, history, blame, rollback (version/author/batch), delete/restore, search, links/backlinks, changes, audit, stats, task *, inbox *, brief, export. Токен из `--token` или `BRAIN_TOKEN`, короткий текст или `--json`, ошибки с кодом != 0.
- `http_api.py`: stdlib `http.server`, Bearer-токен, JSON, те же операции, `/openapi.json`, лимит тела, `/rpc`; `brain serve --port`, по умолчанию только 127.0.0.1.
- `mcp_server.py`: 6 стартовых инструментов (`brain_help`, `brain_read`, `brain_write`, `brain_search`, `brain_ls`, `brain_task`), `brain_help(topic)` со схемами/примерами, `brain_task` — мультиплексор остальных действий. Лимит ответа 8000 символов с `cursor`; ошибки текстом «что не так / что передать».
- MCP-пакет `mcp>=2.3.0` добавлен в `[project.optional-dependencies] mcp` и установлен через uv. Если пакета нет, `run_stdio` использует тонкий JSON-RPC адаптер (initialize, tools/list, tools/call).
- Тесты: CLI через subprocess (write→conflict→rollback→search, права, env-токен, задачи/ревью/inbox), HTTP на случайном порту (401 без токена, 403 вне scope, 413, OpenAPI, RPC), MCP напрямую (каталог, help, лимит и склейка чанков по cursor, отказы).

## Как проверить
- `uv run pytest -q` — 103 теста зелёные (включая старые).
- `uv run brain init --root /tmp/brain-3`, затем `uv run brain --token <owner> token create --name a --role agent --scope docs:write`.

## Что не сделано
- Веб-UI, семантический поиск и песочница — вне границ BRAIN-3.
- HTTP-сервер однопроцессный с общим `Service`; межпроцессная координация не добавлялась.
