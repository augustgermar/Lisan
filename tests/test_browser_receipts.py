from __future__ import annotations

import pytest

from lisan.tools.approval_receipts import ReceiptError, consume_receipt, issue_receipt
from lisan.tools.browser import browser_action, browser_handoff
from lisan.tools.execution_tools import _browser_tool


def _receipt_dir(monkeypatch, tmp_path):
    path = tmp_path / "receipts"
    monkeypatch.setenv("LISAN_RECEIPT_DIR", str(path))
    return path


def test_forged_absent_and_model_asserted_receipts_are_refused(monkeypatch, tmp_path):
    _receipt_dir(monkeypatch, tmp_path)
    for kwargs in (
        {"target": "Create project", "owner_approved": True},
        {"target": "Create project", "receipt_id": "0" * 64},
    ):
        result = __import__("json").loads(_browser_tool("click", **kwargs))
        assert result["refused"] is True


def test_expired_receipt_is_refused(monkeypatch, tmp_path):
    _receipt_dir(monkeypatch, tmp_path)
    receipt = issue_receipt(tool="browser", action="click", target="Create", arguments={"target": "Create"}, now=100, ttl_seconds=1)
    with pytest.raises(ReceiptError, match="expired"):
        consume_receipt(receipt, tool="browser", action="click", target="Create", arguments={"target": "Create"}, now=102)


def test_receipt_is_single_use(monkeypatch, tmp_path):
    _receipt_dir(monkeypatch, tmp_path)
    receipt = issue_receipt(tool="browser", action="click", target="Create", arguments={"target": "Create"}, now=100)
    consume_receipt(receipt, tool="browser", action="click", target="Create", arguments={"target": "Create"}, now=100)
    with pytest.raises(ReceiptError, match="absent|used"):
        consume_receipt(receipt, tool="browser", action="click", target="Create", arguments={"target": "Create"}, now=100)


def test_argument_mutation_invalidates_receipt(monkeypatch, tmp_path):
    _receipt_dir(monkeypatch, tmp_path)
    receipt = issue_receipt(tool="browser", action="type_submit", target="#save", arguments={"target": "#save", "text": "yes", "submit": True}, now=100)
    with pytest.raises(ReceiptError, match="arguments_hash"):
        consume_receipt(receipt, tool="browser", action="type_submit", target="#save", arguments={"target": "#save", "text": "no", "submit": True}, now=100)


@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        ("click", {"index": 2}),
        ("type", {"target": "#save", "text": "yes", "submit": True}),
        ("goto", {"url": "https://example.test"}),
        ("back", {}),
    ],
)
def test_every_consequential_browser_path_requires_receipt(monkeypatch, tmp_path, action, kwargs):
    _receipt_dir(monkeypatch, tmp_path)
    result = browser_action(action, **kwargs)
    assert result["refused"] is True


def test_handoff_auto_login_requires_receipt(monkeypatch, tmp_path):
    _receipt_dir(monkeypatch, tmp_path)
    result = browser_handoff("https://support.csuchico.edu", "sign in", auto_login=True)
    assert result["refused"] is True


def test_visible_handoff_navigation_requires_receipt(monkeypatch, tmp_path):
    _receipt_dir(monkeypatch, tmp_path)
    result = browser_handoff("https://example.test", "open this page")
    assert result["refused"] is True


def test_valid_receipt_is_consumed_before_execution(monkeypatch, tmp_path):
    _receipt_dir(monkeypatch, tmp_path)
    args = {"lane": "quiet", "target": "Create project"}
    receipt = issue_receipt(tool="browser", action="click", target="Create project", arguments=args)
    monkeypatch.setattr("lisan.tools.browser.ensure_browser", lambda lane: False)
    result = browser_action("click", receipt_id=receipt, **args)
    assert result.get("refused") is not True
    assert "could not be started" in result["error"]


def test_valid_handoff_auto_login_receipt_is_consumed_before_execution(monkeypatch, tmp_path):
    _receipt_dir(monkeypatch, tmp_path)
    args = {
        "url": "https://support.csuchico.edu",
        "reason": "sign in",
        "wait_seconds": 0,
        "poll_seconds": 2.0,
        "auto_login": True,
    }
    receipt = issue_receipt(tool="browser", action="handoff", target=args["url"], arguments=args)
    monkeypatch.setattr("lisan.tools.browser.ensure_browser", lambda lane: False)
    result = browser_handoff(receipt_id=receipt, **args)
    assert result.get("refused") is not True
    assert "could not be started" in result["error"]
