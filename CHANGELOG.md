# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/). Versions are not tagged in git; dates are those of the last commit of each version.

## [0.2.1] - 2026-11-22

Fixes for issues found while writing the architecture and usage docs.

### Added

- Conflicts return a diff: `conflict` with the current version, conflicting sections and a unified diff (up to 4000 characters) in HTTP, MCP and `--json`; plain CLI prints the diff to stderr.
- Quotas are configurable in `settings.json` → `quota`.
- The brief honours `review_by` from frontmatter, like the quality report.

### Changed

- `maintain` runs repack before gc, so space freed by repack is reclaimed in the same run.
- Owner `overwrite` still creates no new version, but now writes an `overwrite` audit row with the old and new sha256.
- Version strings in the OpenAPI document and the MCP `serverInfo` come from the package version; CLI and OpenAPI descriptions no longer mention the removed task board.

### Fixed

- `brain_op(action="changes", cursor=N)` in MCP: the cursor was taken by the response limiter, so the feed always started from 0 and the answer was cut at character N. Response continuation for `changes` now uses `response_cursor`.
- `search.sqlite3` (full text of documents), `access.sqlite3` and `semantic.sqlite3` were created with mode 644; they are now 600, and `blobs/` is 700.
- `gc` could delete a blob that another process had written but not yet committed; it now skips files younger than one hour.
- `semantic` status returns `dim` as a number; `brain token revoke` without `--json` prints how many tokens were revoked.

## [0.2.0] - 2026-11-22

Changes driven by an audit of a production instance after its first week of use (13–21.11.2026): reference docs were not updated, truncated reads were not continued, multi-word searches often returned nothing, the audit log had no sessions or searches, and agents did not re-read memory after context compaction.

### Added

- Knowledge aging (`aging.py`): a freshness period per folder or file from `settings.json` → `aging.ttl_days` (defaults include `backlog.md` 7 days, `docs/` 30, `notes/` 90, others 60), and a "docs behind notes" detector for reference documents followed by three or more new notes.
- The brief lists documents overdue by TTL under "check before relying" and docs behind notes; its anchor documents are configurable via `brief.paths`.
- `brain hook session-start`, and `brain setup` installs it as a Claude Code `SessionStart` hook (matcher `startup|resume|clear|compact`), so the brief is injected at start and after compaction.
- Session, model and client in the audit log for MCP calls (`BRAIN_SESSION`, `BRAIN_MODEL`, `BRAIN_CLIENT`, or the client's `clientInfo`); `brain setup` labels `.mcp.json` as `claude-code` and `opencode.json` as `opencode`.
- Successful searches are written to the audit log (query, mode, hits, match mode).
- Search fallback: when a query with all words finds nothing, it is retried with any word by stem (`match: "any"`, with a note in the response).
- `PRAGMA user_version` schema versioning (schema 1); a store created by a newer version is refused instead of opened.
- `scripts/bench.py` for write, read and search timings; GitHub Actions test workflow.

### Changed

- Long documents in `brain_read` are cut at line boundaries, with `shown_lines`, `next.line_start` and `remaining_sections` instead of a JSON cursor.
- `brain quality report` counts freshness by TTL for documents without `review_by`, instead of reporting 100% fresh when no document had `review_by`.
- `brain setup` replaces the rules block in `AGENTS.md` between markers on every run (text outside the markers is kept) and adds missing `BRAIN_CLIENT` labels.
- `brain setup` does not add a project-level hook when the user's `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`) already has one, which also covers git worktrees.
- Commands other than `setup` and `init` no longer create an empty store silently when none is found.
- The MCP overview starts with "begin with `brain_op(action="brief")`".
- `brain.sqlite3` and its WAL files get mode 600; a passive WAL checkpoint runs after each write; `journal_size_limit` keeps the WAL at 1 MB after checkpoints.
- Rows of the removed task-board tables are saved to `.brain/legacy/*.json` before the tables are dropped.
- README: CI badge, architecture diagram, lessons from production use, benchmark numbers.

### Fixed

- Search audit rows were written outside a committed transaction, so an MCP process kept the SQLite write lock after its first search and other processes got "database is locked". The row is now written in its own short transaction.
- `brain_search(type=...)` failed; the `type` filter now works over MCP.
- Contradiction detection no longer takes a number from the middle of a phrase as a fact.

## [0.1.0] - 2026-11-14

First version. Built from the specification in `docs/TZ.md`, stage by stage (`notes/BRAIN-1.md` … `notes/BRAIN-11.md`).

### Added

- Core store: SQLite (WAL) for metadata, content in sha256-addressed, compressed, deduplicated blobs; every write a new version with author, reason, sha256, size and parent; soft delete and restore; history, diff, blame by section; export and import.
- Writes with `base_version` / `expected_sha256`; three-way merge by Markdown section; `ConflictError` naming the conflicting sections.
- Rollback of a version, of one author's edits over a period, or of a batch (`batch_id`), with skipped items reported.
- Reads by version, section, line range and character budget.
- Access control: tokens stored as sha256, roles `owner`, `human`, `agent`, `auditor`, scopes by path prefix (`read`/`write`/`admin`), TTL, revoke, suspend; protected paths `owner/`, `rules/`, `approved/`, `scoring/`, `hidden/`, root `README.md`; create-only `owner/inbox/`; Unicode look-alike normalisation in path checks.
- Secret scanner on every write; secrets redacted in audit details and reasons.
- Quotas: document size, versions per document, writes per author per day, optional disk limit.
- Full-text search on SQLite FTS5 (BM25) over current versions and history, with path, type, status, tag, author and date filters; incremental index catch-up by store fingerprint.
- Navigation: `ls`, `tree`, table of contents, links, backlinks, broken links, change feed, README map.
- Interfaces: MCP stdio server with six tools and a `brain_op` multiplexer, `brain` CLI, HTTP JSON API with OpenAPI.
- Delta storage: line deltas with a full snapshot every 20 versions, `repack`, lzma archive of cold versions, disk report.
- Optional semantic search (hash or Ollama embedder) and hybrid FTS + semantic ranking by RRF.
- Knowledge quality: duplicates, contradictions, broken links, type schemas from `rules/schemas.json`, drift of `refs` against code, review queue.
- Defences against agent shortcuts: proposals approved by a human, guard against silent edits of `approved/`, anomaly detectors with optional token suspension.
- Script sandbox (bubblewrap, systemd scope, prlimit, no network) and a tool registry.
- Backups with sha256 manifests and verification; `brain maintain` (WAL checkpoint, gc, repack).
- Coordination: inbox, presence, `who_knows`, brief.
- `brain setup`: one store per project in `.brain/`, with tokens, `.mcp.json`, `opencode.json` and an `AGENTS.md` rules block; the store is found by walking up from the current directory.
- English README and MIT license.

### Removed

- The task board (tasks, claims with leases, reviews, handoffs, evidence checks, server-side runs) was built during 0.1.0 development and removed on 2026-11-12 before the version was finished; see [docs/design-decisions.md](docs/design-decisions.md#removing-the-task-board).

[0.2.1]: https://github.com/itsuppartem/artek-brain/compare/d254dc8...main
[0.2.0]: https://github.com/itsuppartem/artek-brain/compare/29f26f7...d254dc8
[0.1.0]: https://github.com/itsuppartem/artek-brain/commits/29f26f7
