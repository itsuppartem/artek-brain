# artek-brain
Общая память для людей и агентов, отдельный экземпляр на каждый проект. Спецификация: docs/TZ.md. Правила агентов: AGENTS.md.

## Подключить к проекту
```bash
uv tool install --from git+ssh://git@github.com/itsuppartem/artek-brain "artek-brain[mcp]"
cd /путь/к/проекту && brain setup --name "Название"
```
`brain setup` создаёт `.brain/` (база и токены `owner.token`, `agent.token`, права 600, в git не попадает),
`.mcp.json` для Claude Code, `opencode.json` для агентов opencode и блок правил в `AGENTS.md`.
MCP ищет `.brain` вверх по каталогам, поэтому git-копии агентов внутри проекта пишут в ту же память.

Тесты: `uv run pytest -q`. Тесты песочницы запускают systemd-run (окно пароля), по умолчанию пропущены: `BRAIN_SANDBOX_TESTS=1 uv run pytest -q`.
