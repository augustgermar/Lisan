from __future__ import annotations

import json

import pytest

from lisan.tools.mail import send_email
from lisan.tools.execution_tools import _send_email_tool


def test_dry_run_uses_config_and_resolves_recipients():
    result = send_email(
        subject="Test",
        body="Hello",
        recipients=["Alice@Example.Com", "alice@example.com", "bob"],
        config={"mail": {"sender": "ops@example.com", "relay": "mail.example.com", "port": 25, "default_domain": "example.com"}},
        dry_run=True,
    )
    assert result["sender"] == "ops@example.com"
    assert result["relay"] == "mail.example.com"
    assert result["recipients"] == ["alice@example.com", "bob@example.com"]
    assert result["dry_run"] is True


def _test_config():
    return {"mail": {"sender": "ops@example.com", "relay": "localhost", "port": 25, "default_domain": "example.com"}}


def test_subject_header_injection_is_collapsed():
    result = send_email(
        subject="Hello\r\nX-Test: rejected",
        body="Hello",
        recipients=["owner@example.com"],
        config=_test_config(),
        dry_run=True,
    )
    assert "\r" not in result["accepted_response"]


def test_missing_sender_raises():
    with pytest.raises(ValueError, match="no sender address configured"):
        send_email(
            subject="Test", body="Hello", recipients=["a@b.com"],
            config={"mail": {}}, dry_run=True,
        )


def test_tool_returns_json_error_for_invalid_recipient():
    result = json.loads(_send_email_tool(
        "Test", "Body", ["bad\naddress"], config=_test_config(), dry_run=True,
    ))
    assert result["ok"] is False
    assert "invalid email address" in result["error"]
