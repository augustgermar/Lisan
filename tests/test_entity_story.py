"""Tests for the entity story rewrite job (entity.rewrite_story).

Acceptance criteria:
- D3: correction material updates the entity story with arc preservation
- C2: rewrite produces richer content (non-trivial narrative)
- Coalescing: 5 entity.rewrite_story enqueues for the same entity → 1 queued job
- No-bypass: tokenize_principal is called — {{principal}} token used in output
- Async: entity.rewrite_story is in JOB_TYPES but NOT in INDEX_JOB_TYPES
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lisan.frontmatter import dump_markdown, load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.jobs import INDEX_JOB_TYPES, JOB_TYPES, enqueue_job, list_jobs
from lisan.tools.job_policy import coalesce_key_for_job, priority_for_job_type


def _make_vault() -> tuple[tempfile.TemporaryDirectory, Path, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    ensure_repo_layout(root)
    vault = vault_root(root)
    db_path = root / "lisan.sqlite"
    return tmp, vault, db_path


def _seed_entity(vault: Path, slug: str, canonical: str, summary: str = "") -> Path:
    path = vault / "entities" / "people" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "id": f"entity.{slug}",
        "type": "entity",
        "subtype": "person",
        "kind": "person",
        "canonical_name": canonical,
        "aliases": [],
        "summary": summary or f"{canonical} is a person.",
        "significance": "medium",
        "confidence": "low",
        "confidence_basis": "test seed",
        "created": "2026-06-24",
        "updated": "2026-06-24",
        "status": "active",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "private",
        "review_after": "2026-06-24",
        "last_confirmed": "2026-06-24",
        "epoch": 1,
        "epoch_started": "2026-06-24",
        "previous_epochs": [],
        "links": [],
    }
    body = f"# {canonical}\n\n{summary or f'{canonical} is a person.'}\n"
    path.write_text(dump_markdown(fm, body), encoding="utf-8")
    return path


class AsyncJobTypeTests(unittest.TestCase):
    """entity.rewrite_story must be async (in JOB_TYPES, not INDEX_JOB_TYPES)."""

    def test_in_job_types(self) -> None:
        self.assertIn("entity.rewrite_story", JOB_TYPES)

    def test_not_in_index_job_types(self) -> None:
        self.assertNotIn("entity.rewrite_story", INDEX_JOB_TYPES)


class CoalesceTests(unittest.TestCase):
    """5 enqueues for the same entity → 1 queued job."""

    def setUp(self) -> None:
        self.tmp, self.vault, self.db_path = _make_vault()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_coalesce_key_by_entity_id(self) -> None:
        key = coalesce_key_for_job(
            "entity.rewrite_story",
            {"entity_id": "entity.maya", "vault": str(self.vault)},
        )
        self.assertEqual(key, "entity.rewrite_story|entity_id=entity.maya")

    def test_coalesce_key_falls_back_to_entity_path(self) -> None:
        key = coalesce_key_for_job(
            "entity.rewrite_story",
            {"entity_path": "/vault/entities/people/maya.md"},
        )
        self.assertEqual(key, "entity.rewrite_story|entity_id=/vault/entities/people/maya.md")

    def test_five_enqueues_produce_one_job(self) -> None:
        payload = {
            "vault": str(self.vault),
            "entity_id": "entity.maya",
            "entity_path": str(self.vault / "entities" / "people" / "maya.md"),
        }
        for _ in range(5):
            enqueue_job("entity.rewrite_story", payload, db_path=self.db_path)
        all_queued = list_jobs(db_path=self.db_path, status="queued")
        queued = [j for j in all_queued if j.get("job_type") == "entity.rewrite_story"]
        self.assertEqual(len(queued), 1, "5 enqueues for same entity_id should coalesce to 1 job")

    def test_different_entities_do_not_coalesce(self) -> None:
        for name in ("entity.maya", "entity.bob"):
            enqueue_job(
                "entity.rewrite_story",
                {"vault": str(self.vault), "entity_id": name},
                db_path=self.db_path,
            )
        all_queued = list_jobs(db_path=self.db_path, status="queued")
        queued = [j for j in all_queued if j.get("job_type") == "entity.rewrite_story"]
        self.assertEqual(len(queued), 2)


class PriorityTests(unittest.TestCase):
    def test_priority_is_background(self) -> None:
        prio = priority_for_job_type("entity.rewrite_story")
        # Must be higher number (lower urgency) than analyst.scan (70) but
        # lower urgency than manifest.regenerate (90).
        self.assertGreater(prio, 70)
        self.assertLess(prio, 90)


class RewriteEntityStoryTests(unittest.TestCase):
    """D3 + no-bypass + C2: the rewrite writes richer content through tokenize_principal."""

    def setUp(self) -> None:
        self.tmp, self.vault, self.db_path = _make_vault()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _mock_llm_response(self, narrative: str, arc_note: str) -> dict:
        return {"narrative": narrative, "arc_note": arc_note}

    def test_rewrite_updates_entity_body(self) -> None:
        entity_path = _seed_entity(self.vault, "maya", "Maya Smith", "Maya is a colleague.")

        # Draft episode with new material
        draft_path = self.vault / "drafts" / "test-draft.md"
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_body = "Maya and I worked on the project roadmap."
        draft_fm = {"id": "draft.test", "type": "draft", "created": "2026-06-24", "updated": "2026-06-24"}
        draft_path.write_text(dump_markdown(draft_fm, draft_body), encoding="utf-8")

        new_narrative = "Maya Smith is a colleague of {{principal}}. They worked together on the project roadmap in June 2026."
        mock_result = self._mock_llm_response(new_narrative, "Added roadmap collaboration detail.")

        with patch("lisan.agents.writer.WriterAgent") as MockWriter:
            instance = MagicMock()
            instance.run_json.return_value = mock_result
            MockWriter.return_value = instance

            from lisan.tools.entity_story import rewrite_entity_story
            result = rewrite_entity_story(
                vault=self.vault,
                entity_path=entity_path,
                draft_path=draft_path,
                db_path=self.db_path,
            )

        self.assertTrue(result["updated"])
        doc = load_markdown(entity_path)
        self.assertIn("roadmap", doc.body)
        self.assertIn("Maya Smith", doc.body)

    def test_no_bypass_tokenize_principal_called(self) -> None:
        """rewrite_entity_story must route the narrative through tokenize_principal.

        We mock tokenize_principal to inject the {{principal}} token and verify
        the stored body reflects the tokenized text — not the raw writer output.
        """
        entity_path = _seed_entity(self.vault, "maya", "Maya Smith")

        draft_path = self.vault / "drafts" / "draft2.md"
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_fm = {"id": "draft.test2", "type": "draft", "created": "2026-06-24", "updated": "2026-06-24"}
        draft_path.write_text(dump_markdown(draft_fm, "Maya talked to Jane about work."), encoding="utf-8")

        raw_narrative = "Maya Smith is a colleague of Jane."
        tokenized_narrative = "Maya Smith is a colleague of {{principal}}."

        with patch("lisan.agents.writer.WriterAgent") as MockWriter, \
             patch("lisan.tools.entity_story.tokenize_principal", return_value=tokenized_narrative) as mock_tok:
            instance = MagicMock()
            instance.run_json.return_value = {"narrative": raw_narrative, "arc_note": "test"}
            MockWriter.return_value = instance

            from lisan.tools.entity_story import rewrite_entity_story
            result = rewrite_entity_story(
                vault=self.vault,
                entity_path=entity_path,
                draft_path=draft_path,
                db_path=self.db_path,
            )

        # tokenize_principal must have been called with the raw narrative
        mock_tok.assert_called_once_with(raw_narrative, self.vault)

        # The stored body must contain the tokenized version, not the raw one
        doc = load_markdown(entity_path)
        self.assertIn("{{principal}}", doc.body)
        self.assertNotIn("Jane.", doc.body)

    def test_missing_entity_path_returns_updated_false(self) -> None:
        from lisan.tools.entity_story import rewrite_entity_story
        result = rewrite_entity_story(
            vault=self.vault,
            entity_path=self.vault / "entities" / "people" / "nonexistent.md",
        )
        self.assertFalse(result["updated"])

    def test_no_new_material_returns_updated_false(self) -> None:
        entity_path = _seed_entity(self.vault, "bob", "Bob Jones")

        from lisan.tools.entity_story import rewrite_entity_story
        result = rewrite_entity_story(
            vault=self.vault,
            entity_path=entity_path,
            draft_path=None,
            transcript_path=None,
        )
        self.assertFalse(result["updated"])

    def test_empty_narrative_from_writer_returns_updated_false(self) -> None:
        entity_path = _seed_entity(self.vault, "carol", "Carol White")
        draft_path = self.vault / "drafts" / "draft3.md"
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_fm = {"id": "draft.test3", "type": "draft", "created": "2026-06-24", "updated": "2026-06-24"}
        draft_path.write_text(dump_markdown(draft_fm, "Carol was mentioned."), encoding="utf-8")

        with patch("lisan.agents.writer.WriterAgent") as MockWriter:
            instance = MagicMock()
            instance.run_json.return_value = {"narrative": "", "arc_note": ""}
            MockWriter.return_value = instance

            from lisan.tools.entity_story import rewrite_entity_story
            result = rewrite_entity_story(
                vault=self.vault,
                entity_path=entity_path,
                draft_path=draft_path,
            )
        self.assertFalse(result["updated"])


class EntitesTouchedTests(unittest.TestCase):
    """_create_entity_stubs returns a list of entity paths for all processed entities."""

    def setUp(self) -> None:
        self.tmp, self.vault, self.db_path = _make_vault()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_new_entity_appears_in_entities_touched(self) -> None:
        from lisan.tools.rebuild_index import open_index_connection
        from lisan.tools.memory_pipeline import _create_entity_stubs

        writer = {
            "entities_to_create": [
                {"name": "Olivia Chen", "kind": "person", "summary": "A colleague.", "confidence_basis": "test"}
            ]
        }
        conn = open_index_connection(self.db_path)
        try:
            touched = _create_entity_stubs(
                self.vault, writer, "drafts/test.md", "Olivia Chen is a colleague.",
                index_conn=conn,
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(len(touched), 1)
        self.assertTrue(touched[0].exists())
        self.assertIn("olivia-chen", touched[0].name)

    def test_existing_entity_appears_in_entities_touched(self) -> None:
        from lisan.tools.rebuild_index import open_index_connection
        from lisan.tools.memory_pipeline import _create_entity_stubs

        entity_path = _seed_entity(self.vault, "diana-ross", "Diana Ross", "Diana is a friend.")

        writer = {
            "entities_to_create": [
                {"name": "Diana Ross", "kind": "person", "summary": "Diana is a friend.", "confidence_basis": "test"}
            ]
        }
        conn = open_index_connection(self.db_path)
        try:
            touched = _create_entity_stubs(
                self.vault, writer, "drafts/test.md", "Diana Ross came over.",
                index_conn=conn,
            )
            conn.commit()
        finally:
            conn.close()

        # Existing entity's path should be in the touched list
        self.assertTrue(any(p == entity_path or p.stem == "diana-ross" for p in touched))

    def test_empty_entities_to_create_returns_empty_list(self) -> None:
        from lisan.tools.rebuild_index import open_index_connection
        from lisan.tools.memory_pipeline import _create_entity_stubs

        conn = open_index_connection(self.db_path)
        try:
            touched = _create_entity_stubs(
                self.vault, {"entities_to_create": []}, "drafts/test.md", "No entities here.",
                index_conn=conn,
            )
        finally:
            conn.close()

        self.assertEqual(touched, [])


if __name__ == "__main__":
    unittest.main()
