from __future__ import annotations

from lisan.frontmatter import write_markdown
from lisan.tools.entity_story import _condense, _read_draft_body
from lisan.tools.memory_pipeline import _render_draft_body


def test_story_material_is_the_source_text_not_agent_json(tmp_path):
    body = _render_draft_body(
        "Robin finally got the job at the observatory.",
        {"action": "full", "reason": ["x" * 2000]},
        {"summary": "y" * 2000},
        {"approved": True},
        {"response": "z"},
        "episode",
        True,
    )
    draft = tmp_path / "draft.md"
    write_markdown(draft, {"id": "draft.x"}, body)
    material = _read_draft_body(draft)
    assert material == "Robin finally got the job at the observatory."
    assert "Listener" not in _condense(material)


def test_drafts_without_a_source_section_still_work(tmp_path):
    draft = tmp_path / "draft.md"
    write_markdown(draft, {"id": "draft.y"}, "# Note\n\nPlain words.")
    assert _read_draft_body(draft) == "# Note\n\nPlain words."
