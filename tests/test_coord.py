"""Координация без доски задач: перенос старой базы, письма, бриф."""

from __future__ import annotations

from pathlib import Path

from artek_brain.coord import Coordination
from artek_brain.store import Store

_LEGACY = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT);
INSERT INTO tasks VALUES ('DOCS-001', 'старая задача');
CREATE TABLE task_roles (actor TEXT, role TEXT);
CREATE TABLE task_handoffs (task_id TEXT PRIMARY KEY, path TEXT);
CREATE TABLE runs (id INTEGER PRIMARY KEY, task_id TEXT);
CREATE TABLE task_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL,
    recipient TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
    ts TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0, read_at TEXT
);
CREATE INDEX idx_task_messages_recipient ON task_messages(recipient, read, id);
INSERT INTO task_messages (sender, recipient, subject, body, ts)
    VALUES ('owner', 'agent-1', 'привет', 'текст', '2026-11-12T10:00:00+00:00');
CREATE TABLE task_agents (name TEXT PRIMARY KEY, last_seen TEXT NOT NULL, activity TEXT);
INSERT INTO task_agents VALUES ('agent-1', '2026-11-12T10:00:00+00:00', 'работал');
"""


def _tables(store: Store) -> set[str]:
    rows = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row[0] for row in rows}


def test_legacy_task_tables_are_migrated(tmp_path: Path) -> None:
    store = Store(tmp_path / "brain")
    with store._conn:
        store._conn.executescript(_LEGACY)

    coord = Coordination(store)

    tables = _tables(store)
    assert {"messages", "agents"} <= tables
    assert not tables & {"tasks", "task_roles", "task_handoffs", "runs",
                         "task_messages", "task_agents"}
    inbox = coord.inbox("agent-1")
    assert [item.subject for item in inbox] == ["привет"]
    assert coord.mark_read(inbox[0].id, "agent-1") == 1

    # Повторное открытие ничего не ломает.
    Coordination(store)
    assert coord.inbox("agent-1", unread=True) == []
    store.close()


def test_old_process_rows_are_merged(tmp_path: Path) -> None:
    store = Store(tmp_path / "brain")
    coord = Coordination(store)
    coord.send("agent-1", "новое", "текст", sender="owner")
    # Процесс на старом коде заново создал таблицы и записал в них.
    with store._conn:
        store._conn.executescript(_LEGACY)

    Coordination(store)

    subjects = sorted(item.subject for item in coord.inbox("agent-1"))
    assert subjects == ["новое", "привет"]
    assert [item.name for item in coord.agents_online(ttl_seconds=10**9)] == ["agent-1"]
    assert "task_messages" not in _tables(store)
    store.close()


def test_brief_points_to_notes(tmp_path: Path) -> None:
    store = Store(tmp_path / "brain")
    coord = Coordination(store)
    store.write("docs/a.md", "# A\n", "agent-1", "старт")
    coord.presence("agent-1", "читаю backlog")
    coord.send("agent-1", "тема", "текст", sender="owner")

    text = coord.brief("agent-1")

    assert "README.md и backlog.md" in text
    assert "Сейчас работают: 1" in text
    assert "docs/a.md v1" in text
    assert "Inbox: 1 непрочитанных" in text
    assert "задач" not in text.replace("доски задач нет", "")
    store.close()
