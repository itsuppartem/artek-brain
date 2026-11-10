# BRAIN-4: доска задач и координация роя

## Что сделано
- `src/artek_brain/tasks.py`: `TaskBoard` поверх того же SQLite (`brain.sqlite3`), схема `tasks`, `task_messages`, `task_agents`, `task_roles`, `task_handoffs`.
- Задачи: id `AREA-NNN`, title, body, status, priority, depends_on, area, author, assignee, revision; `create/list/show`.
- Аренда: `claim(minutes, revision)` выдаёт `claim_token`, `heartbeat(token)`, `release` (по агенту или токену); квота `max_claims=2`.
- Оптимистичная блокировка: у `claim/start/release/submit/accept/rework/block/cancel` обязателен `revision`; `TaskRevisionConflictError` отдаёт текущую задачу.
- Защита пересечений областей по префиксу пути; отказ называет занятую задачу и агента. Просроченная аренда снимается лениво при любом вызове.
- Зависимости: claim только после `done`; цикл (включая forward-ref) запрещён при create.
- Ревью: `submit` требует непустой `evidence`; `accept/rework` только reviewer/owner/admin и не автор задачи.
- Presence: `presence/agent_heartbeat`, `agents_online(ttl)`; `who_knows(path)` читает `versions` (точный путь или вложенные).
- Inbox: `send/inbox/mark_read`; `handoff` пишет Markdown в Store `handoffs/<task>.md` с версиями.
- `brief(agent)` детерминированный, до 2000 символов: открытые задачи, занятые области, последние handoff, непрочитанный inbox.
- Экспорт новых классов и ошибок в `artek_brain.__init__`.

## Как проверить
- `uv run pytest -q` — 61 тест зелёный (старые 42 + 19 новых).
- `tests/test_tasks.py`: конкурентный claim из двух потоков, просрочка аренды, пересечение областей, зависимости, цикл, самоприёмка, revision-конфликт, лимит brief, inbox/handoff/presence.

## Что не сделано
- Нет HTTP/MCP/CLI и отдельного веб-UI: это слои интерфейса, не ядро.
- Нет межпроцессной блокировки claim: расчёт рассчитан на один экземпляр Store/TaskBoard (как в ТЗ).
- Нет истории ревизий самой задачи и мягкого удаления задач.
