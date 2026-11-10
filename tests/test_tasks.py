"""Доска задач: claim, аренда, области, зависимости, ревью, inbox, handoff."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from artek_brain import (
    ActionError,
    AreaConflictError,
    Claim,
    ClaimLimitError,
    DependencyCycleError,
    DependencyError,
    LeaseError,
    ReviewPermissionError,
    Store,
    Task,
    TaskBoard,
    TaskError,
    TaskRevisionConflictError,
    ValidationError,
)


class FakeClock:
    """Управляемые часы для проверки просрочки аренды."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now = self.now + timedelta(**kwargs)


@pytest.fixture()
def board(store: Store) -> TaskBoard:
    """Доска задач на чистом хранилище."""
    return TaskBoard(store)


def make_task(
    board: TaskBoard,
    key: str = "CORE",
    *,
    area: list[str] | None = None,
    author: str = "alice",
    depends_on: list[str] | None = None,
    priority: int = 2,
) -> Task:
    """Создать задачу с предсказуемым ключом."""
    return board.create(
        f"Задача {key}",
        f"Тело {key}",
        area=area or [f"src/{key.lower()}"],
        author=author,
        key=key,
        depends_on=depends_on,
        priority=priority,
    )


def test_create_list_show_and_id_format(board: TaskBoard) -> None:
    task = board.create(
        "  Первая задача  ",
        "# Описание\n",
        area=["src/artek_brain", "src/artek_brain/"],
        author="alice",
        priority=1,
    )
    assert re.fullmatch(r"[A-Z][A-Z0-9]*-\d{3}", task.id)
    assert task.id == "SRC-001"
    assert task.title == "Первая задача"
    assert task.status == "todo"
    assert task.revision == 1
    assert task.depends_on == []
    assert task.area == ["src/artek_brain"]
    assert board.show(task.id) == task

    other = board.create("Другая", "", area=["docs"], author="bob")
    assert other.id == "DOCS-001"
    assert [item.id for item in board.list()] == ["SRC-001", "DOCS-001"]
    assert [item.id for item in board.list(status="todo")] == ["SRC-001", "DOCS-001"]
    assert board.list(assignee="bob") == []
    assert [item.id for item in board.list(area="src/artek_brain/tasks.py")] == [
        "SRC-001"
    ]
    assert [item.id for item in board.list(area="tests")] == []
    assert [item.id for item in board.list(limit=1, offset=1)] == ["DOCS-001"]


def test_claim_heartbeat_submit_accept(board: TaskBoard) -> None:
    task = make_task(board)
    claim = board.claim(task.id, "alice", revision=task.revision)
    assert isinstance(claim, Claim)
    assert claim.status == "claimed"
    assert claim.assignee == "alice"
    assert claim.claim_token == claim.token
    assert claim.revision == 2

    renewed = board.heartbeat(claim.token, minutes=10)
    assert renewed.revision == 3
    assert renewed.token == claim.token

    review = board.submit(
        task.id,
        "alice",
        evidence="uv run pytest -q: 10 passed",
        revision=renewed.revision,
        token=claim.token,
    )
    assert review.status == "review"
    assert review.evidence == "uv run pytest -q: 10 passed"
    assert review.claim_token is None

    with pytest.raises(ReviewPermissionError):
        board.accept(task.id, "bob", revision=review.revision)

    board.grant_role("bob", "reviewer")
    done = board.accept(task.id, "bob", revision=review.revision, note="ок")
    assert done.status == "done"
    assert done.note == "ок"
    assert board.list(status="done")[0].id == task.id


def test_submit_requires_evidence(board: TaskBoard) -> None:
    task = make_task(board)
    claim = board.claim(task.id, "alice", revision=task.revision)
    with pytest.raises(ValidationError):
        board.submit(task.id, "alice", evidence="   ", revision=claim.revision)
    with pytest.raises(ValidationError):
        board.submit(task.id, "alice", evidence=123, revision=claim.revision)  # type: ignore[arg-type]


