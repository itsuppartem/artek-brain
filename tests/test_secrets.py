"""Сканер секретов: отказ при записи и чистка журнала аудита."""

from __future__ import annotations

import json

import pytest

from artek_brain import SecretDetectedError, Store
from artek_brain import secrets


def test_aws_key_rejected_and_not_leaked(store: Store) -> None:
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    content = f"# Ключ\n\naws = {key}\n"
    with pytest.raises(SecretDetectedError) as error:
        store.write("secret.md", content, "alice", "плохо")
    assert error.value.secret_type == "aws_access_key"
    assert error.value.line == 3
    assert key not in str(error.value)

    assert store.stats()["versions"] == 0
    rows = store.audit(limit=20)
    assert rows
    assert rows[0].action == "write"
    dumped = json.dumps([row.as_dict() for row in rows], ensure_ascii=False)
    assert key not in dumped


def test_password_private_key_and_github_token(tmp_path) -> None:
    assert secrets.scan("password=hunter2")[0].kind == "password"
    assert secrets.scan("token: ghp_" + "a" * 30)[0].kind == "github_token"
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert secrets.scan(pem)[0].kind == "private_key"
    assert secrets.scan("# просто текст\nбез секретов") == []


def test_reason_is_redacted_in_audit(store: Store) -> None:
    secret = "password=s3cr3t-value"
    store.write("clean.md", "# ok\n", "alice", f"причина: {secret}")
    rows = store.audit(limit=10)
    dumped = json.dumps([row.as_dict() for row in rows], ensure_ascii=False)
    assert "s3cr3t-value" not in dumped
    assert "[REDACTED:password]" in dumped


def test_redact_and_sanitize() -> None:
    text = "ключ AKIA" + "ABCDEFGHIJKLMNOP"
    assert "AKIA" not in secrets.redact(text)
    assert "REDACTED" in secrets.redact(text)
    payload = {"note": text, "items": ["password=abc123"]}
    cleaned = secrets.sanitize_json(payload)
    assert "ABCDEFGHIJKLMNOP" not in cleaned
    assert "abc123" not in cleaned
