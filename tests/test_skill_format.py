"""The Agent Skills format: SKILL.md frontmatter, discovery, progressive disclosure.

The convention every agentic system converged on. What matters here is what the
old loader could not do: a skill that is *instructions plus supporting files*,
with no Python entry point, was skipped without a word — so a correctly-written
standard skill was invisible.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lisan.tools.skill_format import parse_frontmatter, parse_skill_md, supporting_files
from lisan.tools.skill_loader import load_skill_handlers, load_skill_manifest, load_skills, render_skill_body


def _skill(root: Path, name: str, frontmatter: str, body: str = "Do the thing.") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return d


def _executable(root: Path, name: str, *, requires_approval: bool = False) -> Path:
    d = _skill(root, name, f"name: {name}\ndescription: An executable skill.")
    (d / "schema.json").write_text(json.dumps({
        "description": "An executable skill.",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": []},
        "requires_approval": requires_approval,
    }), encoding="utf-8")
    (d / "tool.py").write_text(
        "def run(args, vault, config):\n    return 'ran ' + str(args.get('q'))\n", encoding="utf-8")
    return d


# ── frontmatter parsing ───────────────────────────────────────────────────────

def test_parses_the_fields_the_standard_actually_uses():
    """Measured against the 32 skills installed on this machine: name and
    description in all of them, then version, allowed-tools, user-invocable."""
    data, body = parse_frontmatter(
        "---\n"
        "name: zine-layout\n"
        "description: Use when laying out a letterpress zine spread.\n"
        "version: 0.2.0\n"
        "allowed-tools: Read, Bash\n"
        "user-invocable: true\n"
        "---\n\n"
        "# Body\n"
    )
    assert data["name"] == "zine-layout"
    assert data["version"] == "0.2.0"
    assert data["allowed-tools"] == ["Read", "Bash"]
    assert data["user-invocable"] is True
    assert body.strip() == "# Body"


def test_a_block_sequence_parses_like_the_inline_form():
    data, _ = parse_frontmatter("---\nname: x\nallowed-tools:\n  - Read\n  - Grep\n---\n\nbody\n")
    assert data["allowed-tools"] == ["Read", "Grep"]


def test_a_description_containing_a_colon_survives():
    """Descriptions routinely read "Use when X: do Y", and a naive split on the
    first colon truncates them — which matters because the description is the
    entire basis on which the model decides a skill is relevant."""
    data, _ = parse_frontmatter(
        '---\nname: x\ndescription: "Use when parsing YAML: colons are common"\n---\n\nbody\n')
    assert data["description"] == "Use when parsing YAML: colons are common"


def test_no_frontmatter_is_not_an_error():
    data, body = parse_frontmatter("# Just prose\n\nNo frontmatter here.\n")
    assert data == {}
    assert body.startswith("# Just prose")


def test_unterminated_frontmatter_degrades_instead_of_raising():
    """One malformed skill must not take the catalogue down with it."""
    data, body = parse_frontmatter("---\nname: x\nnever closed\n")
    assert data == {}
    assert "never closed" in body


def test_a_missing_description_is_reported_not_swallowed(tmp_path):
    d = _skill(tmp_path, "thin", "name: thin")
    manifest = parse_skill_md(d / "SKILL.md")
    assert not manifest.valid
    assert any("description" in e for e in manifest.errors)


def test_a_missing_name_falls_back_to_the_directory_and_says_so(tmp_path):
    d = _skill(tmp_path, "unnamed", "description: Something useful.")
    manifest = parse_skill_md(d / "SKILL.md")
    assert manifest.name == "unnamed"
    assert any("name" in e for e in manifest.errors)


# ── discovery ─────────────────────────────────────────────────────────────────

def test_an_instruction_only_skill_is_discovered(tmp_path):
    """The gap this closes. The old loader required schema.json AND tool.py AND
    SKILL.md, so a standard skill — instructions and references, no Python —
    was skipped silently."""
    _skill(tmp_path, "zine-layout", "name: zine-layout\ndescription: Lay out a zine.")
    skills = load_skills(tmp_path)
    assert [s["name"] for s in skills] == ["zine-layout"]
    assert skills[0]["executable"] is False


def test_an_executable_skill_still_works(tmp_path):
    _executable(tmp_path, "arxiv_search")
    skills = load_skills(tmp_path)
    assert skills[0]["executable"] is True
    assert skills[0]["parameters"]["type"] == "object"


def test_instruction_only_skills_get_no_callable_handler(tmp_path):
    """They are reached through the `skill` tool, not registered as functions —
    registering one with no entry point would offer the model a tool that
    cannot run."""
    _skill(tmp_path, "instructions", "name: instructions\ndescription: Read me.")
    _executable(tmp_path, "callable")
    handlers = load_skill_handlers(tmp_path, vault=tmp_path, config={})
    assert set(handlers) == {"callable"}


def test_legacy_skills_without_frontmatter_still_load(tmp_path):
    """Skills written before the format keep working: schema.json fills the
    description. Nothing that worked yesterday stops working."""
    d = tmp_path / "legacy"
    d.mkdir()
    (d / "SKILL.md").write_text("# legacy\n\nProse, no frontmatter.\n", encoding="utf-8")
    (d / "schema.json").write_text(json.dumps(
        {"description": "From the schema.", "parameters": {"type": "object", "properties": {}}}),
        encoding="utf-8")
    (d / "tool.py").write_text("def run(args, vault, config):\n    return 'ok'\n", encoding="utf-8")
    skills = load_skills(d.parent)
    assert skills[0]["name"] == "legacy"
    assert skills[0]["description"] == "From the schema."


def test_shared_underscore_packages_are_not_skills(tmp_path):
    (tmp_path / "_google_common").mkdir()
    (tmp_path / "_google_common" / "lisan_google.py").write_text("x = 1\n", encoding="utf-8")
    _skill(tmp_path, "real", "name: real\ndescription: A real one.")
    assert [s["name"] for s in load_skills(tmp_path)] == ["real"]


def test_a_directory_without_a_skill_md_is_skipped(tmp_path):
    (tmp_path / "not-a-skill").mkdir()
    (tmp_path / "not-a-skill" / "notes.txt").write_text("hi", encoding="utf-8")
    assert load_skills(tmp_path) == []


def test_a_missing_skills_directory_is_empty_not_an_error(tmp_path):
    assert load_skills(tmp_path / "nope") == []


# ── progressive disclosure ────────────────────────────────────────────────────

def test_the_body_loads_only_when_asked_for(tmp_path):
    """The economy the format exists for: a catalogue costs one line per skill
    until one is invoked."""
    from lisan.tools.execution_tools import agent_tools

    _skill(tmp_path, "zine-layout",
           "name: zine-layout\ndescription: Lay out a zine.",
           body="Gutters: 14mm inner, 9mm outer.")

    skill_tool = [t for t in agent_tools(tmp_path) if t["name"] == "skill"][0]
    assert "zine-layout: Lay out a zine." in skill_tool["description"]
    assert "14mm" not in skill_tool["description"]        # the body is NOT in context

    body = render_skill_body(load_skill_manifest(tmp_path, "zine-layout"))
    assert "14mm" in body                                  # until it is asked for


def test_supporting_files_are_listed_not_loaded(tmp_path):
    d = _skill(tmp_path, "zine", "name: zine\ndescription: Zine.")
    (d / "references").mkdir()
    (d / "references" / "stock.md").write_text("SECRET STOCK DETAIL\n", encoding="utf-8")
    body = render_skill_body(load_skill_manifest(tmp_path, "zine"))
    assert "references/stock.md" in body
    assert "SECRET STOCK DETAIL" not in body


def test_supporting_files_skips_dotfiles_and_caches(tmp_path):
    d = _skill(tmp_path, "zine", "name: zine\ndescription: Zine.")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")
    (d / ".DS_Store").write_text("junk", encoding="utf-8")
    (d / "notes.md").write_text("keep", encoding="utf-8")
    assert supporting_files(d) == ["notes.md"]


def test_executable_skills_stay_callable_tools_with_their_schema(tmp_path):
    """An executable skill's parameters must be in context — the model cannot
    call a function correctly without them. Only instructional skills defer."""
    from lisan.tools.execution_tools import agent_tools

    _executable(tmp_path, "arxiv_search")
    names = {t["name"] for t in agent_tools(tmp_path)}
    assert "arxiv_search" in names
    arxiv = [t for t in agent_tools(tmp_path) if t["name"] == "arxiv_search"][0]
    assert arxiv["parameters"]["properties"]["q"]["type"] == "string"


def test_a_skill_can_opt_out_of_model_invocation(tmp_path):
    from lisan.tools.execution_tools import agent_tools

    _skill(tmp_path, "manual", "name: manual\ndescription: Owner only.\ndisable-model-invocation: true")
    tools = agent_tools(tmp_path)
    skill_tool = [t for t in tools if t["name"] == "skill"]
    assert not skill_tool or "manual" not in skill_tool[0]["description"]


def test_asking_for_an_unknown_skill_lists_what_exists(tmp_path, monkeypatch):
    from lisan.tools import execution_tools

    _skill(tmp_path, "zine", "name: zine\ndescription: Zine.")
    monkeypatch.setattr(execution_tools, "skills_root", lambda: tmp_path)
    out = execution_tools.skill_tool(name="nope")
    assert "nope" in out and "zine" in out


# ── approval survives the rewrite ─────────────────────────────────────────────

def test_an_approval_gated_skill_is_still_gated(tmp_path):
    """Anything that leaves the machine keeps its gate through the format
    change."""
    _executable(tmp_path, "sender", requires_approval=True)
    denied = load_skill_handlers(tmp_path, vault=tmp_path, config={}, approval_fn=lambda n, a: False)
    assert "did not run" in denied["sender"](q="x")
    allowed = load_skill_handlers(tmp_path, vault=tmp_path, config={}, approval_fn=lambda n, a: True)
    assert allowed["sender"](q="x") == "ran x"