def test_author_cannot_accept_own_task_even_as_reviewer(board: TaskBoard) -> None:
    task = make_task(board, author="alice")
    claim = board.claim(task.id, "alice", revision=task.revision)
    review = board.submit(
        task.id, "alice", evidence="тесты зелёные", revision=claim.revision
    )
    board.grant_role("alice", "reviewer")
    with pytest.raises(ReviewPermissionError) as error:
        board.accept(task.id, "alice", revision=review.revision)
    assert "не может" in str(error.value)

    with pytest.raises(ReviewPermissionError):
        board.rework(task.id, "alice", revision=review.revision, reason="сам себе")


def test_revision_conflict_returns_current_state(board: TaskBoard) -> None:
    task = make_task(board)
    claim = board.claim(task.id, "alice", revision=task.revision)
    with pytest.raises(TaskRevisionConflictError) as error:
        board.release(task.id, "alice", revision=task.revision)
    current = error.value.current
    assert isinstance(current, Task)
    assert current.revision == claim.revision
    assert current.status == "claimed"
    assert current.assignee == "alice"
    assert str(claim.revision) in str(error.value)
    assert str(task.revision) in str(error.value)

    released = board.release(task.id, "alice", revision=claim.revision)
    assert released.status == "todo"
    assert released.revision == claim.revision + 1

    # Освобождение по одному токену: токен подтверждает право на аренду.
    second = board.claim(task.id, "alice", revision=released.revision)
    by_token = board.release(task.id, token=second.token)
    assert by_token.status == "todo"
    assert by_token.assignee is None


def test_concurrent_claim_only_one_wins(store: Store) -> None:
    board = TaskBoard(store)
    task = make_task(board, key="RACE", area=["src/race"])
    barrier = threading.Barrier(2)
    outcomes: list[Any] = []
    guard = threading.Lock()

    def worker(agent: str) -> None:
        barrier.wait()
        try:
            result = board.claim(task.id, agent, revision=task.revision)
        except Exception as exc:  # noqa: BLE001 - тест собирает любые отказы
            with guard:
                outcomes.append(exc)
        else:
            with guard:
                outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(f"agent-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    winners = [item for item in outcomes if isinstance(item, Claim)]
    losers = [item for item in outcomes if isinstance(item, Exception)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], TaskError)
    stored = board.show(task.id)
    assert stored.status == "claimed"
    assert stored.assignee == winners[0].assignee
    assert stored.claim_token == winners[0].token


def test_expired_lease_is_released_lazily(store: Store) -> None:
    clock = FakeClock()
    board = TaskBoard(store, clock=clock)
    task = make_task(board, key="LEASE", area=["src/lease"])
    claim = board.claim(task.id, "alice", revision=task.revision, minutes=5)
    assert board.show(task.id).status == "claimed"

    clock.advance(minutes=6)

    shown = board.show(task.id)
    assert shown.status == "todo"
    assert shown.assignee is None
    assert shown.claim_token is None
    assert shown.revision == claim.revision + 1

    with pytest.raises(LeaseError):
        board.heartbeat(claim.token)

    again = board.claim(task.id, "bob", revision=shown.revision)
    assert again.assignee == "bob"

    # Просроченная аренда не блокирует пересекающуюся область.
    overlapping = make_task(board, key="OTHER", area=["src/lease/deep"])
    clock.advance(minutes=60)
    second = board.claim(overlapping.id, "carol", revision=overlapping.revision)
    assert second.status == "claimed"


def test_area_overlap_blocks_claim_and_names_busy_agent(board: TaskBoard) -> None:
    first = make_task(board, key="CORE", area=["src/artek_brain"])
    overlapping = make_task(board, key="DOCS", area=["src/artek_brain/tasks.py"])
    free = make_task(board, key="TESTS", area=["tests"])

    claim = board.claim(first.id, "alice", revision=first.revision)

    with pytest.raises(AreaConflictError) as error:
        board.claim(overlapping.id, "bob", revision=overlapping.revision)
    message = str(error.value)
    assert first.id in message
    assert "alice" in message
    assert error.value.busy_task.id == first.id
    assert error.value.overlap == ("src/artek_brain/tasks.py", "src/artek_brain")

    # Свободная область берётся без проблем.
    board.claim(free.id, "bob", revision=free.revision)

    # После освобождения области задача снова доступна.
    board.release(first.id, "alice", revision=claim.revision)
    taken = board.claim(
        overlapping.id, "bob", revision=board.show(overlapping.id).revision
    )
    assert taken.assignee == "bob"


