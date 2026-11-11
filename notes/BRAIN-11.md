# BRAIN-11: качество знаний и семантика в интерфейсах

## Что сделано
- `quality.py`: табличные противоречия только для пары «ключ строки + заголовок колонки»
  и одинаковых единиц; числа с пробелами («1 271 276») разбираются целиком; пример
  «Статус 200 vs Кол-во 100» больше не даёт ложного срабатывания.
- `report(details=True)` и CLI `brain quality report --details`: списки путей без
  frontmatter/owner/review_by/refs и с просроченным review_by (текст и JSON).
- CLI `brain quality links [--prefix] [--json]`: битые внутренние ссылки из nav.
- CLI `brain semantic status|enable|disable|reindex`: настройка в settings.json,
  только owner, аудит включения/выключения/reindex.
- HTTP `/search?mode=fts|semantic|hybrid`. Если семантика выключена, hybrid идёт
  как fts и возвращает `warning`; semantic остаётся ошибкой с подсказкой.
- MCP `brain_search(mode=hybrid)` при выключенной семантике тоже отдаёт fts+warning.

## Как проверить
- `uv run pytest -q` — 197 passed, 2 deselected (load).
- Ключевые тесты: `tests/test_quality.py` (колонки, единицы, тысячи, details),
  `tests/test_cli_quality.py` (links, details), `tests/test_semantic.py` (CLI, MCP),
  `tests/test_http_api.py` (mode=hybrid/semantic).

## Что не сделано
- Нет HTTP-эндпоинтов `/quality` и `/semantic/*` (только mode у поиска).
- Семантика остаётся опциональной: hash без сети, ollama требует локальный сервис.
