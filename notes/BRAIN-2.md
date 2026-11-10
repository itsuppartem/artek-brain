# BRAIN-2: права, поиск и навигация

## Что сделано
- `access.py`: токены (owner/human/agent/auditor), scopes `(префикс, read|write|admin)`, TTL, отзыв; в SQLite только sha256, токен виден один раз.
- Политика путей: `owner/`, `rules/`, `README(.md)`, `approved/` меняет только owner; `owner/inbox/<файл>` любой с write может один раз создать; auditor — только аудит/статистика.
- `brain.py`: фасад `Brain(store, access)` с токеном первым аргументом; автор берётся из токена; отказы `PermissionDenied` пишутся в аудит. Агент не откатывает чужие правки и не трогает approved/.
- `search.py`: FTS5/BM25 по текущим версиям и истории; фильтры пути/type/status/tags/author/дат, сниппеты, лимит+пагинация; индекс обновляется фасадом и сам догоняет Store-записи.
- `nav.py`: `ls`/`tree` с размерами и числом версий, `toc` с символами, `links`/`backlinks`/`broken_links`, `changes_since(cursor)`, `readme_map`.
- `__init__.py` экспортирует новые классы и `PermissionDenied`.

## Как проверить
- `uv run pytest -q` — 73 теста (42 старых + 31 новый), зелёные.
- Новые тесты: `tests/test_access.py`, `test_brain_permissions.py`, `test_search.py`, `test_nav.py`.

## Что не сделано (по границам задачи)
- Нет MCP/CLI/HTTP (BRAIN-3) и доски задач (BRAIN-4).
- Нет семантического поиска, веб-UI и квот на поиск.
