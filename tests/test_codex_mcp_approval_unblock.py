"""`codex exec` refuses any MCP tool its server marked destructive, client
side, before the server ever sees the call — because exec mode has no human
attached to approve it. This is generic: it happens for whatever MCP server
and tool an install has wired up, not anything this codebase names. These
tests cover the core, skill-agnostic bridge: detect the refusal from codex's
own stdout, ask the caller's own approval channel, and retry once with a
scoped override if approved.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from lisan.providers.codex import (
    CodexClient,
    _extract_blocked_mcp_tools,
    _extract_task_summary,
    _mcp_approval_override_args,
)

_BLOCKED_STDOUT = (
    "codex\nSome preamble.\n"
    "mcp: tdx/refresh_token started\n"
    "mcp: tdx/refresh_token (failed)\n"
    "MCP tool call requires approval, but approval policy is never\n"
    "codex\nThe call was blocked before execution.\n"
)


def test_extract_blocked_mcp_tools_is_server_and_tool_agnostic():
    assert _extract_blocked_mcp_tools(_BLOCKED_STDOUT) == [("tdx", "refresh_token")]


def test_extract_blocked_mcp_tools_ignores_ordinary_failures():
    stdout = "mcp: tdx/refresh_token started\nmcp: tdx/refresh_token (failed)\nnetwork unreachable\n"
    assert _extract_blocked_mcp_tools(stdout) == []


def test_extract_blocked_mcp_tools_dedupes_repeated_calls():
    doubled = _BLOCKED_STDOUT + _BLOCKED_STDOUT
    assert _extract_blocked_mcp_tools(doubled) == [("tdx", "refresh_token")]


def test_mcp_approval_override_args_shape():
    args = _mcp_approval_override_args([("tdx", "refresh_token"), ("zabbix", "ack_alert")])
    assert args == [
        "-c", 'mcp_servers.tdx.tools.refresh_token.approval_mode="approve"',
        "-c", 'mcp_servers.zabbix.tools.ack_alert.approval_mode="approve"',
    ]


def test_extract_task_summary_prefers_the_task_marker():
    prompt = "You are Codex executing a task.\n\nWorking directory: /x\n\nTask:\nrefresh the TDX token\n\nmore context"
    assert _extract_task_summary(prompt) == "refresh the TDX token"


def test_extract_task_summary_falls_back_to_prompt_head():
    assert _extract_task_summary("just a short prompt") == "just a short prompt"


def _batch_sequence(responses):
    calls = []

    def fake_batch(args, **kw):
        calls.append(list(args))
        stdout = responses[len(calls) - 1]
        return MagicMock(returncode=0, stdout=stdout, stderr="")

    return calls, fake_batch


def test_approved_block_retries_once_with_the_override_and_succeeds():
    calls, fake_batch = _batch_sequence([_BLOCKED_STDOUT, "all done"])
    approvals = []

    def approve(tool_name, args):
        approvals.append((tool_name, args))
        return True

    with patch("lisan.providers.codex._run_batch", side_effect=fake_batch), \
            patch("lisan.providers.codex.progress_listener_active", return_value=False):
        response = CodexClient({"providers": {"codex": {}}}).complete(
            "Task:\nrefresh the TDX token\n\n", agent="codex", approval_fn=approve,
        )

    assert len(calls) == 2
    assert 'mcp_servers.tdx.tools.refresh_token.approval_mode="approve"' in calls[1]
    assert response.text == "all done"
    assert approvals[0][0] == "mcp_tool_approval"
    assert "tdx.refresh_token" in approvals[0][1]["task"]
    assert "refresh the TDX token" in approvals[0][1]["task"]


def test_denied_block_does_not_retry_and_surfaces_the_refusal():
    calls, fake_batch = _batch_sequence([_BLOCKED_STDOUT])

    with patch("lisan.providers.codex._run_batch", side_effect=fake_batch), \
            patch("lisan.providers.codex.progress_listener_active", return_value=False):
        response = CodexClient({"providers": {"codex": {}}}).complete(
            "hi", agent="codex", approval_fn=lambda *a, **k: False,
        )

    assert len(calls) == 1
    assert "approval policy is never" in response.text


def test_no_approval_channel_means_no_retry_and_no_silent_bypass():
    calls, fake_batch = _batch_sequence([_BLOCKED_STDOUT])

    with patch("lisan.providers.codex._run_batch", side_effect=fake_batch), \
            patch("lisan.providers.codex.progress_listener_active", return_value=False):
        response = CodexClient({"providers": {"codex": {}}}).complete("hi", agent="codex")

    assert len(calls) == 1
    assert "approval policy is never" in response.text


def test_unblocked_run_never_asks_for_approval():
    calls, fake_batch = _batch_sequence(["nothing was blocked here"])
    approve = MagicMock(return_value=True)

    with patch("lisan.providers.codex._run_batch", side_effect=fake_batch), \
            patch("lisan.providers.codex.progress_listener_active", return_value=False):
        response = CodexClient({"providers": {"codex": {}}}).complete(
            "hi", agent="codex", approval_fn=approve,
        )

    assert len(calls) == 1
    approve.assert_not_called()
    assert response.text == "nothing was blocked here"
