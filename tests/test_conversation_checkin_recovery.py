from __future__ import annotations

import json
from pathlib import Path

from lisan.agents.conversation import ConversationAgent
from lisan.paths import ensure_repo_layout, vault_root
from lisan.providers.base import LLMResponse


class _ScriptedLLM:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return LLMResponse(text=self.responses.pop(0), provider="test", model="test")


def _agent(tmp_path: Path, monkeypatch, responses: list[str], handler):
    ensure_repo_layout(tmp_path)
    agent = ConversationAgent(vault=vault_root(tmp_path), config={"test": True})
    llm = _ScriptedLLM(responses)
    agent.llm = llm
    monkeypatch.setattr(
        "lisan.agents.conversation.agent_tools",
        lambda: [{"name": "checkin", "description": "record observation"}],
    )
    monkeypatch.setattr(
        "lisan.agents.conversation.build_tool_handlers",
        lambda **kwargs: {"checkin": handler},
    )
    return agent, llm


def test_unavailable_claim_triggers_narrow_checkin_recovery(tmp_path, monkeypatch):
    seen = []

    def checkin(**args):
        seen.append(args)
        return json.dumps({"ok": True, "recorded": True})

    agent, llm = _agent(
        tmp_path,
        monkeypatch,
        [
            "I couldn't log the check-in because the check-in tool isn't available.",
            json.dumps({"tool": "checkin", "args": {"person": "me", "note": "Feeling good today."}}),
            "Logged a check-in.",
        ],
        checkin,
    )

    result = agent.run_json('{"user_message":"I am feeling good today."}')

    assert seen == [{"person": "me", "note": "Feeling good today."}]
    assert result["response"] == "Logged a check-in."
    assert len(llm.prompts) == 3
    assert "INTERNAL TOOL-RECOVERY INSTRUCTION" in llm.prompts[1]
    available = llm.prompts[1].split("AVAILABLE_TOOLS:\n", 1)[1].split("\n\n", 1)[0]
    assert '"name": "checkin"' in available
    assert '"name": "read_file"' not in available
    assert [call["tool"] for call in agent.last_tool_calls] == ["checkin"]


def test_failed_recovery_does_not_repeat_false_unavailable_claim(tmp_path, monkeypatch):
    agent, _ = _agent(
        tmp_path,
        monkeypatch,
        [
            "The check-in tool isn't available.",
            "The check-in tool isn't available.",
        ],
        lambda **args: json.dumps({"ok": True, "recorded": True}),
    )

    result = agent.run_json('{"user_message":"I slept well."}')

    assert result["response"] == "I didn’t log that check-in. I don’t know why I missed the tool call."
    assert "available" not in result["response"]
    assert agent.last_tool_calls == []


def test_ordinary_turn_does_not_trigger_recovery(tmp_path, monkeypatch):
    agent, llm = _agent(tmp_path, monkeypatch, ["Good morning!"], lambda **args: "ok")

    result = agent.run_json('{"user_message":"Good morning."}')

    assert result["response"] == "Good morning!"
    assert len(llm.prompts) == 1


def test_registered_tool_unavailable_claim_is_retried(tmp_path, monkeypatch):
    seen = []

    def execute_task(**args):
        seen.append(args)
        return "pwd-result"

    ensure_repo_layout(tmp_path)
    agent = ConversationAgent(vault=vault_root(tmp_path), config={"test": True})
    llm = _ScriptedLLM([
        "The callable execute_task tool is not exposed here.",
        json.dumps({"tool": "execute_task", "args": {"task": "pwd"}}),
        "Executed it: pwd-result",
    ])
    agent.llm = llm
    monkeypatch.setattr(
        "lisan.agents.conversation.agent_tools",
        lambda: [{"name": "execute_task", "description": "run an approved task"}],
    )
    monkeypatch.setattr(
        "lisan.agents.conversation.build_tool_handlers",
        lambda **kwargs: {"execute_task": execute_task},
    )

    result = agent.run_json("please run pwd")

    assert seen == [{"task": "pwd"}]
    assert result["response"] == "Executed it: pwd-result"
    assert "INTERNAL TOOL-RECOVERY INSTRUCTION" in llm.prompts[1]
    assert [call["tool"] for call in agent.last_tool_calls] == ["execute_task"]


def test_registered_unavailable_claim_cannot_pass_without_a_tool_call(tmp_path, monkeypatch):
    ensure_repo_layout(tmp_path)
    agent = ConversationAgent(vault=vault_root(tmp_path), config={"test": True})
    llm = _ScriptedLLM([
        "printer-management is unavailable in this session.",
        "printer-management remains unavailable.",
    ])
    agent.llm = llm
    monkeypatch.setattr(
        "lisan.agents.conversation.agent_tools",
        lambda: [{"name": "printer-management", "description": "printer status and approved printing"}],
    )
    monkeypatch.setattr(
        "lisan.agents.conversation.build_tool_handlers",
        lambda **kwargs: {"printer-management": lambda **args: "status-ok"},
    )

    result = agent.run_json("check the printer")

    assert result["response"] == (
        "I did not call printer-management; it is registered, but no action was performed."
    )
    assert agent.last_tool_calls == []


def test_neither_tools_exposed_claim_is_detected(tmp_path):
    ensure_repo_layout(tmp_path)
    agent = ConversationAgent(vault=vault_root(tmp_path), config={"test": True})
    result = agent._registered_unavailable_tools(
        type("Result", (), {
            "data": None,
            "text": "Neither `printer-management` nor `execute_task` is exposed.",
            "tool_calls": [],
        })(),
        [
            {"name": "execute_task"},
            {"name": "printer-management"},
        ],
    )
    assert result == ["execute_task", "printer-management"]


def test_generic_tools_doesnt_expose_claim_is_not_guessed(tmp_path):
    ensure_repo_layout(tmp_path)
    agent = ConversationAgent(vault=vault_root(tmp_path), config={"test": True})
    result = agent._registered_unavailable_tools(
        type("Result", (), {
            "data": None,
            "text": "This turn doesn't expose the available tools.",
            "tool_calls": [],
        })(),
        [
            {"name": "execute_task"},
            {"name": "printer-management"},
        ],
    )
    assert result == []


def test_role_named_tools_doesnt_expose_claim_is_mapped(tmp_path):
    ensure_repo_layout(tmp_path)
    agent = ConversationAgent(vault=vault_root(tmp_path), config={"test": True})
    result = agent._registered_unavailable_tools(
        type("Result", (), {
            "data": None,
            "text": "This turn doesn't expose the live printer or executor tools.",
            "tool_calls": [],
        })(),
        [
            {"name": "execute_task"},
            {"name": "printer-management"},
        ],
    )
    assert result == ["execute_task", "printer-management"]