def test_same_agent_two_claims_in_overlapping_area_conflict(board: TaskBoard) -> None:
    first = make_task(board, key="ONE", area=["src/shared"])
    second = make_task(board, key="TWO", area=["src/shared/inner"])
    board.claim(first.id, "alice", revision=first.revision)
    with pytest.raises(AreaConflictError):
        board.claim(second.id, "alice", revision=second.revision)


def test_dependencies_must_be_done(board: TaskBoard) -> None:
    base = make_task(board, key="BASE", area=["src/base"], author="bob")
    dependent = make_task(
        board,
        key="DEP",
        area=["src/dep"],
        depends_on=[base.id],
    )

    with pytest.raises(DependencyError) as error:
        board.claim(dependent.id, "alice", revision=dependent.revision)
    assert base.id in str(error.value)
    assert error.value.pending == [f"{base.id} (todo)"]

    base_claim = board.claim(base.id, "bob", revision=base.revision)
    base_review = board.submit(
        base.id, "bob", evidence="готово", revision=base_claim.revision
    )
    board.grant_role("carol", "owner")
    board.accept(base.id, "carol", revision=base_review.revision)

    taken = board.claim(dependent.id, "alice", revision=dependent.revision)
    assert taken.status == "claimed"

    forward = make_task(
        board, key="FWD", area=["src/fwd"], depends_on=["MISSING-001"]
    )
    with pytest.raises(DependencyError) as missing:
        board.claim(forward.id, "dave", revision=forward.revision)
    assert "MISSING-001 (missing)" in str(missing.value)


def test_dependency_cycle_is_rejected(board: TaskBoard) -> None:
    first = make_task(board, key="CYCLE", area=["src/cycle"], depends_on=["CYCLE-002"])
    with pytest.raises(DependencyCycleError) as error:
        make_task(board, key="CYCLE", area=["src/cycle2"], depends_on=[first.id])
    assert "CYCLE-001" in str(error.value)
    assert "CYCLE-002" in str(error.value)

    with pytest.raises(DependencyCycleError):
        make_task(board, key="SELF", area=["src/self"], depends_on=["SELF-001"])


def test_claim_quota_per_agent(store: Store) -> None:
    board = TaskBoard(store, max_claims=2)
    tasks = [
        make_task(board, key=f"Q{i}", area=[f"src/q{i}"]) for i in range(3)
    ]
    for task in tasks[:2]:
        board.claim(task.id, "alice", revision=task.revision)
    with pytest.raises(ClaimLimitError) as error:
        board.claim(tasks[2].id, "alice", revision=tasks[2].revision)
    assert error.value.limit == 2
    assert error.value.active == 2
    # Другой агент не ограничен чужой квотой.
    board.claim(tasks[2].id, "bob", revision=tasks[2].revision)


def test_rework_returns_task_to_pool(board: TaskBoard) -> None:
    task = make_task(board, author="alice")
    claim = board.claim(task.id, "alice", revision=task.revision)
    review = board.submit(
        task.id, "alice", evidence="тест: ok", revision=claim.revision
    )
    board.grant_role("bob", "reviewer")
    back = board.rework(
        task.id, "bob", revision=review.revision, reason="нужны тесты на отказ"
    )
    assert back.status == "rework"
    assert back.assignee is None
    assert back.note == "нужны тесты на отказ"

    taken = board.claim(task.id, "alice", revision=back.revision)
    assert taken.status == "claimed"


