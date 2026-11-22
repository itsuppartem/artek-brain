# Design decisions

Short records of the main decisions: what was decided, what the alternative was, and why. Sources are the specification (`docs/TZ.md`, in Russian), the development notes (`notes/BRAIN-*.md`, in Russian), commit messages and the code. The last section lists known trade-offs and limitations.

- [1. No LLM inside the memory layer](#1-no-llm-inside-the-memory-layer)
- [2. SQLite in WAL mode plus hash-addressed blobs](#2-sqlite-in-wal-mode-plus-hash-addressed-blobs)
- [3. Progressive disclosure: six tools and `brain_op`](#3-progressive-disclosure-six-tools-and-brain_op)
- [4. Explicit conflicts: `base_version` and section merge](#4-explicit-conflicts-base_version-and-section-merge)
- [5. Secret scanning on write](#5-secret-scanning-on-write)
- [6. One instance per project, found by walking up to `.brain`](#6-one-instance-per-project-found-by-walking-up-to-brain)
- [7. TTL-based aging instead of mandatory frontmatter](#7-ttl-based-aging-instead-of-mandatory-frontmatter)
- [8. Brief injected by a hook, including after compaction](#8-brief-injected-by-a-hook-including-after-compaction)
- [9. Removing the task board](#removing-the-task-board)
- [Trade-offs and limitations](#trade-offs-and-limitations)

## 1. No LLM inside the memory layer

**Decision.** The memory makes no model calls. Reads, writes, merges, search ranking, quality checks and the brief are deterministic functions of the stored data. The one network client in the package, `OllamaEmbedder`, is optional, off by default and only computes embeddings.

**Alternative.** "Smart" memory that summarises, deduplicates or rewrites notes with an LLM on write or read.

**Why.** The specification makes it a principle ("В слое памяти нет LLM-вызовов", TZ §1.5) and lists "smart memory without a trace" as an anti-goal (TZ §5). The memory has to be the thing you can check when an agent was wrong: same input, same output, every change attributable to a token and a reason. It also has to outlive a change of model, harness or vendor (TZ §0), so it cannot depend on one. Agents are the LLMs; the memory is their shared, auditable state.

**Cost.** Anything that needs judgement (merging two edits of the same section, deciding that a note supersedes a doc) is left to the agent or a human. The memory reports the situation (conflict, overdue, docs behind notes) instead of resolving it.

## 2. SQLite in WAL mode plus hash-addressed blobs

**Decision.** Metadata (documents, versions, audit, tokens, indexes) in SQLite with `journal_mode=WAL`; content in files under `blobs/` named by sha256, zlib-compressed, with line deltas between versions and a full snapshot every 20 versions.

**Alternatives.** PostgreSQL; a git repository as the store or transport; content as BLOB columns inside SQLite.

**Why.**

- *Not Postgres*: the system is single-tenant and self-hosted (TZ §6), runs next to each project and should need no server to operate. SQLite ships with Python; the core uses only the standard library (`AGENTS.md`), and `mcp` is the only optional dependency. Backup is `Connection.backup` plus copying immutable files.
- *Not git*: "git with manual merges for agents" is an anti-goal (TZ §5) and "git is not a transport, only the API" is a default (TZ §6). Agents need an atomic write that either succeeds or returns a structured conflict, per-section merge, permission checks per path and an audit row per read; git gives none of these without a server in front of it, and its merge conflicts are text an agent has to repair by hand.
- *WAL*: every agent session starts its own MCP server process, so several processes read and write the same database. WAL lets readers proceed during a write; short transactions with `busy_timeout = 30 s` serialise writers.
- *Blobs outside the database*: content is deduplicated by hash for free, the database stays small, blobs are immutable (safe to copy during a backup), and the delta layer could be added later without a migration because blobs carry their own format header and old raw-zlib blobs are still readable.

**Evidence.** The load test in `notes/BRAIN-5.md`: 20,000 versions of ~25 KB documents took 35 MB on disk (13.4 MB blobs) against 499 MB of logical content, with reads of the latest version in 2.7 ms.

## 3. Progressive disclosure: six tools and `brain_op`

**Decision.** The MCP server lists six tools: `brain_help`, `brain_read`, `brain_write`, `brain_search`, `brain_ls`, `brain_op`. About 30 further operations (history, diff, rollbacks, inbox, tokens, sandbox, …) are actions of `brain_op`, and `brain_help(topic)` returns the schema and an example for any of them. Responses are size-limited.

**Alternative.** One MCP tool per operation, each with a full JSON schema.

**Why.** Tool definitions are loaded into every agent's context on every session. Thirty-plus schemas cost context in every call and make tool choice harder, while most sessions only read, search and write. The specification asks for "a small starting set of tools, help per tool, clear errors, response limits, pagination" (TZ §3.6). Errors follow the same idea: every error has a message and a hint saying what to pass instead.

**Cost.** `brain_op` has `additionalProperties: true`, so the client cannot validate action parameters; the server does, and its errors say what to pass and point to `brain_help`.

## 4. Explicit conflicts: `base_version` and section merge

**Decision.** A write to an existing document must carry the `base_version` it was based on. If the document moved on since then, the server merges by Markdown section: edits to different sections are combined (`op: merge`), edits to the same section are refused with `ConflictError` listing the sections. `expected_sha256` offers a stricter check that refuses any intervening change.

**Alternatives.** Last write wins; pessimistic locks or leases; line-level three-way merge as in git.

**Why.**

- *Not last write wins*: silently losing an edit is the failure the specification ranks worst ("better a refusal with a clear error than a quiet loss", TZ §1.8). With many agents writing `backlog.md`, it would happen daily.
- *Not locks*: an agent can crash, be compacted or simply forget to release; leases need heartbeats and expiry. The removed task board had exactly this machinery (claims, heartbeats) and it was not used (see [decision 9](#removing-the-task-board)).
- *Sections, not lines*: a section is the unit agents think in ("Steps", "Rollback"). Two edits of the same section are likely to be semantically related even if their lines differ, and a refusal is cheap: re-read, re-apply, write. Two edits of different sections are almost always independent, so merging them removes most conflicts without guessing.

**In practice.** The README records a concurrent edit of the backlog caught by `base_version` and merged without loss during the first week of production use.

## 5. Secret scanning on write

**Decision.** Every write is scanned (`secrets.py`) for AWS keys, `sk-` API keys, GitHub and Slack tokens, PEM private keys, JWTs and `password|secret|api_key|access_token = value` pairs. A hit rejects the whole write with the secret type and line number; the secret itself never appears in the error. Reasons are redacted before they are stored, and audit details are passed through `sanitize_json`.

**Alternatives.** No scanning (trust agents); redact and store; scan asynchronously.

**Why.** Memory is shared and versioned: a secret written once stays in history and is readable by every agent with access, and rollback does not remove it. Agents paste configuration and command output into notes as a matter of course. Rejecting is better than redacting because a redacted note silently loses information the author thought they recorded; the error tells them to store a reference instead. The README records a secret in a note being rejected and rephrased within seconds.

**Cost.** Regexes produce false positives (`password: see the vault` is rejected) and miss formats they do not know. There is no PII detection, which the specification also mentions (TZ §3.3).

## 6. One instance per project, found by walking up to `.brain`

**Decision.** `brain setup` creates a store in `<project>/.brain`. Every command looks for the store in `--root`, then `BRAIN_HOME`, then the nearest `.brain` in the current directory or its parents. Commands other than `setup` and `init` refuse to run when no store is found.

**Alternatives.** One central instance per team (the original default in TZ §0); a store per working directory.

**Why.** The original specification describes one instance for a team or swarm. Before the switch, a shared instance kept projects apart by a path convention (the brief pointed agents at `projects/<name>/backlog.md`). Commit `967c656` (13.11.2026) replaced that with one store per project, made the brief point at the root `README.md` and `backlog.md`, and let the CLI and MCP pick up `.brain/agent.token` without configuration. Walking up the tree is what makes it work with agents: Claude Code subagents and parallel sessions run in git worktrees and subfolders, and they all find the project's `.brain` without configuration. Refusing to create an empty store prevents the failure where an agent in the wrong directory writes into a fresh, invisible store.

**Cost.** Knowledge shared between projects has to be copied or kept in one of them. Worktrees created outside the project tree need `BRAIN_HOME`.

## 7. TTL-based aging instead of mandatory frontmatter

**Decision.** Every document has a freshness period derived from its path (`settings.json` → `aging.ttl_days`; defaults: `backlog.md` 7 days, `docs/` 30, `notes/` 90, others 60). A document whose last version is older than its TTL is reported as overdue in the brief and the quality report. A detector flags reference documents after which three or more new notes appeared ("docs behind notes"). An explicit `review_by` in frontmatter still takes precedence, in the brief and in the quality report.

**Alternative.** Require `review_by` and `owner` in frontmatter, enforced by schema on every write (what `quality.py` supported since 0.1.0).

**Why.** The audit of a production instance after its first week (README, "Lessons from real use"; `notes/BRAIN-12.md`): agents almost never set `review_by`, so the quality report showed 100% freshness while reference docs had not been touched since they were first filled; a typical operational fact stayed true for 3–5 days; 50+ notes piled up while docs stayed the same. Requiring frontmatter would have turned every write into a schema negotiation and still produced made-up dates. A TTL per folder needs no cooperation from the writer and encodes what the owner actually knows: the backlog goes stale in a week, architecture notes in months.

**Cost.** TTLs are heuristics; a stable document will be flagged on schedule even if nothing changed, unless it sets `review_by`.

## 8. Brief injected by a hook, including after compaction

**Decision.** `brain hook session-start` prints the brief; `brain setup` registers it as a Claude Code `SessionStart` hook with the matcher `startup|resume|clear|compact`. The MCP `brain_help()` overview also starts with "begin with `brain_op(action="brief")`".

**Alternative.** Rely on instructions in `AGENTS.md` / `CLAUDE.md` to make the agent read memory.

**Why.** The same audit found that agents read memory only when told to: worktree sessions and subagents ignored it unless the prompt named it, and after context compaction the agent recovered from files on disk, not from memory. Instructions in a file are one more thing to forget after compaction. A hook's output lands in the context deterministically, at start and right after compaction, which is when the agent has just lost its picture of the project. The brief is short (2000 characters by default) so it can be injected every time.

**Details.** If the hook is already in the user's `~/.claude/settings.json` (it then covers every project and worktree), `brain setup` does not add a project-level copy, so the brief is not injected twice. The hook never fails the session: no store or no token means no output.

**Cost.** Specific to Claude Code's hook mechanism; opencode agents rely on the `AGENTS.md` instructions. Because subagents ignored memory unless their prompt named it, the rules also tell agents to put "start with the brief" into every delegated prompt.

## Removing the task board

**Decision.** On 2026-11-12 the task board was removed from the memory (commit `ab62c8e`). Inbox, presence, `who_knows` and the brief stayed, moved to `coord.py`. Agents orient themselves by notes in memory, primarily `README.md` and `backlog.md`.

**What was removed.** `tasks.py` (1827 lines: tasks with ids, priorities and dependencies; claims with leases, heartbeats and area-overlap protection; optimistic revisions; review, acceptance and rework; handoffs), `evidence.py` (498 lines, typed evidence checked against the audit log) and `runs.py` (585 lines, server-side test runs). The MCP tool `brain_task` became `brain_op`; the CLI `task` and `run` commands became `coord`; HTTP `/tasks` became `/agents`. Old stores are migrated on open: messages and presence are kept, task tables are dropped and their rows saved to `.brain/legacy/*.json`.

**Why.** The commit records the owner's decision: agents did not take or close tasks on the board; the working reference was the backlog note. The board duplicated what a versioned `backlog.md` already provides (a list of what is being done, edited by everyone, with history and merge), while adding a second source of truth that drifted from it, plus the largest and most stateful module in the codebase. The specification was updated the same day (TZ §3.9, §6).

**Cost.** No machine-enforced claims: two agents can start the same work. The mitigations are presence (`agents`), the brief's "who is working now" and recent-edits list, and section-level conflicts on `backlog.md`.

## Trade-offs and limitations

- **Single-tenant.** One store per project, one owner. No multi-tenant isolation, no organisations, no SSO. Tokens are bearer secrets in files with mode 600. The HTTP server binds to localhost and has no TLS.
- **The MCP cursor cuts JSON text.** Reads are fitted by lines with a list of unread sections, but every other oversized response is returned as a character slice of its JSON plus a `cursor`; the client has to join all chunks before it can parse anything. Pagination by data (e.g. `limit`/`offset` on every list) was not done yet (`notes/BRAIN-12.md`). The same mechanism consumes the `cursor` argument of `brain_op(action="changes")`.
- **Search is linear in the number of matches.** About 170 ms median on 1000 notes (`scripts/bench.py`, see README) against 0.2 ms for a read: the query has no SQL `LIMIT` because permission filtering happens in Python, and every search computes a store fingerprint and writes an audit row. Fine for project-sized memories (hundreds to low thousands of documents), not for large corpora.
- **Morphology is crude.** The `match=any` fallback cuts the last two characters of words longer than five and uses them as prefixes. It helps with Russian and English inflection in common cases and over-matches or misses in others. There is no stemmer or lemmatiser, by design (stdlib only).
- **Semantic search is brute force.** Cosine over all chunks in Python, with an FTS prefilter above 50,000 chunks; no ANN index. The default `hash` embedder is lexical, not semantic in the model sense.
- **Every read writes.** Reads are audited in the same transaction, so a read is a small write transaction. This is what makes "who read what before acting" answerable, and it limits read throughput under heavy concurrency.
- **Owner overwrite bypasses history.** `overwrite` (owner only) replaces the current version in place without a new version. The previous content of that version is not kept, but an `overwrite` audit row records who did it and the old and new sha256, so the trace required by TZ §1.2 stays. It exists for hot fixes and is not available to agents.
- **Quotas are global.** Per-document size, versions per document, writes per author per day and total disk, set in `settings.json`; no per-area quotas (TZ §3.12 mentions them).
- **The sandbox is Linux-only.** It needs `bwrap` and `systemd-run`; without them, runs are refused.
- **Messages are in Russian.** Error messages, hints, the brief and CLI text output are in Russian; JSON keys and structure are English.
- **No scheduled jobs.** Backups, `maintain`, drift checks and anomaly scans run when called; there is no built-in scheduler and no restore command (restoring is unpacking the archive).
