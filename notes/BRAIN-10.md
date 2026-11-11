# BRAIN-10: надёжность хранилища

## Что сделано
- `search.py`: отпечаток состояния хранилища хранится в `search.sqlite3` (`meta`), учтены версии и `documents.current_version/deleted`; повторное открытие не пишет индекс. Догон — по изменившимся документам и новым версиям (`indexed_docs`). Полный `rebuild()` — по явному вызову, пустому/старому/повреждённому индексу; повреждённый файл пересоздаётся.
- `backup.py` + `brain backup create/verify`: согласованный tar.gz баз через `Connection.backup`, блобы, `settings.json`, `tokens/`; манифест sha256 рядом, права 0600, ротация `--keep`; verify распаковывает, сверяет манифест и sha256 текущих версий.
- `maintenance.py` + `brain maintain`: `wal_checkpoint(TRUNCATE)` по всем `*.sqlite3` (busy — в отчёте, не ошибка), затем gc, repack, disk-report; JSON-итог и аудит.
- Всё только для owner, запись в аудит. Тесты: `tests/test_search_incremental.py`, `test_backup.py`, `test_maintain.py`, CLI-сценарий.

## Как проверить
- `uv run pytest -q` — 201 passed, 2 deselected.
- `brain backup create --output /tmp/bk --keep 3`, затем `brain backup verify /tmp/bk/brain-backup-*.tar.gz`.
- `brain maintain` на работающем хранилище; параллельное чтение не блокируется.

## Что не сделано
- Нет фонового расписания backup/maintain и удалённого хранилища копий.
- `verify` проверяет текущие версии и целостность файлов, но не перечитывает все старые версии.
