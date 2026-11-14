# artek-brain

Shared, versioned memory for people and AI coding agents. One instance per project.

Agents (Claude Code, opencode, Cursor) lose context between sessions and overwrite each other's notes. artek-brain gives them one place to read and write project knowledge through MCP, CLI or HTTP, with every change versioned, attributed and reversible. There are no LLM calls inside the memory layer: reads and writes are deterministic.

[Русская версия](README.ru.md) · [Specification (RU)](docs/TZ.md)

## What it does

- **Versioned documents.** Markdown with frontmatter. Every write is a new version with author, reason, sha256 and diff. Roll back a document, a version, one author's edits over a period, or a bad batch.
- **Explicit conflicts.** Writes carry `base_version`; on mismatch the write is refused with the current version and diff. Edits to different sections merge, edits to the same section conflict.
- **Access control.** Read, write and admin rights by path prefix; scoped agent tokens with TTL and revocation. Agents cannot change `rules/`, `owner/` or approved documents; they leave proposals in `owner/inbox` for a human to approve.
- **Secret scanning.** Writes containing keys, tokens or PEM blocks are rejected; secrets are stripped from logs.
- **Search and navigation.** SQLite FTS5 (BM25) with filters, outlines with section sizes, reads by section or token budget, links and backlinks, "what changed since my last visit".
- **Audit and metrics.** Every action is logged: who, what, when, why, from which session.
- **Cheap storage.** Metadata in SQLite (WAL), content in hash-addressed blobs with dedup and compression, quotas, garbage collection, backups with sha256 manifests.

## Quick start

```bash
uv tool install --from git+https://github.com/itsuppartem/artek-brain "artek-brain[mcp]"
cd /path/to/project && brain setup --name "My project"
```

`brain setup` creates `.brain/` (database plus `owner.token` and `agent.token`, mode 600, git-ignored), `.mcp.json` for Claude Code, `opencode.json` for opencode agents, and a rules block in `AGENTS.md`. The MCP server looks for `.brain` up the directory tree, so agent worktrees inside the project share the same memory.

## Tests

```bash
uv run pytest -q                              # 173 tests
BRAIN_SANDBOX_TESTS=1 uv run pytest -q        # plus sandbox tests (use systemd-run)
uv run pytest -m load                          # load tests
```

## How it was built

Designed and specified by me; most of the code was written by AI coding agents (opencode and Claude Code) working from the spec in `docs/TZ.md`, with review and tests before merge. It is used day to day as the memory layer for my own agent-driven projects.

## Status

Early (0.1.0), single-tenant, self-hosted. Python 3.12+, stdlib plus minimal dependencies (`mcp` is optional).

## License

MIT
