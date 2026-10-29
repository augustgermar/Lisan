"""The tool loop stays inside a prompt budget and says honestly when it
runs out of rounds."""
from __future__ import annotations

import json
from pathlib import Path

from lisan.agents.base import _render_tool_rounds
from lisan.agents.conversation import ConversationAgent
from lisan.providers.base import LLMResponse


def test_old_results_are_elided_newest_kept():
    rounds = [{"call": f'{{"tool": "read_file", "n": {i}}}', "result": "x" * 5000} for i in range(10)]
    out = _render_tool_rounds(rounds, budget=12_000)
    assert len(out) < 13_000
    assert out.count("x" * 5000) >= 1
    assert out.rstrip().endswith("final JSON response now.")
    assert all(f'"n": {i}' in out for i in range(10))  # every call still visible


def test_running_out_of_rounds_is_reported_as_such(tmp_path: Path):
    agent = ConversationAgent(vault=tmp_path, config={"conversation": {"max_tool_iterations": 2}})

    class Looping:
        def complete(self, prompt, **kwargs):
            return LLMResponse(text='{"tool": "self_state", "args": {}}', provider="fake", model="fake")

    agent.llm = Looping()
    out = agent.run_json(json.dumps({"user_message": "go"}), provider_error_mode="raise")
    assert "ran out of steps" in out["response"]
    assert "2 tool calls" in out["response"]
