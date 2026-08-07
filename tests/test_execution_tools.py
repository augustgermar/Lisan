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


def test_run_codex_executes_without_asking(tmp_path: Path, monkeypatch) -> None:
    """Owner decision 2026-07-26: the per-action approval gate is deleted.
    The owner's command is the consent; the run executes immediately."""
    called = {"complete": 0}

    class FakeCodex:
        def __init__(self, config):
            self.config = config

        def complete(self, *args, **kwargs):
            called["complete"] += 1
            return LLMResponse(text="ok", provider="codex", model="fake")

    monkeypatch.setattr(execution_tools, "CodexClient", FakeCodex)
    result = run_codex(
        "fix the config",
        working_directory=str(tmp_path),
        vault=tmp_path,
        config={"providers": {}},
    )
    assert result == "ok"
    assert called["complete"] == 1


class _FakeCodex:
    def __init__(self, config):
        self.config = config

    def complete(self, *args, **kwargs):
        return LLMResponse(text="ok", provider="codex", model="fake")


def _write_chat_intent(vault: Path, *, json_patch: dict[str, str]) -> None:
    """A customized (non-sentinel) intent.md with the template's delegations
    JSON edited via literal string replacement."""
    from lisan.tools.intent import default_intent_document, intent_path

    text = default_intent_document(today="2026-07-26")
    for old, new in json_patch.items():
        assert old in text, f"template drifted: {old!r} not found"
        text = text.replace(old, new)
    path = intent_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_run_codex_intent_never_rule_is_final(tmp_path: Path, monkeypatch) -> None:
    """With the approval gate gone, intent.md never-rules are the ONE hard
    rail left: a global 'never' must block execution outright."""
    called = {"complete": 0}

    class Codex(_FakeCodex):
        def complete(self, *args, **kwargs):
            called["complete"] += 1
            return LLMResponse(text="ok", provider="codex", model="fake")

    monkeypatch.setattr(execution_tools, "CodexClient", Codex)
    _write_chat_intent(tmp_path, json_patch={
        '"spend_money": "confirm_always"': '"run_local_scripts": "never", "spend_money": "confirm_always"',
    })
    result = run_codex(
        "fix the config", working_directory=str(tmp_path), vault=tmp_path,
        config={"providers": {}},
    )
    assert "intent.md" in result and "forbids" in result
    assert called["complete"] == 0


def test_run_codex_uncustomized_intent_still_executes(tmp_path: Path, monkeypatch) -> None:
    """Sentinel dates = no standing authority either way: with the gate
    deleted, the command simply executes."""
    from lisan.tools.intent import default_intent_document, intent_path

    monkeypatch.setattr(execution_tools, "CodexClient", _FakeCodex)
    path = intent_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_intent_document(), encoding="utf-8")  # sentinel dates
    result = run_codex(
        "fix the config", working_directory=str(tmp_path), vault=tmp_path,
        config={"providers": {}},
    )
    assert result == "ok"


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
    # Stale-memory refusals: a remembered restriction must never preempt a
    # live attempt (2026-07-26: a mkdir was refused from a superseded July 18
    # "sandbox" claim the owner had already lifted).
    assert "Never refuse an action on memory alone" in text


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

    from lisan.tools.execution_tools import codex_workspace, repo_root

    workspace = Path(codex_workspace())
    assert workspace != Path.home()
    assert workspace not in Path.home().parents
    assert workspace != Path(workspace.anchor)
    # The real invariant, independent of what the clone directory is named:
    # the workspace is the repo itself or an ancestor of it (the smallest
    # tree that also holds the vault). The old check grepped the path for
    # "lisan" and failed on any differently-named clone.
    repo = Path(repo_root())
    assert workspace == repo or workspace in repo.parents


def _workspace_for(monkeypatch, repo, vault):
    """codex_workspace() with both trees placed under the test's own tmp_path.

    The previous version of the disjoint test hardcoded the vault as
    ``/private/tmp/nowhere/disjoint-vault``, which is only disjoint from a repo
    that does not itself live under ``/private/tmp``. It passed on the
    developer's install at ``~/.lisan/repo`` and failed on any clean checkout
    under a temp dir — a test that measured where the developer keeps their
    code. Building both sides from ``tmp_path`` makes the *shape* the subject,
    which is what the rule is actually about.
    """
    from lisan.tools import execution_tools

    repo.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(execution_tools, "repo_root", lambda *a, **k: repo)
    monkeypatch.setattr("lisan.paths.vault_root", lambda *a, **k: vault)
    return Path(execution_tools.codex_workspace())


def test_codex_workspace_keeps_the_install_root_shape(monkeypatch, tmp_path):
    """repo and vault as direct children of one directory — what install.sh
    builds and what production runs. This is the shape that must not collapse."""
    install = tmp_path / "dot-lisan"
    workspace = _workspace_for(monkeypatch, install / "repo", install / "vault")
    assert workspace == install.resolve()


def test_codex_workspace_keeps_a_vault_nested_in_the_repo(monkeypatch, tmp_path):
    repo = tmp_path / "clone"
    workspace = _workspace_for(monkeypatch, repo, repo / "lisan-vault")
    assert workspace == repo.resolve()


def test_codex_workspace_collapses_to_repo_when_vault_is_disjoint(monkeypatch, tmp_path):
    repo = tmp_path / "somewhere" / "repo"
    workspace = _workspace_for(monkeypatch, repo, tmp_path / "elsewhere" / "vault")
    assert workspace == repo.resolve()


def test_codex_workspace_collapses_on_an_accidental_shared_ancestor(monkeypatch, tmp_path):
    """The hole the home-relative rule could not see.

    ``~/Documents/code/lisan`` beside ``~/Documents/vault`` shares an ancestor
    that is neither home, nor above home, nor the filesystem root — so the old
    rule accepted it and handed the executor the whole of ``~/Documents`` as
    its working directory. Sharing a big unrelated parent is a coincidence of
    where two trees sit, not a declaration that the space between them is a
    workspace.
    """
    documents = tmp_path / "Documents"
    repo = documents / "code" / "lisan"
    workspace = _workspace_for(monkeypatch, repo, documents / "vault")
    assert workspace == repo.resolve()
    assert workspace != documents.resolve()


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
    # Owner decision 2026-07-25: full filesystem access, with two standing
    # rules — memory updates mean Lisan's own records, and the identity
    # kernel stays read-only outside the ratification ceremony.
    assert "FILESYSTEM ACCESS" in prompt
    assert "full read and write access" in prompt
    assert "identity-core.md" in prompt
    assert "READ-ONLY" in prompt
