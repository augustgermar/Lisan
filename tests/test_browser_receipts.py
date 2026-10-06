from __future__ import annotations

import inspect

import pytest

from lisan.tools.approval_receipts import ReceiptError, action_records_path, audit_root, consume_receipt, issue_receipt, record_action_result
from lisan.tools.browser import (
    NON_CONSEQUENTIAL_BROWSER_PATHS,
    RECEIPT_REQUIRED_BROWSER_PATHS,
    browser_action,
    browser_handoff,
)
from lisan.tools.execution_tools import _browser_tool
from lisan.tools.execution_tools import TOOLS


def test_browser_action_surface_is_classified_and_receipt_gated():
    browser_schema = next(tool for tool in TOOLS if tool["name"] == "browser")
    schema_actions = set(browser_schema["parameters"]["properties"]["action"]["enum"])
    classified_actions = {
        path.split("[", 1)[0]
        for path in RECEIPT_REQUIRED_BROWSER_PATHS | NON_CONSEQUENTIAL_BROWSER_PATHS
    }
    assert schema_actions == classified_actions

    browser_source = inspect.getsource(browser_action)
    handoff_source = inspect.getsource(browser_handoff)
    assert "consume_receipt" in browser_source
    assert "consume_receipt" in handoff_source
    for action in ("click", "goto", "back"):
        assert f'action == "{action}"' in browser_source
    assert 'action == "type" and kw.get("submit")' in browser_source
    assert 'action="handoff"' in handoff_source


def _receipt_dir(monkeypatch, tmp_path):
    path = tmp_path / "receipts"
    monkeypatch.setenv("LISAN_RECEIPT_DIR", str(path))
    monkeypatch.setenv("LISAN_AUDIT_DIR", str(path))
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


def test_action_ledger_records_issue_consumption_and_result(monkeypatch, tmp_path):
    _receipt_dir(monkeypatch, tmp_path)
    args = {"lane": "quiet", "url": "https://example.test", "auto_login": False}
    receipt = issue_receipt(tool="browser", action="goto", target=args["url"], arguments=args, now=100)
    consume_receipt(receipt, tool="browser", action="goto", target=args["url"], arguments=args, now=101)
    record_action_result(
        receipt,
        tool="browser",
        action="goto",
        target=args["url"],
        arguments=args,
        result={"ok": True, "url": args["url"], "title": "Example"},
        timestamp=102,
    )
    lines = [__import__("json").loads(line) for line in action_records_path().read_text().splitlines()]
    assert [line["event"] for line in lines] == ["issued", "consumed", "result"]
    assert lines[-1]["receipt_id"] == receipt
    assert lines[-1]["arguments"] == args
    assert lines[-1]["navigation_url"] == args["url"]
    assert lines[-1]["result"]["ok"] is True


def test_browser_tool_writes_result_after_consuming_receipt(monkeypatch, tmp_path):
    _receipt_dir(monkeypatch, tmp_path)
    args = {"lane": "quiet", "target": "Create project"}
    receipt = issue_receipt(tool="browser", action="click", target=args["target"], arguments=args)
    monkeypatch.setattr("lisan.tools.browser.ensure_browser", lambda lane: False)
    result = __import__("json").loads(_browser_tool("click", receipt_id=receipt, **args))
    assert result["ok"] is False and result.get("refused") is not True
    lines = [__import__("json").loads(line) for line in action_records_path().read_text().splitlines()]
    assert [line["event"] for line in lines] == ["issued", "consumed", "result"]
    assert lines[-1]["target"] == args["target"]
    assert lines[-1]["arguments"] == args
    assert lines[-1]["result"]["ok"] is False


def test_action_ledger_rotates_and_retains_segments(monkeypatch, tmp_path):
    path = _receipt_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("LISAN_AUDIT_MAX_BYTES", "1024")
    monkeypatch.setenv("LISAN_AUDIT_SEGMENTS", "2")
    for index in range(8):
        issue_receipt(
            tool="browser",
            action="goto",
            target=f"https://example.test/{index}",
            arguments={"url": f"https://example.test/{index}", "padding": "x" * 500},
        )
    assert (path / "action_records.jsonl").exists()
    assert (path / "action_records.jsonl.1").exists()
    assert (path / "action_records.jsonl.2").exists()
    assert not (path / "action_records.jsonl.3").exists()
    assert oct((path / "action_records.jsonl").stat().st_mode & 0o777) == "0o600"
    assert oct(path.stat().st_mode & 0o777) == "0o700"


def test_audit_root_resolves_platform_paths(monkeypatch, tmp_path):
    monkeypatch.delenv("LISAN_AUDIT_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("lisan.tools.approval_receipts.sys.platform", "darwin")
    assert audit_root() == tmp_path / "Library" / "Application Support" / "Lisan" / "audit"
    monkeypatch.setattr("lisan.tools.approval_receipts.sys.platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert audit_root() == tmp_path / "state" / "lisan" / "audit"
