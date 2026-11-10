# BRAIN-1: ядро хранилища

## Что сделано
- `store.py`: SQLite (WAL, foreign_keys), блобы `blobs/aa/bbbb...` по sha256, zlib, дедупликация.
- Версии документов, аудит всех действий (read тоже), soft delete/restore, история, diff, blame.
- `write` с base_version/expected_sha256, ConflictError (current_version, unified diff, разделы).
- Трёхсторонняя склейка Markdown-разделов (заголовки `#..`), op=`merge`; один раздел — конфликт.
- Откаты: версии, правок автора за период, пачки (`rollback_author`, `rollback_batch`) с пропусками.
- `read` по версии/разделу/диапазону строк/бюджету с `truncated`; `export_markdown`, `import_file`.
- `frontmatter.py`: парсер/запись `key: value`, списков `[a, b]`, понятные ошибки.
- `secrets.py`: AWS/sk-/ghp_/xox/PEM/password/JWT; секрет не показывается, audit.detail чистится.
- `quota.py`: размер (1 МиБ), версии, суточные записи; `QuotaError`; `store.gc()` блобов.
- `__init__.py`: публичное API и исключения.

## Как проверить
- `uv run pytest -q` — 42 теста зелёные.
- Нагрузка: 2000 версий за ~0.47 c; диск 5 571 968 байт (блобы 54 888, БД ~5.5 МБ).

## Что не сделано (по границам задачи)
- Нет MCP/CLI/HTTP, прав доступа, FTS/поиска, веб-UI: это другие задачи.
