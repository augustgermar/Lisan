"""A tool's side effect runs once per turn, however the turn is retried."""
from __future__ import annotations

from pathlib import Path

from lisan.agents.base import invoke_tool
from lisan.agents.conversation import ConversationAgent


def test_a_type_error_inside_a_tool_does_not_run_it_again():
    calls = []

    def handler(message):
        calls.append(message)
        raise TypeError("bug inside the tool")

    try:
        invoke_tool(handler, {"message": "hi"}, tool_name="send")
    except TypeError:
        pass
    assert calls == ["hi"]


def test_dict_style_handlers_still_receive_the_args_dict():
    seen = []
    assert invoke_tool(lambda args: seen.append(args) or "ok", {"a": 1}) == "ok"
    assert seen == [{"a": 1}]


def test_unbindable_arguments_are_reported_not_executed():
    def handler(*, required):
        raise AssertionError("must not run")

    out = invoke_tool(handler, {"wrong": 1}, tool_name="t")
    assert out.startswith("Error: invalid arguments for t")


def test_identical_side_effect_calls_within_a_turn_run_once(tmp_path: Path):
    agent = ConversationAgent(vault=tmp_path)
    sent = []
    handlers = {"send_email": lambda subject, body: sent.append(subject) or "sent"}
    first_pass = agent._once_per_turn(handlers)
    assert first_pass["send_email"](subject="s", body="b") == "sent"
    # a retry of the same turn rebuilds its handlers from scratch
    second_pass = agent._once_per_turn(handlers)
    again = second_pass["send_email"](subject="s", body="b")
    assert sent == ["s"]
    assert "not run again" in again and again.endswith("sent")
    second_pass["send_email"](subject="different", body="b")
    assert sent == ["s", "different"]


def test_read_only_tools_are_not_memoized(tmp_path: Path):
    agent = ConversationAgent(vault=tmp_path)
    reads = []
    handlers = agent._once_per_turn({"read_file": lambda path: reads.append(path) or "x"})
    handlers["read_file"](path="a")
    handlers["read_file"](path="a")
    assert reads == ["a", "a"]


def test_a_new_turn_gets_a_fresh_memo(tmp_path: Path):
    sent = []
    handlers = {"send_email": lambda subject: sent.append(subject) or "sent"}
    ConversationAgent(vault=tmp_path)._once_per_turn(handlers)["send_email"](subject="s")
    ConversationAgent(vault=tmp_path)._once_per_turn(handlers)["send_email"](subject="s")
    assert sent == ["s", "s"]
