# BRAIN-7: качество знаний

## Что сделано
- `knowledge.py`: снимок документа, права токена, n-граммы, даты.
- `drift.py`: refs `path@sha256`, `path#symbol@git-sha`; `check_drift`, `mark_stale` (system,
  status=stale, очередь), `pin`.
- `review_queue.py`: очередь при drift, status=review, просроченном `review_by`;
  approve/reject/defer только owner/human; видна причина и `path@version`.
- `quality.py`: дубли (Jaccard 5-грамм, порог), противоречия по `facts` и числам в
  таблицах/`ключ: значение`, битые ссылки через nav, схемы `rules/schemas.md|json`,
  `validate`, `strict_check` для `approved/`, метрики `report`.
- CLI: `brain quality report|drift|dups|contradictions|queue`; MCP: `brain_help("quality")`.
- Service проверяет схему при записи в `approved/`, если `rules/schemas.*` есть.

## Как проверить
- `uv run pytest -q` — 123 теста зелёные (старые + новые).
- `brain quality report --json`; `brain quality drift --repo . --mark-stale`; `queue list`.

## Что не сделано
- Нет HTTP-эндпоинта `/quality`; нет LLM/семантики; очередь синхронизируется явно.
