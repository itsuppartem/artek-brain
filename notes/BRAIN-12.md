# BRAIN-12: старение знаний, журнал сессий, поиск и хук (0.2.0)

Повод: аудит боевого экземпляра (касса, 13–21.11.2026). Docs не обновлялись после заливки,
агенты не дочитывали обрезанные документы, многословный поиск был пуст в 47% случаев,
в журнале не было сессий и поисков, после сжатия контекста мозг не перечитывали.

Сделано:
- aging.py: TTL по папкам (settings.json → aging), «docs отстали от notes», бриф «проверь перед опорой»;
  quality report считает свежесть по сроку, а не ложные 100% при пустых review_by.
- MCP: session/model/client в журнале (BRAIN_SESSION, BRAIN_MODEL, BRAIN_CLIENT, clientInfo);
  фильтр type; поиски в журнале; обрезка чтения по строкам с remaining_sections и next.line_start.
- Поиск: запасной режим match=any по основам слов, если строгий AND пуст.
- brain hook session-start + хук SessionStart (startup|resume|clear|compact) в brain setup;
  setup обновляет блок AGENTS.md между маркерами и ставит метки BRAIN_CLIENT.
- Хранилище: PRAGMA user_version (схема 1, новее — отказ), права 600, PASSIVE checkpoint после
  записи, journal_size_limit 1 МБ; строки удаляемых старых таблиц доски — в .brain/legacy/*.json.
- CLI не создаёт пустое хранилище молча. Противоречия не берут число из середины фразы.
- README: CI, схема, уроки эксплуатации, замеры (scripts/bench.py).

Проверка: uv run pytest -q (183 passed), tests/test_aging_audit.py.
Не сделано: пагинация по данным вместо JSON-курсора для остальных инструментов; курсор
«с последнего визита» на сервере.
