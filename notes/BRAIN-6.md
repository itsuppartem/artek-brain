# BRAIN-6: семантический поиск как опциональный слой

## Что сделано
- `embeddings.py`: интерфейс `Embedder`; `HashEmbedder` (hashing trick по словам и триграммам, L2-норма, без модели); `OllamaEmbedder` (`POST /api/embed`, urllib, адрес/модель из настроек, `EmbeddingError` с подсказкой).
- `semantic.py`: SQLite-индекс чанков по разделам Markdown (окно 1500 символов), векторы BLOB float32, косинус на `struct`/`math`; хранит модель и версию документа; при >50k чанков — предфильтр по FTS-кандидатам.
- Включение через `settings.json` (`configure_semantic`); по умолчанию выключено. Смена модели/размерности → `stale`, поиск требует `reindex()`.
- `hybrid_search`: RRF (k=60) для FTS/BM25 и семантики, те же фильтры; права применяются до ранжирования, чужое не влияет на счёт.
- Фасад `Brain`: `search(..., mode=fts|semantic|hybrid)`, хуки после write/delete/restore/rollback рядом с FTS, `semantic_search`, `hybrid_search`, `reindex_semantic`.
- CLI: `brain search --semantic` (гибрид) и `--mode`; MCP: параметр `mode` у `brain_search` (по умолчанию fts). `service.py` проксирует режим, история — только fts.

## Как проверить
- `uv run pytest -q` — 113 тестов зелёные (103 старых + 10 новых).
- `tests/test_semantic.py`: HashEmbedder, ранжирование, права в semantic/hybrid, актуальность после rollback/delete/restore, stale при смене модели, stdlib-фейк Ollama, CLI и MCP.
- Пример: включить `{"semantic":{"enabled":true,"embedder":"hash","dim":256}}` в `<root>/settings.json`, затем `brain search "запрос" --semantic`.

## Что не сделано
- Нет HTTP-параметра mode и отдельного CLI для настройки/reindex (доступно через API `configure_semantic`/`SemanticIndex.reindex`).
- Индекс строится при включении только для новых/изменённых документов; существующее хранилище нужно один раз переиндексировать.
- Нет кэша норм векторов и ANN-структур: косинус полным перебором (с FTS-предфильтром для больших индексов).
