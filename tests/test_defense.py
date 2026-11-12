"""Защита от хитростей агентов: предложения, доказательства, прогоны, аномалии."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from artek_brain import (
    AnomalyDetector,
    PermissionDenied,
    ValidationError,
)
from artek_brain.service import Service


@pytest.fixture()
def service(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    instance = Service(tmp_path / "brain", repo_root=repo)
    try:
        yield instance
    finally:
        instance.close()


def tokens(service: Service) -> dict[str, str]:
    """Набор токенов: owner, агент, ревьюер."""
    access = service.access
    return {
        "owner": str(access.create_token("root", "owner")),
        "agent": str(access.create_token("agent-1", "agent", [("", "write")])),
        "human": str(access.create_token("reviewer", "human", [("", "write")])),
    }


def python_command(code: str) -> str:
    return f'{sys.executable} -c "{code}"'


# ----------------------------------------------------------------------
# Предложения и утверждённое знание
# ----------------------------------------------------------------------
def test_proposal_approve_records_approver_and_hash(service: Service) -> None:
    token = tokens(service)
    proposal = service.proposals.submit(
        token["agent"], "docs/a.md", "# A\n\nтекст\n", "предлагаю"
    )
    assert proposal.path == "proposals/agent-1/docs/a.md"
    assert proposal.target == "approved/docs/a.md"
    assert proposal.status == "pending"

    approved = service.proposals.approve(token["human"], proposal.path)
    assert approved["proposal"]["status"] == "approved"
    assert approved["approval"]["approved_by"] == "reviewer"
    assert approved["approval"]["target_path"] == "approved/docs/a.md"

    read = service.brain.read(token["owner"], "approved/docs/a.md")
    assert read.content == "# A\n\nтекст\n"
    info = service.store.history("approved/docs/a.md", limit=1)[0]
    assert approved["approval"]["target_sha256"] == info.sha256


def test_author_cannot_approve_own_proposal(service: Service) -> None:
    token = tokens(service)
    proposal = service.proposals.submit(
        token["agent"], "docs/a.md", "# A\n", "предлагаю"
    )
    with pytest.raises(PermissionDenied):
        service.proposals.approve(token["agent"], proposal.path)

    same_name_human = str(
        service.access.create_token("agent-1", "human", [("", "write")])
    )
    with pytest.raises(PermissionDenied) as error:
        service.proposals.approve(same_name_human, proposal.path)
    assert "автор" in str(error.value)


def test_approved_cannot_be_changed_silently(service: Service) -> None:
    token = tokens(service)
    proposal = service.proposals.submit(
        token["agent"], "docs/a.md", "# A\n", "предлагаю"
    )
    service.proposals.approve(token["human"], proposal.path)

    with pytest.raises(PermissionDenied) as error:
        service.brain.write(
            token["owner"],
            "approved/docs/a.md",
            "# подмена\n",
            "тихая правка",
            base_version=1,
        )
    assert "утвержден" in str(error.value).lower()

    with pytest.raises(PermissionDenied):
        service.brain.write(
            token["agent"], "approved/docs/a.md", "# агент\n", "в обход"
        )

    new = service.proposals.submit(
        token["agent"], "docs/a.md", "# A v2\n", "новая версия", base_version=1
    )
    service.proposals.approve(token["human"], new.path)
    assert service.brain.read(token["owner"], "approved/docs/a.md").version == 2


def test_agent_cannot_write_to_foreign_proposal(service: Service) -> None:
    token = tokens(service)
    service.access.create_token("agent-2", "agent", [("", "write")])
    with pytest.raises(PermissionDenied):
        service.proposals.submit(
            token["agent"], "proposals/agent-2/docs/a.md", "# A\n", "чужое"
        )


def test_reject_proposal(service: Service) -> None:
    token = tokens(service)
    proposal = service.proposals.submit(
        token["agent"], "docs/b.md", "# B\n", "предлагаю"
    )
    rejected = service.proposals.reject(token["human"], proposal.path, reason="нет")
    assert rejected.status == "rejected"
    assert service.proposals.list(token["human"], status="rejected")


# ----------------------------------------------------------------------
# Аномалии
# ----------------------------------------------------------------------
def test_version_spike_creates_incident_and_suspends(service: Service) -> None:
    token = tokens(service)
    spiky = str(service.access.create_token("spiky", "agent", [("", "write")]))
    for index in range(4):
        service.brain.write(spiky, f"docs/{index}.md", "# x\n", "быстро")
    detector = AnomalyDetector(
        service.store,
        access=service.access,
        config={
            "window_minutes": 60,
            "version_spike": 3,
            "mass_edit_docs": 100,
            "auto_suspend": True,
        },
    )
    incidents = detector.scan()
    assert any(item.kind == "version_spike" and item.actor == "spiky" for item in incidents)
    assert any(item.suspended for item in incidents)
    with pytest.raises(PermissionDenied) as error:
        service.access.verify(spiky)
    assert "приостановлен" in str(error.value)
    assert service.access.resume_name("spiky") == 1
    assert service.access.verify(spiky).name == "spiky"

    stored = detector.incidents(actor="spiky")
    assert stored and service.store.read(stored[0].path).content


def test_mass_edits_size_growth_and_rollbacks_are_detected(
    service: Service,
) -> None:
    bulk = str(service.access.create_token("bulk", "agent", [("", "write")]))
    for index in range(4):
        service.brain.write(bulk, f"docs/{index}.md", "# x\n", "массово")
    service.brain.write(bulk, "docs/big.md", "a" * 200, "маленький")
    service.brain.write(
        bulk, "docs/big.md", "a" * 200_000, "большой", base_version=1
    )
    service.brain.write(bulk, "docs/roll.md", "# v1\n", "первая")
    service.brain.write(
        bulk, "docs/roll.md", "# v2\n", "вторая", base_version=1
    )
    for _ in range(3):
        service.brain.rollback_version(bulk, "docs/roll.md", 1, "откат")

    detector = AnomalyDetector(
        service.store,
        access=service.access,
        config={
            "window_minutes": 60,
            "version_spike": 100,
            "mass_edit_docs": 3,
            "rollback_count": 3,
            "size_growth_factor": 5,
            "size_growth_min_bytes": 100_000,
            "auto_suspend": False,
        },
    )
    kinds = {item.kind for item in detector.scan()}
    assert {"mass_edits", "size_growth", "repeated_rollbacks"} <= kinds


def test_protected_path_probing_and_hidden_reads_are_detected(
    service: Service,
) -> None:
    attacker = str(service.access.create_token("attacker", "agent", [("", "write")]))
    peeker = str(service.access.create_token("peeker", "agent", [("", "write")]))
    for _ in range(3):
        with pytest.raises(PermissionDenied):
            service.brain.write(attacker, "rules/x.md", "# x\n", "перебор")
    with pytest.raises(PermissionDenied):
        service.brain.read(peeker, "hidden/secret.md")

    detector = AnomalyDetector(
        service.store,
        access=service.access,
        config={
            "window_minutes": 60,
            "protected_attempts": 3,
            "hidden_reads": 1,
            "version_spike": 100,
            "mass_edit_docs": 100,
            "auto_suspend": True,
        },
    )
    incidents = detector.scan()
    kinds = {item.kind for item in incidents}
    assert "protected_write_attempts" in kinds
    assert "hidden_reads" in kinds
    with pytest.raises(PermissionDenied):
        service.access.verify(attacker)
    with pytest.raises(PermissionDenied):
        service.access.verify(peeker)


# ----------------------------------------------------------------------
# Обход защищённых путей и hidden в фасаде
# ----------------------------------------------------------------------
def test_protected_paths_cannot_be_bypassed(service: Service) -> None:
    token = tokens(service)
    paths = [
        "RULES/x.md",
        "Rules/x.md",
        "ｒｕｌｅｓ/x.md",
        "rul\u0435s/x.md",
        "scoring/x.md",
        "hidden/x.md",
        "approved/x.md",
        "owner/x.md",
    ]
    for path in paths:
        with pytest.raises(PermissionDenied):
            service.brain.write(token["agent"], path, "# x\n", "обход")

    malformed = [
        "docs/../rules/x.md",
        "rules//x.md",
        "/rules/x.md",
        "docs\\rules\\x.md",
        "rules/./x.md",
    ]
    for path in malformed:
        with pytest.raises(ValidationError):
            service.brain.write(token["agent"], path, "# x\n", "обход")

    with service.store._lock:
        stored = [
            row["path"]
            for row in service.store._conn.execute("SELECT path FROM documents")
        ]
    assert not any("rules" in path.casefold() for path in stored)


def test_hidden_is_invisible_to_agent_tokens(service: Service) -> None:
    token = tokens(service)
    service.brain.write(token["owner"], "hidden/secret.md", "# hidden\n", "owner")
    service.brain.write(token["owner"], "docs/open.md", "# open\n", "owner")

    with pytest.raises(PermissionDenied):
        service.brain.read(token["agent"], "hidden/secret.md")

    listing = [node.path for node in service.brain.ls(token["agent"], "")]
    assert "hidden" not in listing
    assert "docs" in listing

    found = service.brain.search(token["agent"], "hidden")
    assert all("hidden" not in item.path for item in found.items)

    changes = service.brain.changes_since(token["agent"])
    assert all(
        item.path is None or "hidden" not in item.path for item in changes.items
    )

    auditor = str(service.access.create_token("auditor", "auditor", [("", "read")]))
    entries = service.brain.audit(auditor, action="write")
    hidden_entries = [entry for entry in entries if entry.detail.get("hidden")]
    assert hidden_entries
    assert all(entry.path is None for entry in hidden_entries)
