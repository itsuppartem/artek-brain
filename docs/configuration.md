# Configuration

artek-brain has three places for configuration: environment variables, the store's `settings.json`, and documents inside the memory (`rules/schemas.json`). Command-line flags cover the servers. Anything not listed here (quotas, sandbox limits, anomaly thresholds) is set through the Python API only, see [Not configurable from files](#not-configurable-from-files).

- [Environment variables](#environment-variables)
- [settings.json](#settingsjson)
  - [semantic](#semantic)
  - [aging](#aging)
  - [brief](#brief)
- [rules/schemas.json](#rulesschemasjson)
- [Frontmatter fields](#frontmatter-fields)
- [Server flags](#server-flags)
- [Not configurable from files](#not-configurable-from-files)

## Environment variables

| Variable | Read by | Effect |
|---|---|---|
| `BRAIN_HOME` | `service.resolve_root` (CLI, MCP, hook, HTTP) | Store directory. Overrides the upward search for `.brain`. `--root` overrides it. |
| `BRAIN_TOKEN` | `cli._token` (CLI, MCP, hook, `serve` does not use it) | Token to act with. `--token` overrides it. If neither is set, `.brain/agent.token` in the store is used. |
| `BRAIN_SESSION` | `mcp_server.BrainMCP` | Session id written to the `session` column of every audit row from this MCP process. Default: 12 random hex chars per process. |
| `BRAIN_MODEL` | `mcp_server.BrainMCP` | Model name for the `model` audit column. |
| `BRAIN_CLIENT` | `mcp_server.BrainMCP` | Client label, appended to the `model` column as `<model> · <client>`. If unset, the MCP client's `clientInfo` (`name/version`) from `initialize` is used. `brain setup` sets it to `claude-code` in `.mcp.json` and `opencode` in `opencode.json`. |
| `CLAUDE_CONFIG_DIR` | `project._ensure_claude_hook` | Where `brain setup` looks for the user-level Claude Code `settings.json` (default `~/.claude`). If that file already contains `hook session-start`, no project hook is added. |
| `BRAIN_SANDBOX_TESTS` | `tests/conftest.py` | `1` runs the sandbox tests, which need `bwrap` and `systemd-run` without a password prompt. Skipped otherwise. |
| `BRAIN_LOAD_VERSIONS`, `BRAIN_LOAD_DOCS` | `tests/test_load.py` | Size of the load test (`uv run pytest -m load`): defaults 20000 versions over 500 documents. |

The HTTP server takes the token from each request's `Authorization: Bearer` header, not from the environment.

Example: an agent session outside the project tree, with its own label in the audit log:

```bash
export BRAIN_HOME=/path/to/project/.brain
export BRAIN_SESSION=nightly-docs-check
export BRAIN_MODEL=my-model
brain mcp
```

## settings.json

Optional file `<store>/settings.json` (e.g. `.brain/settings.json`). It is a JSON object with up to three blocks. The file is read when a `Service` is created (that is, when an MCP, CLI or HTTP process starts) and, for `aging`/`brief`, again on every brief and quality report. Unknown keys are ignored.

A complete example with all defaults written out:

```json
{
  "semantic": {
    "enabled": false,
    "embedder": null,
    "model": null,
    "url": "http://127.0.0.1:11434",
    "dim": 256,
    "timeout": 10.0
  },
  "aging": {
    "ttl_days": {
      "status.md": 3,
      "backlog.md": 7,
      "README.md": 14,
      "docs/prod.md": 7,
      "docs/": 30,
      "owner/": 60,
      "notes/": 90
    },
    "default_days": 60
  },
  "brief": {
    "paths": ["README.md", "backlog.md"]
  }
}
```

### semantic

Defined in `semantic.py` (`SemanticSettings`, `load_settings`). Prefer `brain semantic enable|disable` (owner only, audited) over editing the file; it rewrites the block atomically and keeps the other blocks.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turn the semantic index on. When off, `mode=semantic` is an error and `mode=hybrid` runs as `fts` with a warning. |
| `embedder` | `null` | `hash` (deterministic, no model, no network) or `ollama` (local Ollama HTTP API). `brain semantic enable` defaults to `hash`. |
| `model` | `null` | Ollama embedding model name. Required for `ollama`. |
| `url` | `http://127.0.0.1:11434` | Ollama base URL; requests go to `<url>/api/embed`. |
| `dim` | `256` | Vector size for `hash` (minimum 8). For Ollama the model decides. |
| `timeout` | `10.0` | Ollama request timeout in seconds. |

Changing `embedder`, `model` or `dim` marks the existing index stale; run `brain semantic reindex`. An existing store's documents are indexed by `reindex`, not on enable.

Fixed in code, not in settings: chunk size 1500 characters (`CHUNK_CHARS`), FTS prefilter above 50,000 chunks (`PREFILTER_THRESHOLD`), RRF constant 60 (`DEFAULT_RRF_K`).

If `settings.json` is not valid JSON, reads, writes and FTS search keep working, but `mode=semantic` and `mode=hybrid` fail with an error that names the file; the `aging`/`brief` reader silently falls back to defaults.

### aging

Defined in `aging.py` (`DEFAULT_TTL_DAYS`, `DEFAULT_DAYS`, `load_aging`).

| Key | Default | Meaning |
|---|---|---|
| `ttl_days` | see the example above | Map of path keys to a freshness period in days (minimum 1). Your keys are merged over the defaults; to change a default, repeat its key. |
| `default_days` | `60` | TTL for paths that match no key. |

Key matching (`AgingSettings.ttl_for`):

- a key ending in `/` is a folder prefix: `docs/` matches `docs/deploy.md` and `docs/api/auth.md`;
- any other key is an exact path or an `fnmatch` pattern: `docs/prod.md`, `notes/*.md`;
- the longest matching key wins, so `docs/prod.md` (7) beats `docs/` (30).

A document is overdue when the age of its last version exceeds its TTL. Overdue documents appear in the brief under "check before relying" and in `brain quality report`. In the quality report, a document with `review_by` in its frontmatter is judged by `review_by` instead of the TTL; the brief uses the TTL for every document.

Example: a project where the deploy runbook goes stale in three days and decisions are kept for half a year:

```json
{
  "aging": {
    "ttl_days": {"docs/deploy.md": 3, "docs/decisions/": 180},
    "default_days": 45
  }
}
```

### brief

| Key | Default | Meaning |
|---|---|---|
| `paths` | `["README.md", "backlog.md"]` | Anchor documents listed at the top of the brief, if they exist. Must be a non-empty list of strings, otherwise the default is used. |

The brief's length is a call parameter (`limit`, default 2000 characters, minimum 200), not a setting.

## rules/schemas.json

A document inside the memory (not a file in `.brain/`), so only the owner can write it and every change is versioned. It maps a frontmatter `type` to required frontmatter fields. `Quality.load_schemas` reads `rules/schemas.json` first, then `rules/schemas.md`.

Accepted JSON shapes (`Quality._parse_json_schemas`):

```json
{"decision": ["owner", "status", "review_by"], "runbook": "owner, review_by"}
```

```json
{"types": {"decision": {"required": ["owner", "status"]}}}
```

The Markdown form uses one line per type, `decision: owner, status, review_by`, or a table with the type in the first column and comma-separated fields in the second.

Where schemas apply:

- **On write**, only for paths under `approved/` (`Quality.strict_check`, called by `Service.write`). A document whose `type` has a schema and lacks a required field is rejected:

  ```console
  $ printf '{"decision": ["owner", "status", "review_by"]}\n' | brain write rules/schemas.json --reason "schemas"   # owner token
  $ printf -- '---\ntype: decision\n---\n# X\n' | brain write approved/x.md --reason "try"
  Ошибка: Документ «approved/x.md» не проходит схему типа «decision»: нет полей owner, status, review_by. …
  ```

- **On demand** for any path, through `Quality.validate` (Python API).

Documents without `type`, or with a type that has no schema, always pass.

## Frontmatter fields

Frontmatter is optional. The parser (`frontmatter.py`) supports `key: value` scalars and `[a, b]` lists between `---` lines. Fields with meaning to artek-brain:

| Field | Used by |
|---|---|
| `type` | search filter `type`, schemas |
| `status` | search filter `status`; `approved` and `stale` matter to quality checks; `review` puts the document into the review queue on `queue sync` |
| `tags` | search filter `tags` |
| `owner` | quality report (`without_owner`), schemas |
| `review_by` | quality report and review queue (overdue date) |
| `refs` | drift check: `path@sha256` or `path#symbol@git-sha` |
| `facts` | contradiction check: `[key=value, …]` in approved documents |

```markdown
---
type: decision
status: approved
owner: owner
tags: [deploy, release]
review_by: 2026-12-31
---
# Release cadence

We release on Tuesdays.
```

```console
$ brain --json search release --type decision --tag deploy
{ "items": [{"path": "docs/decisions/release.md", "type": "decision", "status": "approved",
             "tags": ["deploy", "release"], …}], "total": 1, … }
```

## Server flags

**`brain mcp`**

| Flag | Default | Meaning |
|---|---|---|
| `--max-chars N` | `8000` | Maximum characters per tool response. Long reads are cut by lines with `remaining_sections`; other responses are cut into JSON-text chunks with a `cursor`. Values below 200 are rejected. |
| `--root DIR`, `--token TOKEN` | see environment | Store and token for the whole session. |

To pass a flag from Claude Code, add it to `args` in `.mcp.json`: `"args": ["mcp", "--max-chars", "12000"]`.

**`brain serve`**

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address. |
| `--port` | `8765` | Port. |
| `--max-body` | `1048576` | Maximum request body in bytes; larger bodies get HTTP 413. |

## Not configurable from files

These limits have defaults in code and can be changed only by constructing the objects yourself in Python (for example `Service(root, quota=Quota(...))`); the CLI, MCP and HTTP servers use the defaults.

| Setting | Where | Default |
|---|---|---|
| Document size | `Quota.max_document_bytes` | 1 MiB |
| Versions per document | `Quota.max_versions_per_document` | 10,000 |
| Writes per author per UTC day | `Quota.max_writes_per_author_per_day` | 100,000 |
| Total store size | `Quota.max_disk_bytes` | none |
| Full snapshot interval / delta threshold | `Store(snapshot_every=…, delta_ratio=…)` | 20 / 0.9 (`brain repack --snapshot-every N` overrides the interval for one repack) |
| Sandbox limits and role ceilings | `SandboxConfig` | agent: 10 s CPU, 20 s wall, 512 MiB, 32 processes; 2 concurrent runs, 1 per token |
| Sandbox tool calls per token per day | `Registry.register(..., daily_limit=100)` in `Service` | 100 |
| Anomaly thresholds | `AnomalyConfig` | 10-minute window, 20 versions, 10 documents, 3 rollbacks, 3 protected-path attempts, 1 hidden read, size ×5 above 100 kB, `auto_suspend` off (`brain anomaly scan --auto-suspend` turns it on for one scan) |
| Duplicate threshold | `Quality(threshold=…)` | 0.8 (`brain quality dups --threshold` per call) |
