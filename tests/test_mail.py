from __future__ import annotations

import json

import pytest

from lisan.tools.mail import send_email
from lisan.tools.execution_tools import _send_email_tool


def test_dry_run_uses_config_and_resolves_recipients():
    result = send_email(
        subject="Test",
        body="Hello",
        recipients=["Robin@Example.Com", "robin@example.com", "bob"],
        config={"mail": {"sender": "ops@example.com", "relay": "mail.example.com", "port": 25, "default_domain": "example.com"}},
        dry_run=True,
    )
    assert result["sender"] == "ops@example.com"
    assert result["relay"] == "mail.example.com"
    assert result["recipients"] == ["robin@example.com", "bob@example.com"]
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


@pytest.fixture()
def sendable(tmp_path, monkeypatch):
    """An adopted intent and outbound allowed: only the approval gate remains."""
    from tests.intent_helpers import adopt_intent

    adopt_intent(tmp_path)
    monkeypatch.delenv("LISAN_NO_OUTBOUND", raising=False)
    monkeypatch.setenv("LISAN_ALLOW_TEST_OUTBOUND", "1")
    return tmp_path


def test_tool_refuses_real_send_without_an_approval_channel(monkeypatch, sendable):
    sent = []
    monkeypatch.setattr("smtplib.SMTP", lambda *a, **k: sent.append(a))
    result = json.loads(_send_email_tool(
        "Test", "Body", ["owner@example.com"], config=_test_config(), vault=sendable,
    ))
    assert result["ok"] is False
    assert "Approval required" in result["error"]
    assert sent == []


def test_tool_refuses_when_owner_denies_and_shows_resolved_recipients(sendable):
    seen = {}

    def deny(name, args):
        seen.update(name=name, **args)
        return False

    result = json.loads(_send_email_tool(
        "Test", "Body", ["Owner"], config=_test_config(), approval_fn=deny, vault=sendable,
    ))
    assert result["ok"] is False
    assert "not granted" in result["error"]
    assert seen["name"] == "send_email"
    assert seen["recipients"] == ["owner@example.com"]


def test_dry_run_never_asks_for_approval():
    def explode(*_):
        raise AssertionError("dry run must not prompt")

    result = json.loads(_send_email_tool(
        "Test", "Body", ["owner@example.com"], config=_test_config(), dry_run=True,
        approval_fn=explode,
    ))
    assert result["ok"] is True and result["dry_run"] is True


def test_approved_send_still_honours_the_outbound_kill_switch(monkeypatch, sendable):
    monkeypatch.setenv("LISAN_NO_OUTBOUND", "1")
    monkeypatch.setattr("smtplib.SMTP", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    result = json.loads(_send_email_tool(
        "Test", "Body", ["owner@example.com"], config=_test_config(),
        approval_fn=lambda *_: True, vault=sendable,
    ))
    assert result["ok"] is False
    assert "LISAN_NO_OUTBOUND" in result["error"]


def test_model_chosen_sender_is_rejected_on_unauthenticated_relay():
    with pytest.raises(ValueError, match="not the configured mail.sender"):
        send_email(subject="T", body="B", recipients=["a@example.com"], config=_test_config(),
                   sender="ceo@example.com", dry_run=True)


def test_allowlisted_sender_is_accepted():
    config = _test_config()
    config["mail"]["allowed_senders"] = ["alerts@example.com"]
    result = send_email(subject="T", body="B", recipients=["a@example.com"], config=config,
                        sender="alerts@example.com", dry_run=True)
    assert result["sender"] == "alerts@example.com"


def test_readable_attachment_is_accepted(tmp_path):
    attachment = tmp_path / "note.txt"
    attachment.write_text("hi", encoding="utf-8")
    result = send_email(subject="T", body="B", recipients=["a@example.com"], config=_test_config(),
                        attachments=[str(attachment)], dry_run=True)
    assert result["attachments"] == ["note.txt"]


def test_an_unadopted_intent_blocks_email_before_anyone_is_asked(tmp_path, monkeypatch):
    monkeypatch.delenv("LISAN_NO_OUTBOUND", raising=False)
    monkeypatch.setenv("LISAN_ALLOW_TEST_OUTBOUND", "1")

    def explode(*_):
        raise AssertionError("must not ask when intent cannot be checked")

    result = json.loads(_send_email_tool(
        "Test", "Body", ["owner@example.com"], config=_test_config(), approval_fn=explode, vault=tmp_path,
    ))
    assert result["ok"] is False and "intent" in result["error"]
