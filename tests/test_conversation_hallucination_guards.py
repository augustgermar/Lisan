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


def _agent(tmp_path: Path, monkeypatch, responses: list[str], tools: list[dict], handlers: dict):
    ensure_repo_layout(tmp_path)
    agent = ConversationAgent(vault=vault_root(tmp_path), config={"test": True})
    llm = _ScriptedLLM(responses)
    agent.llm = llm
    monkeypatch.setattr("lisan.agents.conversation.agent_tools", lambda: tools)
    monkeypatch.setattr("lisan.agents.conversation.build_tool_handlers", lambda **kwargs: handlers)
    return agent, llm


# --- Fix: a false "unavailable" claim buried in an otherwise-real answer
# must not wipe out the real answer (the Oct-14/Nov-2 "canned non-answer"
# bug: unrelated questions got replaced wholesale by the recovery fallback).


def test_false_claim_in_one_sentence_does_not_erase_the_real_answer(tmp_path, monkeypatch):
    agent, llm = _agent(
        tmp_path,
        monkeypatch,
        [
            (
                "The database file can't be opened because of a file-permission mismatch "
                "on /var/www/data/state/lisan_agent.sqlite. Also, execute_task is not available "
                "in this channel."
            ),
            "Still can't confirm execute_task is available here.",
        ],
        [{"name": "execute_task", "description": "run an approved task"}],
        {"execute_task": lambda **args: "ran"},
    )

    result = agent.run_json("why can't you open the database file?")

    assert "file-permission mismatch" in result["response"]
    assert "/var/www/data/state/lisan_agent.sqlite" in result["response"]
    assert "I did not call execute_task; it is registered, but no action was performed." in result["response"]
    # recovery ran exactly once
    assert len(llm.prompts) == 2


def test_hedge_phrasing_without_literal_not_available_is_caught(tmp_path, monkeypatch):
    seen = []

    def execute_task(**args):
        seen.append(args)
        return "done"

    agent, llm = _agent(
        tmp_path,
        monkeypatch,
        [
            "I don't have the GitHub executor available in this channel.",
            json.dumps({"tool": "execute_task", "args": {"task": "check github status"}}),
            "Done — checked GitHub status.",
        ],
        [{"name": "execute_task", "description": "run an approved task"}],
        {"execute_task": execute_task},
    )

    result = agent.run_json("check github status")

    assert seen == [{"task": "check github status"}]
    assert result["response"] == "Done — checked GitHub status."
    assert "INTERNAL TOOL-RECOVERY INSTRUCTION" in llm.prompts[1]


def test_unrelated_tool_mention_elsewhere_is_not_flagged(tmp_path, monkeypatch):
    # printer-management is mentioned, but not in the same sentence as any
    # unavailability phrase — must not trigger the false-claim path at all.
    agent, llm = _agent(
        tmp_path,
        monkeypatch,
        ["I checked printer-management earlier today and the queue was empty."],
        [{"name": "printer-management", "description": "printer status"}],
        {"printer-management": lambda **args: "ok"},
    )

    result = agent.run_json("any printer issues today?")

    assert result["response"] == "I checked printer-management earlier today and the queue was empty."
    assert len(llm.prompts) == 1


# --- Fix: claiming a side-effecting action succeeded when no tool call was
# made (the Sept-22 "fabricated email send" bug).


def test_fabricated_email_send_is_caught_and_corrected(tmp_path, monkeypatch):
    agent, llm = _agent(
        tmp_path,
        monkeypatch,
        [
            "Test email sent. The relay accepted it.",
            "I didn't actually send it — let me call the tool now.",
        ],
        [{"name": "send_email", "description": "send an email"}],
        {"send_email": lambda **args: "sent-ok"},
    )

    result = agent.run_json("send me a test email")

    assert "I did not actually call send_email" in result["response"]
    assert "sent" not in result["response"].lower().split("i did not actually call send_email")[0]
    assert len(llm.prompts) == 2


def test_fabricated_claim_recovers_when_model_calls_the_real_tool(tmp_path, monkeypatch):
    seen = []

    def send_email(**args):
        seen.append(args)
        return "sent-ok"

    agent, llm = _agent(
        tmp_path,
        monkeypatch,
        [
            "Test email sent to you just now.",
            json.dumps({"tool": "send_email", "args": {"to": "owner", "subject": "test", "body": "hi"}}),
            "Sent the test email.",
        ],
        [{"name": "send_email", "description": "send an email"}],
        {"send_email": send_email},
    )

    result = agent.run_json("send me a test email")

    assert seen == [{"to": "owner", "subject": "test", "body": "hi"}]
    assert result["response"] == "Sent the test email."


def test_real_tool_call_this_turn_is_not_flagged_as_fabricated(tmp_path, monkeypatch):
    def send_email(**args):
        return "sent-ok"

    agent, llm = _agent(
        tmp_path,
        monkeypatch,
        [
            json.dumps({"tool": "send_email", "args": {"to": "owner", "subject": "test", "body": "hi"}}),
            "Email sent.",
        ],
        [{"name": "send_email", "description": "send an email"}],
        {"send_email": send_email},
    )

    result = agent.run_json("send me a test email")

    assert result["response"] == "Email sent."
    assert len(llm.prompts) == 2
