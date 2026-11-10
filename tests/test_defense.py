"""Защита от хитростей агентов: предложения, доказательства, прогоны, аномалии."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from artek_brain import (
    AnomalyDetector,
    EvidenceError,
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

    new = service.proposals.submit(
        token["agent"], "docs/a.md", "# A v2\n", "новая версия", base_version=1
    )
    service.proposals.approve(token["human"], new.path)
    assert service.brain.read(token["owner"], "approved/docs/a.md").version == 2


def test_reject_proposal(service: Service) -> None:
    token = tokens(service)
    proposal = service.proposals.submit(
        token["agent"], "docs/b.md", "# B\n", "предлагаю"
    )
    rejected = service.proposals.reject(token["human"], proposal.path, reason="нет")
    assert rejected.status == "rejected"
    assert service.proposals.list(token["human"], status="rejected")


# ----------------------------------------------------------------------
# Доказательства
# ----------------------------------------------------------------------
def _claimed_task(service: Service, area: list[str], key: str):
    task = service.board.create(
        f"Задача {key}", "", area=area, author="agent-1", key=key
    )
    claim = service.board.claim(task.id, "agent-1", revision=task.revision)
    return task, claim


def test_fake_and_missing_evidence_are_rejected(service: Service) -> None:
    token = tokens(service)
    task, claim = _claimed_task(service, ["docs"], "DOC")
    with pytest.raises(EvidenceError):
        service.board.submit(
            task.id,
            "agent-1",
            evidence=[
                {
                    "type": "doc_version",
                    "path": "docs/a.md",
                    "version": 99,
                    "sha256": "0" * 64,
                }
            ],
            revision=claim.revision,
            token=claim.token,
        )
    with pytest.raises(EvidenceError):
        service.board.submit(
            task.id,
            "agent-1",
            evidence=[{"type": "test_run", "run_id": 999}],
            revision=claim.revision,
            token=claim.token,
        )
    with pytest.raises(EvidenceError):
        service.board.submit(
            task.id,
            "agent-1",
            evidence=[{"type": "audit_ref", "audit_id": 999}],
            revision=claim.revision,
            token=claim.token,
        )


def test_valid_doc_version_evidence_is_accepted(service: Service) -> None:
    token = tokens(service)
    service.brain.write(token["agent"], "docs/a.md", "# A\n", "создал")
    info = service.store.history("docs/a.md", limit=1)[0]
    task, claim = _claimed_task(service, ["docs"], "DOC")
    review = service.board.submit(
        task.id,
        "agent-1",
        evidence=[
            {
                "type": "doc_version",
                "path": "docs/a.md",
                "version": info.n,
                "sha256": info.sha256,
            }
        ],
        revision=claim.revision,
        token=claim.token,
    )
    assert review.status == "review"
    assert review.evidence_mismatch is False


def test_free_text_is_rejected_for_critical_area(service: Service) -> None:
    _claimed_task(service, ["rules"], "RULE")
    task, claim = _claimed_task(service, ["rules"], "RUL2")
    with pytest.raises(EvidenceError) as error:
        service.board.submit(
            task.id,
            "agent-1",
            evidence="тесты прошли, всё хорошо",
            revision=claim.revision,
            token=claim.token,
        )
    assert "критичной" in str(error.value)


def test_claimed_read_must_exist_in_audit(service: Service) -> None:
    task, claim = _claimed_task(service, ["docs"], "DOC")
    review = service.board.submit(
        task.id,
        "agent-1",
        evidence="прочитал docs/never-read.md",
        revision=claim.revision,
        token=claim.token,
    )
    assert review.status == "review"
    assert review.evidence_mismatch is True
    assert review.evidence_report["mismatches"]


def test_file_hash_evidence(service: Service, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    target = repo / "src" / "a.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('ok')\n", encoding="utf-8")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    task, claim = _claimed_task(service, ["src"], "SRC")
    review = service.board.submit(
        task.id,
        "agent-1",
        evidence=[{"type": "file_hash", "path": "src/a.py", "sha256": sha}],
        revision=claim.revision,
        token=claim.token,
    )
    assert review.status == "review"

    task2, claim2 = _claimed_task(service, ["src"], "SR2")
    with pytest.raises(EvidenceError):
        service.board.submit(
            task2.id,
            "agent-1",
            evidence=[
                {"type": "file_hash", "path": "src/a.py", "sha256": "f" * 64}
            ],
            revision=claim2.revision,
            token=claim2.token,
        )


# ----------------------------------------------------------------------
# Прогоны и скрытые проверки
# ----------------------------------------------------------------------
def test_runner_records_real_run_and_seed_fraction(
    service: Service, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    record = service.runs.run(
        "agent-1",
        command=python_command("import sys; sys.exit(0)"),
        seeds=2,
        workdir=repo,
        timeout=10,
    )
    assert record.status == "pass"
    assert record.pass_rate == 1.0
    assert record.system is True
    assert record.code_sha

    fraction = service.runs.run(
        "agent-1",
        command=python_command(
            "import os,sys; sys.exit(0 if os.environ['BRAIN_SEED']=='0' else 1)"
        ),
        seeds=2,
        workdir=repo,
        timeout=10,
    )
    assert fraction.pass_rate == 0.5
    assert fraction.status == "partial"


def test_test_run_evidence_must_exist_and_pass(service: Service) -> None:
    token = tokens(service)
    repo = service.root.parent / "repo"
    good = service.runs.run(
        "agent-1", command=python_command("import sys; sys.exit(0)"), workdir=repo
    )
    task, claim = _claimed_task(service, ["docs"], "DOC")
    review = service.board.submit(
        task.id,
        "agent-1",
        evidence=[{"type": "test_run", "run_id": good.id}],
        revision=claim.revision,
        token=claim.token,
    )
    assert review.status == "review"

    bad = service.runs.run(
        "agent-1", command=python_command("import sys; sys.exit(1)"), workdir=repo
    )
    task2, claim2 = _claimed_task(service, ["docs"], "DO2")
    with pytest.raises(EvidenceError):
        service.board.submit(
            task2.id,
            "agent-1",
            evidence=[{"type": "test_run", "run_id": bad.id}],
            revision=claim2.revision,
            token=claim2.token,
        )


def test_hidden_checks_are_run_but_content_not_leaked(
    service: Service, tmp_path: Path
) -> None:
    hidden = service.root / "hidden"
    hidden.mkdir(parents=True)
    (hidden / "secret.txt").write_text("SECRET_HIDDEN_CONTENT", encoding="utf-8")
    repo = tmp_path / "repo"
    hidden_command = python_command(
        "import pathlib; print(pathlib.Path('_hidden/secret.txt').read_text())"
    )
    record = service.runs.run(
        "agent-1",
        command=python_command("import sys; sys.exit(0)"),
        hidden_command=hidden_command,
        workdir=repo,
    )
    assert record.hidden_total == 1
    assert record.hidden_passed == 1
    assert record.status == "pass"
    assert "SECRET_HIDDEN_CONTENT" not in record.output_tail

    failing = service.runs.run(
        "agent-1",
        command=python_command("import sys; sys.exit(0)"),
        hidden_command=python_command(
            "import pathlib,sys; pathlib.Path('_hidden/secret.txt').read_text(); sys.exit(1)"
        ),
        workdir=repo,
    )
    assert failing.status == "fail"
    assert failing.hidden_passed == 0
    assert "SECRET_HIDDEN_CONTENT" not in failing.output_tail


# ----------------------------------------------------------------------
# Аномалии и приостановка токена
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

    auditor = str(service.access.create_token("auditor", "auditor", [("", "read")]))
    entries = service.brain.audit(auditor, action="write")
    hidden_entries = [entry for entry in entries if entry.detail.get("hidden")]
    assert hidden_entries
    assert all(entry.path is None for entry in hidden_entries)
