from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lisan.agents.interlocutor import InterlocutorAgent
from lisan.paths import ensure_repo_layout, vault_root
from lisan.providers.base import LLMResponse


class _ScriptedLLM:
    """Returns canned responses in order and records every call."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, prompt, *, agent=None, significance=None, provider=None, model=None, schema=None, **kwargs):
        self.calls.append({"prompt": prompt, "schema": schema})
        text = self.responses.pop(0) if self.responses else "{}"
        return LLMResponse(text=text, provider="stub", model="stub")


_FINAL = json.dumps({
    "response": "Ingested it.",
    "questions": [],
    "updated_narrative_state": {},
    "recommended_action": "auto_commit",
})


class ToolLoopSchemaTests(unittest.TestCase):
    """With tools present, the output schema must be offered as one of two
    legal response shapes — passing it to the provider renders a hard
    "must match this schema" instruction that forbids the tool-call form
    and silently disables every tool (the production ingestion dodge)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, responses: list[str]) -> tuple[InterlocutorAgent, _ScriptedLLM]:
        agent = InterlocutorAgent(vault=self.vault, config={})
        llm = _ScriptedLLM(responses)
        agent.llm = llm
        return agent, llm

    def test_schema_not_passed_to_provider_when_tools_present(self):
        agent, llm = self._agent([_FINAL])
        result = agent.complete_with_tools(
            "{}", schema={"type": "object"}, tools=[{"name": "echo"}], tool_handlers={},
        )
        self.assertIsNone(llm.calls[0]["schema"])
        self.assertIn("EITHER a tool call", llm.calls[0]["prompt"])
        self.assertIn('"type": "object"', llm.calls[0]["prompt"])
        self.assertEqual(result.data["response"], "Ingested it.")

    def test_schema_passed_through_without_tools(self):
        agent, llm = self._agent([_FINAL])
        agent.complete_with_tools("{}", schema={"type": "object"}, tools=None, tool_handlers={})
        self.assertEqual(llm.calls[0]["schema"], {"type": "object"})

    def test_tool_call_round_trip(self):
        seen: list[dict] = []

        def echo(**args):
            seen.append(args)
            return "echoed: " + json.dumps(args, sort_keys=True)

        agent, llm = self._agent([
            json.dumps({"tool": "echo", "args": {"path": "/tmp/x"}}),
            _FINAL,
        ])
        result = agent.complete_with_tools(
            "{}", schema={"type": "object"}, tools=[{"name": "echo"}], tool_handlers={"echo": echo},
        )
        self.assertEqual(seen, [{"path": "/tmp/x"}])
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("TOOL_RESULT", llm.calls[1]["prompt"])
        self.assertIn("echoed:", llm.calls[1]["prompt"])
        self.assertEqual(result.data["response"], "Ingested it.")
        self.assertEqual(result.tool_calls[0]["tool"], "echo")


if __name__ == "__main__":
    unittest.main()