def test_block_and_cancel(board: TaskBoard) -> None:
    task = make_task(board)
    blocked = board.block(task.id, "alice", revision=task.revision, reason="нет доступа")
    assert blocked.status == "blocked"
    assert blocked.note == "нет доступа"
    with pytest.raises(ActionError):
        board.claim(task.id, "bob", revision=blocked.revision)

    cancelled = board.cancel(
        task.id, "alice", revision=blocked.revision, reason="устарела"
    )
    assert cancelled.status == "cancelled"
    with pytest.raises(ActionError):
        board.cancel(task.id, "alice", revision=cancelled.revision)
    assert board.list(status="cancelled")[0].id == task.id


def test_claim_rejects_wrong_status(board: TaskBoard) -> None:
    task = make_task(board)
    claim = board.claim(task.id, "alice", revision=task.revision)
    with pytest.raises(ActionError):
        board.claim(task.id, "bob", revision=claim.revision)


def test_presence_heartbeat_and_ttl(store: Store) -> None:
    clock = FakeClock()
    board = TaskBoard(store, clock=clock)
    board.presence("alice", "пишет tasks.py")
    board.agent_heartbeat("bob", "ждёт ревью")
    online = board.agents_online()
    assert [item.name for item in online] == ["alice", "bob"]
    assert online[0].activity == "пишет tasks.py"
    assert all(item.online for item in online)

    clock.advance(minutes=10)
    assert board.agents_online(ttl_seconds=300) == []
    board.presence("bob", "снова тут")
    assert [item.name for item in board.agents_online()] == ["bob"]


def test_handoff_is_stored_as_document_and_who_knows(store: Store, board: TaskBoard) -> None:
    result = board.handoff(
        "CORE-001",
        "сделал API claim",
        "проверить пересечения",
        "не пушить",
        author="alice",
    )
    assert result.path == "handoffs/CORE-001.md"
    document = store.read("handoffs/CORE-001.md")
    assert "# Handoff: CORE-001" in document.content
    assert "сделал API claim" in document.content
    assert "проверить пересечения" in document.content
    assert "не пушить" in document.content

    who = board.who_knows("handoffs/CORE-001.md")
    assert who is not None
    assert who.author == "alice"
    assert who == "alice"
    assert who.path == "handoffs/CORE-001.md"

    prefix = board.who_knows("handoffs")
    assert prefix is not None
    assert prefix.path == "handoffs/CORE-001.md"
    assert board.who_knows("docs") is None

    # Повторный handoff создаёт новую версию, а не падает на base_version.
    second = board.handoff(
        "CORE-001", "сделал API", "осталось ревью", True, author="alice"
    )
    assert second.version == 2
    assert store.read("handoffs/CORE-001.md").version == 2


def test_inbox_send_and_mark_read(board: TaskBoard) -> None:
    message = board.send("bob", "Вопрос по claim", "Как работает аренда?", sender="alice")
    assert message.read is False
    assert board.inbox("bob") == [message]
    assert board.inbox("bob", unread=True) == [message]
    assert board.inbox("carol") == []

    assert board.mark_read(message.id, "bob") == 1
    assert board.inbox("bob", unread=True) == []
    assert board.inbox("bob")[0].read is True
    assert board.mark_read(message.id, "bob") == 0

    with pytest.raises(TaskError):
        board.mark_read(message.id, "carol")


def test_brief_is_short_and_deterministic(store: Store) -> None:
    clock = FakeClock()
    board = TaskBoard(store, clock=clock)
    for index in range(30):
        board.create(
            "Очень длинное название задачи " + "x" * 60 + f" {index}",
            "тело",
            area=[f"src/area{index}"],
            author="alice",
            priority=index % 5,
        )
    busy = board.list()[0]
    board.claim(busy.id, "alice", revision=busy.revision)
    board.handoff(busy.id, "сделано", "осталось", author="alice")
    board.send("bob", "привет", "тело", sender="alice")

    text = board.brief("bob")
    assert len(text) <= 2000
    assert "Бриф для bob" in text
    assert "Открытые задачи: 30" in text
    assert "Занятые области: 1" in text
    assert "Inbox: 1 непрочитанных" in text
    assert text == board.brief("bob")

    # Непрочитанное письмо не мешает уложиться в маленький лимит.
    assert len(board.brief("bob", limit=300)) <= 300
