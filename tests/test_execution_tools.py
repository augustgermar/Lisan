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
    assert "Approval was not granted" in denied
    assert called["complete"] == 0

    approved = run_codex(
        "fix the config",
        working_directory=str(tmp_path),
        vault=tmp_path,
        approval_fn=lambda *_: True,
    )
    assert approved == "ok"
    assert called["complete"] == 1


def test_run_codex_tool_description_mentions_lisan_cli_commands() -> None:
    description = next(tool["description"] for tool in execution_tools.TOOLS if tool["name"] == "run_codex")
    assert "run Lisan CLI commands" in description
    assert "run shell commands" in description


def test_interlocutor_prompt_strongly_prefers_action_first_rules() -> None:
    prompt_path = Path("prompts/interlocutor_v1.md")
    text = prompt_path.read_text(encoding="utf-8")
    assert "CRITICAL RESPONSE RULE" in text
    assert "When the user asks you to SHOW, READ, LIST, or DISPLAY a file or directory" in text
    assert "You can run ANY Lisan CLI command via run_codex" in text


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


def test_codex_workspace_is_the_install_not_home():
    from pathlib import Path

    from lisan.tools.execution_tools import codex_workspace

    workspace = Path(codex_workspace())
    assert workspace != Path.home()
    assert "Lisan" in str(workspace) or ".lisan" in str(workspace) or "lisan" in str(workspace).lower()


def test_codex_briefing_declares_write_boundary():
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from lisan.paths import ensure_repo_layout, vault_root
    from lisan.tools import execution_tools
    from lisan.tools.execution_tools import _build_codex_prompt

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ensure_repo_layout(root)
        with patch.object(execution_tools, "assemble_context", return_value="(ctx)"):
            prompt = _build_codex_prompt(task="t", working_directory=root, vault=vault_root(root), db_path=root / "x.sqlite")
    assert "HARD WRITE BOUNDARY" in prompt
    assert "READ-ONLY" in prompt
