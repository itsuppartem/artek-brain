# Usage

How to install artek-brain, connect it to agents, and use it from MCP, the CLI and HTTP. All outputs below come from a real run against a temporary store (version 0.2.0), shortened where marked with `…`; MCP calls were sent as raw JSON-RPC over stdio. Hashes and timestamps will differ on your machine.

Error messages, hints and the text of the brief are in Russian; field names and JSON structure are in English.

- [Install and set up a project](#install-and-set-up-a-project)
- [MCP tools](#mcp-tools)
- [`brain_op` actions](#brain_op-actions)
- [CLI](#cli)
- [HTTP API](#http-api)
- [SessionStart hook](#sessionstart-hook)
- [Agent workflow](#agent-workflow)

## Install and set up a project

```bash
uv tool install --from git+https://github.com/itsuppartem/artek-brain "artek-brain[mcp]"
cd /path/to/project
brain setup --name "shop"
```

The `[mcp]` extra installs the official `mcp` package. Without it, `brain mcp` still works through a minimal built-in JSON-RPC adapter (`initialize`, `tools/list`, `tools/call`, `ping`).

`brain setup [PROJECT] [--name NAME]` (`project.py`) is idempotent. On the first run it creates:

| Item | What it is |
|---|---|
| `.brain/` | The store: `brain.sqlite3`, `access.sqlite3`, `search.sqlite3`, `registry.sqlite3`, `blobs/`. |
| `.brain/owner.token` | Owner token, mode 600. Created only if the store has no owner token yet. |
| `.brain/agent.token` | Agent token with scope `:write` (whole store, except protected paths), mode 600. |
| `README.md`, `backlog.md` in memory | Starter documents written by `owner` (a memory map and an empty backlog). |
| `.gitignore` | `.brain/` line added if missing. |
| `.mcp.json` | MCP server `brain` for Claude Code, with `env.BRAIN_CLIENT = "claude-code"`. |
| `opencode.json` | MCP server `brain` for opencode, with `environment.BRAIN_CLIENT = "opencode"`. |
| `AGENTS.md` | A rules block between `<!-- BEGIN:artek-brain -->` and `<!-- END:artek-brain -->`. |
| `CLAUDE.md` | `@AGENTS.md` import, so Claude Code reads the same rules. |
| `.claude/settings.json` | `SessionStart` hook, see [below](#sessionstart-hook). Skipped if your user-level settings already contain it. |

The MCP command in `.mcp.json` and `opencode.json` is the absolute path of `brain` found on `PATH`, or `python -m artek_brain` if it is not there.

```console
$ brain setup . --name shop
{
  "project": "/path/to/project",
  "root": "/path/to/project/.brain",
  "created": [".brain/owner.token", ".brain/agent.token", "память:README.md",
              "память:backlog.md", ".gitignore", ".mcp.json", "opencode.json",
              ".claude/settings.json", "AGENTS.md"],
  "owner_token_file": "/path/to/project/.brain/owner.token",
  "agent_token_file": "/path/to/project/.brain/agent.token"
}
$ brain setup . --name shop      # second run
{ …, "created": [], … }
```

A second run never overwrites data or tokens. It only replaces the rules block in `AGENTS.md` (text outside the markers is kept), adds missing `BRAIN_CLIENT` labels and adds the hook if it is missing.

**Upgrading.** `uv tool upgrade artek-brain`, then `brain setup` in each project (to refresh the `AGENTS.md` block), then restart agent sessions: the MCP server process starts with the session and keeps the old code until then.

**Without a project.** `brain --root DIR init` creates a bare store and prints an owner token once. All other commands refuse to run when no store is found; an empty store is never created silently:

```console
$ brain --root /tmp/nowhere ls
Ошибка: Хранилище памяти не найдено: /tmp/nowhere. Что сделать: Разверните мозг в проекте (brain setup <каталог>) или brain init, либо укажите BRAIN_HOME / --root. …
```

**Which store is used.** `--root`, else `BRAIN_HOME`, else the nearest `.brain` directory in the current directory or any parent, else `./.brain` (`service.resolve_root`). Agents running in a git worktree or subfolder inside the project therefore share the project's memory.

**Which token is used.** CLI and MCP: `--token`, else `BRAIN_TOKEN`, else `.brain/agent.token`. So by default everything runs as the agent; pass the owner token explicitly for owner commands:

```bash
export BRAIN_TOKEN="$(cat .brain/owner.token)"   # owner for this shell only
```

## MCP tools

`brain mcp` exposes six tools. Everything else goes through `brain_op` and is documented by `brain_help`, so the tool list in the agent's context stays small. With the `mcp` package installed, the server also sends the `brain_help()` overview as its MCP `instructions`.

Every tool returns one text item: JSON for data, plain text for help. Errors are returned with `isError: true` and a text `Ошибка: <what is wrong> Что сделать: <what to pass>`.

### `brain_help`

| Parameter | Type | Description |
|---|---|---|
| `topic` | string | A tool or action name (`history`, `read`, `sandbox`, …), `tools` for all tool schemas, or empty for the overview. |

```text
brain_help(topic="history")
→ # history

  История версий. Параметры: path (обязателен), limit=50, offset=0.

  Пример:
  brain_op(action="history", path="docs/a.md", limit=10)

  Результат: Возвращает список версий: n, parent_n, sha256, author, ts, op, reason.
```

### `brain_read`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes | Logical path, e.g. `docs/deploy.md`. |
| `version` | integer | | Version number; current by default. |
| `section` | string | | Heading text without `#`; returns the section and its subsections. `preamble` = text before the first heading. |
| `line_start` | integer | | First line, 1-based. |
| `line_end` | integer | | Last line, inclusive. |
| `max_chars` | integer | | Cut the content to this many characters (`truncated: true`). |
| `cursor` | string | | Continue a response cut by the server limit. |

```text
brain_read(path="docs/deploy.md", section="Steps")
→ {"path": "docs/deploy.md", "version": 4,
   "content": "## Steps\n- Build the image.\n- Upload it to the staging host.\n- Run smoke tests.\n\n",
   "truncated": false, "total_chars": 81, "total_lines": 5,
   "section": "Steps", "line_range": null}
```

A document longer than the server limit is cut at a line boundary, and the response says what was not shown (server started with `--max-chars 1500` here):

```text
brain_read(path="docs/runbook.md")
→ {"path": "docs/runbook.md", "version": 1,
   "content": "# Runbook\n\n## Part 1\n- step 1.0: …\n- step 1.1: …\n- step 1.2: …\n",
   "truncated": true, "total_chars": 5491, "total_lines": 114, …,
   "shown_lines": "1-6",
   "next": {"path": "docs/runbook.md", "line_start": 7},
   "note": "Документ показан не целиком. …",
   "remaining_sections": [{"line": 17, "title": "Part 2"}, {"line": 31, "title": "Part 3"}, …,
                          {"line": 101, "title": "Part 8"}]}
```

Continue with `brain_read(path="docs/runbook.md", line_start=7)` or `brain_read(path="docs/runbook.md", section="Part 5")`.

### `brain_write`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `path` | string | yes | Logical path. |
| `content` | string | yes | Full new content (Markdown, optional frontmatter). |
| `reason` | string | yes | Why the document changes (1–2000 chars), stored in history and audit. |
| `base_version` | integer | for existing documents | The version you read. Must be omitted for a new document. |
| `expected_sha256` | string | | Stricter check: refuse if the current content hash differs. |
| `batch_id` | string | | Label to roll back a group of writes together. |
| `cursor` | string | | Response cursor, as for every tool (see `brain_op`). |

```text
brain_write(path="docs/deploy.md", content="# Deploy\n\n## Steps\n…- Tag the release.\n…",
            reason="add tagging step", base_version=4)
→ {"path": "docs/deploy.md", "version": 5, "parent_version": 4,
   "sha256": "4e726270…", "size": 154, "op": "write",
   "created": false, "merged": false, "changed": true, "batch_id": null}

brain_write(path="docs/deploy.md", content="# Deploy\n\n## Steps\n- Build only.\n",
            reason="stale edit", base_version=4)
→ isError: true
  Ошибка: Конфликт в документе «docs/deploy.md»: текущая версия 5, а переданная запись
  основана на другой версии. Пересекаются разделы: Steps. Что сделать: Прочитайте документ
  заново через read(path) и передайте base_version=5 (и expected_sha256 текущей версии).
```

`op` is `create`, `write`, `merge` (stale `base_version`, edits in different sections, merged automatically) or `noop` (content unchanged, no new version).

### `brain_search`

| Parameter | Type | Description |
|---|---|---|
| `query` | string, required | Words; all must match. If nothing matches, the server retries with any word by stem. |
| `path_prefix` | string | Only under this folder, e.g. `docs`. |
| `type` | string | Frontmatter `type`. |
| `status` | string | Frontmatter `status`. |
| `tags` | array of strings | All listed tags must be present. |
| `author` | string | Author of the current version. |
| `since`, `until` | string | ISO 8601 time of the current version. |
| `limit` | integer | 1–1000, default 20. |
| `offset` | integer | Default 0. |
| `history` | boolean | Search all versions instead of current ones (FTS only). |
| `mode` | `fts` \| `semantic` \| `hybrid` | Default `fts`. `hybrid` falls back to `fts` with a `warning` if semantic search is off. |
| `cursor` | string | Continue a response cut by the server limit. |

```text
brain_search(query="smoke", path_prefix="docs", limit=5)
→ {"items": [{"path": "docs/deploy.md", "version": 4, "title": "Deploy",
              "snippet": "# Deploy\n\n## Steps\n- Build the image.\n…\n- Run [smoke] tests.\n…",
              "type": "", "status": "", "tags": [], "author": "agent",
              "ts": "2026-11-22T09:01:17.551777+00:00", "score": -0.91}],
   "total": 1, "limit": 5, "offset": 0, "has_more": false,
   "query": "smoke", "match": "all"}
```

When no document contains all the words, the response comes from the fallback and says so:

```text
brain_search(query="smoke deployment checklist")
→ {"items": [{"path": "docs/deploy.md", …}], "total": 1, …,
   "match": "any",
   "note": "Точного совпадения по всем словам нет; показаны документы с любым из слов (по основе)."}
```

### `brain_ls`

| Parameter | Type | Description |
|---|---|---|
| `prefix` | string | Folder, empty for the root. |
| `tree` | boolean | Recursive listing. |
| `max_depth` | integer | Depth limit for `tree`. |
| `cursor` | string | Response cursor, as for every tool (see `brain_op`). |

```text
brain_ls(prefix="docs")
→ {"prefix": "docs", "tree": false,
   "nodes": [{"path": "docs/deploy.md", "kind": "file", "size": 135, "versions": 4,
              "version": 4, "author": "agent", "ts": "2026-11-22T09:01:17.551777+00:00",
              "depth": 0}]}
```

### `brain_op`

| Parameter | Type | Description |
|---|---|---|
| `action` | string, required | Action name, see the next section. |
| `cursor` | string | Continue a response cut by the server limit. |
| any other | | Parameters of the action (`additionalProperties: true`). |

```text
brain_op(action="history", path="docs/deploy.md", limit=2)
→ [{"path": "docs/deploy.md", "n": 5, "parent_n": 4, "sha256": "4e726270…", "size": 154,
    "author": "agent", "reason": "add tagging step", "ts": "…", "batch_id": null, "op": "write"},
   {"path": "docs/deploy.md", "n": 4, "parent_n": 3, …, "reason": "revert team note", "op": "rollback"}]

brain_op(action="audit")            # with an agent token
→ isError: true
  Ошибка: Отказ: для действия «audit» нужно право «audit» у «agent». Причина: журнал аудита
  и статистика доступны только owner и auditor. …
```

**Response limit.** Responses longer than `--max-chars` (default 8000) that are not reads come back as `{"truncated": true, "cursor": "<offset>", "total_chars": N, "chunk": "<part of the JSON text>"}`. Call the same tool with the same arguments plus `cursor` to get the next part, and join the chunks before parsing.

## `brain_op` actions

The list comes from `_MEMORY_ACTIONS` and `_COORD_ACTIONS` in `mcp_server.py`; descriptions from `_ACTION_HELP`. `brain_help(topic="<action>")` prints the schema and an example for each one.

**Documents and history**

| Action | Parameters | Returns |
|---|---|---|
| `read` | same as `brain_read` | same as `brain_read` (without line-based fitting) |
| `write` | same as `brain_write` | same as `brain_write` |
| `history` | `path`, `limit=50`, `offset=0` | versions: `n`, `parent_n`, `sha256`, `author`, `ts`, `op`, `reason` |
| `diff` | `path`, `a`, `b` | `{path, diff, chars}`, unified diff |
| `blame` | `path` | per section: `title`, `level`, `version`, `author`, `ts` |
| `export` | `path`, `version` | `{path, version, content}` |
| `delete` | `path`, `reason`, `base_version`, `batch_id` | soft delete, new version with `op: delete` |
| `restore` | `path`, `reason`, `base_version`, `batch_id` | new version with `op: restore` |
| `rollback_version` | `path`, `to_version`, `reason` | new version with the old content |
| `rollback_author` | `author`, `since`, `until`, `reason` | `rolled_back` and `skipped` with reasons |
| `rollback_batch` | `batch_id`, `reason` | `rolled_back` and `skipped` |

**Navigation and search**

| Action | Parameters | Returns |
|---|---|---|
| `search` | same as `brain_search` | same as `brain_search` |
| `toc` | `path`, `version` | sections with `chars` and `lines` |
| `links` | `path`, `version` | outgoing links: `source`, `target`, `exists`, `external` |
| `backlinks` | `path` | documents linking to `path` |
| `broken_links` | `prefix` | links with `exists: false` |
| `changes` | `limit=100` (see note) | change feed after an audit id: `items`, `next_cursor`, `has_more` |
| `readme` | — | memory map: `readme`, `sections`, `links`, `top_level` |
| `who_knows` | `path` | last change: `author`, `version`, `ts`, `reason` |

**Coordination**

| Action | Parameters | Returns |
|---|---|---|
| `brief` | `limit=2000` | `{agent, brief}`, the start-of-work digest |
| `presence` | `activity` | marks the caller online |
| `agents` | `ttl_seconds=300` | agents seen recently |
| `inbox` | `unread` | caller's messages: `id`, `sender`, `subject`, `body`, `read` |
| `send` | `to`, `subject`, `body` | the message; sender is the token's name |
| `mark_read` | `message_id` (number or list) | `{changed}` |

**Owner and auditor**

| Action | Parameters | Returns |
|---|---|---|
| `audit` | `limit`, `actor`, `action`, `since` | audit entries (owner, auditor) |
| `stats` | — | `documents`, `versions`, `blob_bytes`, `top_authors`, `errors`, `reads` (owner, auditor) |
| `token_create` | `name`, `role`, `scopes`, `ttl` | token (shown once) and metadata (owner) |
| `token_list` | — | tokens without secrets (owner) |
| `token_revoke` | `token_id`, `value` or `name` | `{revoked}` (owner) |

**Sandbox**

| Action | Parameters | Returns |
|---|---|---|
| `sandbox_run` | `script`, `language`, `inputs` (`prefix`/`paths`), `limits`, `cache` | `exit_code`, `stdout`, `stderr`, `out`, `duration_ms`, `peak_memory_bytes`, `slice` |
| `sandbox_status` | `job_id` | status and result |
| `sandbox_cancel` | `job_id` | kills the process group |

Aliases accepted by `brain_op`: `get` → `read`, `rollback` / `rollback-to` → `rollback_version`, `rollback_by_author`, `rollback_by_batch`, `who-knows`, `inbox_list`, `inbox_send`, `inbox_read` / `mark-read`. A `brain_` prefix on the action is ignored.

Note on `changes`: `cursor` is the feed position. If the MCP response itself is cut, continue it with `response_cursor`.

Upkeep actions:

| Action | Parameters | Returns |
|---|---|---|
| `upkeep` | `path`, `status=open\|resolved\|confirmed\|all`, `limit` | persistent tasks for stale documents, changed decisions and rule conflicts |
| `confirm_fresh` | `path`, `reason` (10+ chars), optional `task_id` | closes the document's tasks; without `task_id` also restarts its freshness clock |
| `propose` | `target`, `content`, `reason` | submits a decision change for owner approval and notifies the owner |
| `authorship` | none (owner, auditor) | counts of agent, on-request and owner-token versions |

A `brain_read` of a stale document or one with open tasks includes:

```json
{"freshness": {"status": "stale", "warning": "Документ устарел: …", "age_days": 40.0, "ttl_days": 30, "tasks": [{"id": 7, "kind": "stale", "detail": "…"}], "fix": "…"}}
```

`brain_search` marks such hits with `stale: true` and `stale_reason`, and lists them in `stale_paths`. The plain CLI prints these warnings to stderr, so stdout stays the document.

Quality checks are not MCP actions; `brain_help(topic="quality")` points to the CLI (`brain quality …`).

## CLI

`brain --help` lists all commands. Global options, accepted before or after the command: `--root DIR`, `--token TOKEN`, `--json` (machine-readable output; errors become `{"ok": false, "error", "message", "hint"}` and the exit code is non-zero).

Examples run from a subfolder of the project (the store is found upwards), with the agent token unless noted.

**Write and read**

```console
$ brain write docs/deploy.md --file deploy.md --reason "describe deploy"
{ "path": "docs/deploy.md", "version": 1, "parent_version": null, "sha256": "d2533413…",
  "size": 116, "op": "create", "created": true, "merged": false, "changed": true, "batch_id": null }

$ brain read docs/deploy.md --section Rollback
## Rollback
- Restore the previous release.

$ brain --json read docs/deploy.md --lines 3:6
{ "path": "docs/deploy.md", "version": 4,
  "content": "## Steps\n- Build the image.\n- Upload it to the staging host.\n- Run smoke tests.\n",
  "truncated": false, "total_chars": 80, "total_lines": 4, "section": null, "line_range": [3, 6] }
```

`write` takes `--file PATH`, `--file -` (stdin), `--content TEXT` or stdin; plus `--reason` (required), `--base-version`, `--expected-sha256`, `--batch`. `read` takes `--version`, `--section`, `--lines START:END`, `--max-chars`, `--output FILE`.

**Concurrent edits.** Three edits of version 1: A changes `Steps`, B changes `Rollback`, C changes `Steps` again.

```console
$ brain write docs/deploy.md --file a.md --reason "add smoke tests" --base-version 1
{ …, "version": 2, "op": "write", "merged": false, … }

$ brain write docs/deploy.md --file b.md --reason "notify team" --base-version 1
{ …, "version": 3, "parent_version": 2, "op": "merge", "merged": true, … }

$ brain write docs/deploy.md --file c.md --reason "no cache" --base-version 1
Ошибка: Конфликт в документе «docs/deploy.md»: текущая версия 3, а переданная запись основана
на другой версии. Пересекаются разделы: Steps. Что сделать: Прочитайте документ заново через
read(path) и передайте base_version=3 (и expected_sha256 текущей версии).
$ echo $?
1

$ brain write docs/deploy.md --file c.md --reason "x"
Ошибка: Для существующего документа «docs/deploy.md» (версия 3) обязателен base_version. …
```

**History, diff, rollback**

```console
$ brain history docs/deploy.md
v3  2026-11-22T09:00:40.334893+00:00  agent  merge  notify team
v2  2026-11-22T09:00:39.894055+00:00  agent  write  add smoke tests
v1  2026-11-22T09:00:28.778550+00:00  agent  create  describe deploy

$ brain diff docs/deploy.md --from 2 --to 3
--- docs/deploy.md@2
+++ docs/deploy.md@3
@@ -7,3 +7,4 @@

 ## Rollback
 - Restore the previous release.
+- Tell the team in the channel.

$ brain rollback docs/deploy.md --to 2 --reason "revert team note"
{ …, "version": 4, "parent_version": 3, "op": "rollback", … }
```

`brain rollback` also takes `--author NAME [--since] [--until]` or `--batch ID` instead of `PATH --to N`. `brain delete PATH --reason …` and `brain restore PATH --reason …` are soft delete and restore.

**Search**

```console
$ brain search smoke
Найдено: 1
- docs/deploy.md v3 (agent) Deploy
  # Deploy
  …
  - Run [smoke] tests.
  …
```

Options: `--prefix`, `--type`, `--status`, `--tag` (repeatable), `--author`, `--since`, `--until`, `--limit`, `--offset`, `--history`, `--mode fts|semantic|hybrid`, `--semantic` (same as `--mode hybrid`).

**Protected paths and secrets**

```console
$ printf '# hi\n' | brain write README.md --reason x --base-version 1
Ошибка: Отказ: для действия «write» на путь «README.md» нужно право «owner» у «agent».
Причина: путь защищён: писать может только owner. …

$ printf '# Proposal\n\nAdd a rollback drill.\n' | brain write owner/inbox/rollback-drill.md --reason "propose drill"
{ "path": "owner/inbox/rollback-drill.md", "version": 1, "op": "create", … }

$ printf '# Keys\n\napi_key = abcd1234efgh\n' | brain write notes/keys.md --reason "store key"
Ошибка: Найден секрет (тип: password, строка 3) в документе «notes/keys.md». Запись отклонена. …
```

**Coordination**

```console
$ brain coord presence --activity "writing deploy docs"
{ "name": "agent", "last_seen": "…", "activity": "writing deploy docs", "online": true }

$ brain inbox send --to owner --subject "Deploy doc ready" --body "See docs/deploy.md"
{ "id": 1, "sender": "agent", "recipient": "owner", "subject": "Deploy doc ready", … }

$ brain --token "$(cat .brain/owner.token)" inbox --unread
*#1  2026-11-22T09:01:19.205036+00:00  от agent: Deploy doc ready

$ brain brief
# Бриф для agent
Ориентир: README.md, backlog.md (доски задач нет).
Сейчас работают: 1
- agent: writing deploy docs
Последние правки памяти:
- docs/deploy.md v4 — agent: revert team note
- owner/inbox/rollback-drill.md v1 — agent: propose drill
…
Inbox: 0 непрочитанных
```

`brain coord` actions: `presence`, `agents` (`--ttl`), `who-knows` (`--path`). `brain inbox` actions: `list` (default, `--unread`), `send`, `read` (`--message-id`, repeatable).

**Tokens (owner)**

```console
$ export BRAIN_TOKEN="$(cat .brain/owner.token)"
$ brain --json token create --name docs-bot --role agent --scope docs:write --ttl 86400
{ "token": "<shown once>", "id": 3, "name": "docs-bot", "role": "agent",
  "scopes": [{"prefix": "docs", "level": "write"}],
  "created_at": "2026-11-22T09:01:39.041652+00:00",
  "expires_at": "2026-11-23T09:01:39.041652+00:00", "revoked_at": null, "suspended_at": null }

$ brain token list
#3  docs-bot  agent  active  docs:write
#2  agent  agent  active  :write
#1  owner  owner  active

$ brain --json token revoke --name docs-bot
{ "revoked": 1, "by": "name", "name": "docs-bot" }
```

`token create`: `--name`, `--role owner|human|agent|auditor`, `--scope PREFIX:LEVEL` (repeatable), `--ttl SECONDS`. `token revoke`, `token suspend` and `token resume` take `--id`, `--value` or `--name` (revoking by name revokes every token with that name).

**Audit and stats (owner, auditor)**

```console
$ brain audit --limit 3 --action search
2026-11-22T09:01:06.621694+00:00  agent  search
…
$ brain --json stats
{ "documents": 4, "documents_deleted": 0, "versions": 7, "blobs": 6, "blob_bytes": 943,
  "db_bytes": 438366, "disk_bytes": 439309,
  "top_authors": [{"author": "agent", "versions": 5}, {"author": "owner", "versions": 2}],
  "errors": 9, "reads": 8 }
```

**Quality**

```console
$ brain quality report
документов: 4
покрытие refs: 0 (0.00%)
просрочено review_by: 0 (0.00%)
просрочено по сроку свежести (без review_by): 0
свежих: 100% (по ttl)
отстали от заметок: 0
stale: 0
открытая очередь ревью: 0
```

`brain quality report [--prefix] [--details]`, `links`, `drift --repo DIR [--mark-stale]`, `dups [--threshold] [--limit]`, `contradictions [--limit]`, `queue list|sync|approve|reject|defer [--id] [--note] [--until]`.

**Semantic search (owner to configure)**

```console
$ brain --json semantic enable --embedder hash
{ "enabled": true, "embedder": "hash", "model": null, "dim": 256,
  "url": "http://127.0.0.1:11434", "chunks": 0, "stale": false, "stale_reason": null }
$ brain --json semantic reindex
{ "chunks": 9, "model": "hash:256", "dim": "256", "stale": false, "stale_reason": null }
$ brain --json search "rollback release" --mode hybrid
{ "items": [{"path": "docs/deploy.md", …, "score": 0.0328}, {"path": "owner/inbox/rollback-drill.md", …}, …], … }
```

`semantic enable` takes `--embedder hash|ollama`, `--dim`, `--model` (required for Ollama, e.g. an embedding model you have pulled), `--url`. `semantic status`, `semantic disable`.

**Maintenance and backups (owner)**

```console
$ brain --json maintain
{ "action": "maintain", "checkpoints": [{"database": "brain.sqlite3", "ok": true, "busy": false, …}, …],
  "busy": 0, "gc": {"scanned": 9, "removed": 0, "freed_bytes": 0},
  "repack": {"versions": 10, "rewritten": 1, "deltas": 1, "fulls": 9, …}, … }

$ brain --json backup create --output ./backups --keep 3
{ "archive": "./backups/brain-backup-20261122T090232717169Z.tar.gz",
  "manifest": "./backups/brain-backup-20261122T090232717169Z.tar.gz.manifest.json",
  "databases": ["access.sqlite3", "brain.sqlite3", "registry.sqlite3", "search.sqlite3", "semantic.sqlite3"],
  "files": 16, "bytes": 25440, "sha256": "1b59e7ef…", "removed": [] }

$ brain --json backup verify ./backups/brain-backup-20261122T090232717169Z.tar.gz
{ "archive": "…", "manifest": "…", "files": 16, "documents": 6, "ok": true }
```

Other commands: `ls`, `tree`, `blame`, `links`, `backlinks`, `changes [--cursor] [--limit]`, `export PATH [--output]`, `repack [--snapshot-every N]`, `disk-report [--days] [--forecast-days]`, `proposal submit|list|show|approve|reject|verify`, `upkeep list|sync|confirm|authorship`, `anomaly scan|list`, `sandbox run|status|cancel`, `serve`, `mcp`. Each has `--help`.

## HTTP API

```bash
brain serve                      # http://127.0.0.1:8765, --host, --port, --max-body (bytes, default 1 MiB)
```

All routes except `/health` and `/openapi.json` need `Authorization: Bearer <token>`. Errors are `{"error": {"type", "message", "hint"}}` (a conflict also carries `conflict`: current version, sections and a diff of at most 4000 characters) with status 400 (validation, secret), 401 (no or bad token), 403 (permission), 404 (not found), 409 (conflict, quota), 410 (deleted), 413 (body too large).

| Method and path | Operation |
|---|---|
| `GET /health` | `{"name": "artek-brain", "status": "ok"}` |
| `GET /openapi.json` | OpenAPI 3.0 description |
| `GET /documents/{path}` | read; query `version`, `section`, `line_start`, `line_end`, `max_chars` |
| `POST /documents/{path}` | write; body `content`, `reason`, `base_version`, `expected_sha256`, `batch_id` |
| `DELETE /documents/{path}` | soft delete; body `reason`, `base_version` |
| `GET /documents/{path}/history\|diff\|blame\|links\|backlinks\|toc\|export` | as in the CLI; `diff` takes `a`/`b` or `from`/`to` |
| `POST /documents/{path}/delete\|restore\|rollback` | body `reason`, `base_version`; `rollback` takes `to_version` |
| `POST /rollback/author`, `POST /rollback/batch` | body `author`, `since`, `until`, `reason` / `batch_id`, `reason` |
| `GET /search` | `q` (or `query`), `prefix`, `type`, `status`, `tag` (repeatable), `author`, `since`, `until`, `limit`, `offset`, `history`, `mode` |
| `GET /ls`, `GET /tree` | `prefix`, `max_depth` |
| `GET /changes` | `cursor`, `limit` |
| `GET /audit`, `GET /stats` | owner, auditor |
| `GET /readme`, `GET /who-knows?path=` | memory map, last change |
| `GET /tokens`, `POST /tokens`, `DELETE /tokens/{id}` | owner |
| `GET /agents`, `GET /brief` | presence, brief |
| `GET /inbox`, `POST /inbox/send`, `POST /inbox/read` | inbox |
| `POST /sandbox/run`, `GET /sandbox/status/{id}`, `POST /sandbox/cancel/{id}` | sandbox |
| `POST /rpc` | `{"op": "<Service method>", …params}` for any allowed operation |
| `POST /read`, `POST /write`, `GET /history`, … | flat aliases taking `path` as a parameter |
| `POST /overwrite` | owner only: replace the current version in place, no new version; an `overwrite` audit row keeps old and new sha256 |

```console
$ T="$(cat .brain/agent.token)"
$ curl -s localhost:8765/health
{"name": "artek-brain", "status": "ok"}

$ curl -s -i localhost:8765/documents/docs/deploy.md | head -1
HTTP/1.1 401 Unauthorized

$ curl -s -H "Authorization: Bearer $T" "localhost:8765/documents/docs/deploy.md?section=Rollback"
{"path": "docs/deploy.md", "version": 5, "content": "## Rollback\n- Restore the previous release.\n",
 "truncated": false, "total_chars": 44, "total_lines": 2, "section": "Rollback", "line_range": null}

$ curl -s -H "Authorization: Bearer $T" "localhost:8765/search?q=smoke&prefix=docs&limit=5"
{"items": [{"path": "docs/deploy.md", "version": 5, "title": "Deploy", …}], "total": 1, …}

$ curl -s -H "Authorization: Bearer $T" -X POST localhost:8765/documents/notes/2026-11-22-deploy.md \
    -d '{"content": "# Deploy notes\n\nSmoke tests take 3 minutes.\n", "reason": "session note"}'
{"path": "notes/2026-11-22-deploy.md", "version": 1, "parent_version": null, …, "op": "create", …}

$ curl -s -H "Authorization: Bearer $T" -X POST localhost:8765/documents/docs/deploy.md \
    -d '{"content": "# Deploy\n\n## Steps\n- Build.\n", "reason": "stale", "base_version": 1}' -w '\nHTTP %{http_code}\n'
{"error": {"type": "ConflictError", "message": "Конфликт в документе «docs/deploy.md»: текущая версия 5, …
 Пересекаются разделы: Steps.", "hint": "Прочитайте документ заново через read(path) и передайте base_version=5 …",
  "conflict": {"path": "docs/deploy.md", "current_version": 5, "conflicting_sections": ["Steps"], "diff": "--- docs/deploy.md@5\n+++ new\n…", "diff_truncated": false}}}
HTTP 409

$ curl -s -H "Authorization: Bearer $T" -X POST localhost:8765/rpc -d '{"op": "history", "path": "docs/deploy.md", "limit": 1}'
[{"path": "docs/deploy.md", "n": 5, "parent_n": 4, …, "reason": "add tagging step", "op": "write"}]

$ curl -s -H "Authorization: Bearer $T" localhost:8765/stats -w '\nHTTP %{http_code}\n'
{"error": {"type": "PermissionDenied", "message": "Отказ: для действия «stats» нужно право «audit» у «agent». …"}}
HTTP 403
```

The server binds to `127.0.0.1` by default and has no TLS; put it behind a reverse proxy if it must be reachable from other hosts.

## SessionStart hook

Agents read memory reliably only when it is already in their context. `brain hook session-start` prints the brief, and Claude Code puts a `SessionStart` hook's stdout into the agent's context. With the matcher `startup|resume|clear|compact` this happens at the start of a session and again after context compaction.

`brain setup` writes this to the project's `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
          { "type": "command", "command": "/abs/path/to/brain hook session-start", "timeout": 15 }
        ]
      }
    ]
  }
}
```

**Install it once for the user instead.** Put the same block into `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`). It then works in every project and in every git worktree, including worktrees that do not contain the project's `.claude/settings.json`. When `brain setup` finds the string `hook session-start` in that file, it does not add a project-level hook, so the brief is not injected twice.

**How it behaves.** The hook runs in the session's working directory, so it finds the nearest `.brain` upwards (`resolve_root`) and uses `BRAIN_TOKEN` or `.brain/agent.token`. In a worktree created inside the project folder this is the project's store. A worktree outside the project folder needs `BRAIN_HOME` pointing at the project's `.brain`. If there is no store or no token, the hook prints nothing and exits 0; it never fails the session start.

```console
$ brain hook session-start
Память проекта (artek-brain, MCP-сервер brain). Бриф на сейчас:
# Бриф для agent
Ориентир: README.md, backlog.md (доски задач нет).
Сейчас работают: 1
- agent: writing deploy docs
Последние правки памяти:
- docs/deploy.md v4 — agent: revert team note
…
Inbox: 0 непрочитанных
Дальше: brain_read нужных документов из брифа, brain_search перед правкой незнакомой части.
```

## Agent workflow

The rules block that `brain setup` writes to `AGENTS.md` describes this loop. In MCP terms:

1. **Brief.** `brain_op(action="brief")` (or the hook does it). It lists anchor documents, documents overdue by TTL ("check before relying"), docs behind notes, who else is working, recent edits and unread inbox.
2. **Read.** `brain_read(path="backlog.md")`, then the documents you need. Keep the returned `version`. If `truncated` is true, continue with `next.line_start` or a `section` from `remaining_sections` before relying on the document.
3. **Search** before touching an unfamiliar area: `brain_search(query="deploy")`. One or two words work best; if the response has `"match": "any"`, the results are a looser match.
4. **Write** with the version you read: `brain_write(path, content, reason, base_version=<version>)`.
   - `op: "write"`: nobody changed the document since you read it.
   - `op: "merge"`: someone changed *other* sections; their edits are kept and yours are applied.
   - `op: "noop"`: your content equals the current version.
5. **On a conflict** (`ConflictError`, HTTP 409): someone changed the *same* section after you read it. Nothing was written. The response already has a diff of the current text against yours (`conflict.diff` in HTTP, MCP and `--json`, stderr in plain CLI). Read the current version (`brain_read(path)`), or look at what changed with `brain_op(action="diff", path=…, a=<your base>, b=<current>)` and `brain_op(action="history", path=…)`. Re-apply your edit on top of the current text and write again with `base_version=<current>`. Do not retry with the old `base_version`, and do not drop the other agent's edit unless you mean to.
6. **Finish.** Write a note to `notes/` and update the reference document in `docs/` (or `backlog.md`) if facts or decisions changed. For `README.md`, `rules/`, `approved/` and `owner/`, which only the owner can change, leave a proposal in `owner/inbox/<name>.md`.
7. **Delegating.** When handing work to a subagent or another session, put "start with `brain_op(action=\"brief\")`, write the result to memory" into its prompt; in production use, subagents ignored memory unless the prompt named it.
