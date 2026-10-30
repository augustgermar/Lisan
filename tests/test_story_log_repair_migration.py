from __future__ import annotations

import json

import pytest

from lisan.frontmatter import load_markdown, write_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.entity_story import _condense
from lisan.tools.memory_pipeline import _render_draft_body
from lisan.tools.story_log_repair_migration import (
    MigrationError,
    apply_prepared_migration,
    prepare_migration,
    rollback_migration,
)


def _vault(tmp_path):
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    body = _render_draft_body("Robin started at the observatory.", {"action": "full", "memory_type": "episode"},
                              {"summary": "s"}, {}, {}, "episode", True)
    write_markdown(vault / "drafts" / "2026-08-01-abc-robin.md", {"id": "draft.robin"}, body)
    polluted_from_draft = _condense(body)
    polluted_orphan = ('# Memory Draft ## Status Skeptic approved ## Writer ```json { "record_type": "episode", '
                       '"summary": "Robin adopted a dog named Comet.", "significance": "low"')
    entity = vault / "entities" / "people" / "robin.md"
    write_markdown(entity, {"id": "entity.robin", "type": "entity", "source_log": [
        {"date": "2026-08-01", "text": polluted_from_draft, "folded": True},
        {"date": "2026-08-02", "text": polluted_orphan, "folded": False},
        {"date": "2026-08-03", "text": "Robin is fine.", "folded": False},
    ]}, "# Robin\n")
    return vault, entity


def test_prepare_apply_and_rollback(tmp_path):
    vault, entity = _vault(tmp_path)
    original = entity.read_text(encoding="utf-8")
    bundle = tmp_path / "bundle"
    record = prepare_migration(vault, bundle)
    assert record["repaired_entries"] == {"draft": 1, "writer_summary": 1, "unrecoverable": 0}
    assert entity.read_text(encoding="utf-8") == original  # prepare touches nothing
    apply_prepared_migration(vault, bundle)
    log = load_markdown(entity).frontmatter["source_log"]
    assert [e["text"] for e in log] == ["Robin started at the observatory.", "Robin adopted a dog named Comet.",
                                        "Robin is fine."]
    assert log[0]["folded"] is True and log[0]["repaired"].endswith("/draft")
    assert "repaired" not in log[2]
    rollback_migration(vault, bundle)
    assert entity.read_text(encoding="utf-8") == original


def test_apply_refuses_if_a_file_changed_since_prepare(tmp_path):
    vault, entity = _vault(tmp_path)
    bundle = tmp_path / "bundle"
    prepare_migration(vault, bundle)
    entity.write_text(entity.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="changed since prepare"):
        apply_prepared_migration(vault, bundle)
    assert json.loads((bundle / "migration.json").read_text())["state"] == "prepared"
