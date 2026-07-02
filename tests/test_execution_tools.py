from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from lisan.agents.interlocutor import InterlocutorAgent
from lisan.providers.base import LLMResponse
from lisan.tools import execution_tools
from lisan.tools.execution_tools import read_file, run_codex, search_memory


def test_search_memory_delegates_to_assemble_context(tmp_path: Path, monkeypatch) -> None:
    called = {}

    def fake_assemble_context(query: str, **kwargs):
        called["query"] = query
        called["kwargs"] = kwargs
        return "assembled context"

    monkeypatch.setattr(execution_tools, "assemble_context", fake_assemble_context)
    out = search_memory("budget authority", vault=tmp_path, db_path=tmp_path / "db.sqlite")
    assert out == "assembled context"
    assert called["query"] == "budget authority"
    assert called["kwargs"]["vault"] == tmp_path


def test_read_file_validates_path_and_size(tmp_path: Path) -> None:
    rel = read_file("relative/path.txt")
    assert "absolute" in rel.lower()

    missing = read_file(str(tmp_path / "missing.txt"))
    assert "does not exist" in missing.lower()

    small = tmp_path / "note.txt"
    small.write_text("hello", encoding="utf-8")
    assert read_file(str(small)) == "hello"

    large = tmp_path / "large.txt"
    large.write_text("x" * (50 * 1024 + 1), encoding="utf-8")
    too_large = read_file(str(large))
    assert "exceeds size limit" in too_large.lower()


def test_run_codex_respects_approval_gate(tmp_path: Path, monkeypatch) -> None:
    called = {"complete": 0}

    class FakeCodex:
        def __init__(self, config):
            self.config = config

        def complete(self, *args, **kwargs):
            called["complete"] += 1
            return LLMResponse(text="ok", provider="codex", model="fake")

    monkeypatch.setattr(execution_tools, "CodexClient", FakeCodex)
    denied = run_codex(
        "fix the config",
        working_directory=str(tmp_path),
        vault=tmp_path,
        approval_fn=lambda *_: False,
    )
    assert denied == "User denied the task"
    assert called["complete"] == 0

    approved = run_codex(
        "fix the config",
        working_directory=str(tmp_path),
        vault=tmp_path,
        approval_fn=lambda *_: True,
    )
    assert approved == "ok"
    assert called["complete"] == 1


def test_interlocutor_tool_loop_executes_tool_and_returns_final_response(tmp_path: Path, monkeypatch) -> None:
    agent = InterlocutorAgent(vault=tmp_path)

    first = LLMResponse(
        text='<tool_call>{"tool": "read_file", "args": {"path": "/tmp/example.txt"}}</tool_call>',
        provider="mock",
        model="mock",
    )
    second = LLMResponse(
        text=json.dumps(
            {
                "response": "I checked the file.",
                "questions": [],
                "updated_narrative_state": {},
                "recommended_action": "auto_commit",
            }
        ),
        provider="mock",
        model="mock",
    )
    agent.llm = MagicMock()
    agent.llm.complete.side_effect = [first, second]

    monkeypatch.setattr(
        "lisan.agents.interlocutor.build_tool_handlers",
        lambda **kwargs: {"read_file": lambda path: "file contents"},
    )

    result = agent.run_json(
        "what's in the file?",
        vault=tmp_path,
        db_path=tmp_path / "lisan.sqlite",
        conversation_id="demo",
    )

    assert result["response"] == "I checked the file."
    assert len(agent.last_tool_calls) == 1
    assert agent.last_tool_calls[0]["tool"] == "read_file"
    assert agent.last_tool_calls[0]["result"] == "file contents"
