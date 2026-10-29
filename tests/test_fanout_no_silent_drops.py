"""Writer output the prompts ask for must reach a record, not vanish."""
from __future__ import annotations

from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.memory_pipeline import _merge_writer_outputs
from lisan.tools.record_fanout import fanout_knowledge, fanout_open_loops


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    return vault


def test_artifact_behavioural_contracts_survive_the_merge():
    merged = _merge_writer_outputs(
        {"summary": "s", "behavioral_contracts": ["Use ISO dates."]},
        {"entities_to_create": [], "behavioral_contracts": ["Stop using bullet points.", "Use ISO dates."]},
    )
    assert merged["behavioral_contracts"] == ["Use ISO dates.", "Stop using bullet points."]


def test_open_loop_writer_output_becomes_a_loop(tmp_path):
    vault = _vault(tmp_path)
    writer = {
        "record_type": "open_loop",
        "summary": "Renew the car registration",
        "frontmatter": {"next_action": "Pay the renewal fee online", "priority": "high", "owner": "user"},
        "sections": {"open_loop": "Registration expires next month."},
    }
    fanout_open_loops(vault, writer, "drafts/x.md")
    loops = list((vault / "open_loops").glob("*.md"))
    assert len(loops) == 1
    fm = load_markdown(loops[0]).frontmatter
    assert fm["next_action"] == "Pay the renewal fee online"


def test_a_loop_owned_by_the_principal_token_is_the_users(tmp_path):
    vault = _vault(tmp_path)
    writer = {"open_loops_to_create": [
        {"title": "Book dentist", "next_action": "Call the office", "owner": "{{principal}}"},
    ]}
    fanout_open_loops(vault, writer, "drafts/x.md")
    assert len(list((vault / "open_loops").glob("*.md"))) == 1


def test_someone_elses_loop_is_still_refused(tmp_path):
    vault = _vault(tmp_path)
    writer = {"open_loops_to_create": [
        {"title": "Decide on job", "next_action": "Decide", "owner": "Robin"},
    ]}
    fanout_open_loops(vault, writer, "drafts/x.md")
    assert list((vault / "open_loops").glob("*.md")) == []


def test_knowledge_writer_output_becomes_a_knowledge_record(tmp_path):
    vault = _vault(tmp_path)
    writer = {
        "record_type": "knowledge",
        "summary": "Pool chemistry targets",
        "significance": "medium",
        "frontmatter": {"category": "technical", "confidence": "high"},
        "sections": {"knowledge": "Free chlorine 1-3 ppm; pH 7.4-7.6."},
    }
    path = fanout_knowledge(vault, writer, "drafts/k.md")
    assert path is not None and path.exists()
    doc = load_markdown(path)
    assert doc.frontmatter["type"] == "knowledge"
    assert "pH 7.4-7.6" in doc.body


def test_new_knowledge_on_the_same_subject_appends_instead_of_vanishing(tmp_path):
    vault = _vault(tmp_path)
    base = {"record_type": "knowledge", "summary": "Pool chemistry targets",
            "frontmatter": {"category": "technical"}}
    first = fanout_knowledge(vault, {**base, "sections": {"knowledge": "pH 7.4-7.6."}}, "drafts/a.md")
    second = fanout_knowledge(vault, {**base, "sections": {"knowledge": "Shock weekly in summer."}}, "drafts/b.md")
    assert first == second
    body = load_markdown(first).body
    assert "pH 7.4-7.6." in body and "Shock weekly in summer." in body
    # an identical repeat does not duplicate
    fanout_knowledge(vault, {**base, "sections": {"knowledge": "Shock weekly in summer."}}, "drafts/c.md")
    assert load_markdown(first).body.count("Shock weekly in summer.") == 1
