from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisan.frontmatter import load_markdown, write_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.batch_review import _pending_drafts
from lisan.tools.capture import capture_text
from lisan.tools.rebuild_index import index_single_record, open_index_connection
from lisan.tools.record_factory import new_claim
from lisan.tools.retrieval import assemble_context
from lisan.tools import memory_pipeline as mp


class IncrementalIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _conn(self) -> sqlite3.Connection:
        return open_index_connection(self.db_path)

    def test_index_single_record_upserts_file_and_fts_rows(self) -> None:
        claim = new_claim(
            vault=self.vault,
            claim_text="Person A decided to confront Bram Thursday.",
            claim_class="observation",
            owner="user",
            status="active",
            confidence=0.7,
            summary="Person A decided to confront Bram Thursday.",
        )

        conn = self._conn()
        try:
            self.assertTrue(index_single_record(claim.path, self.vault, conn))
            conn.commit()
            row = conn.execute("SELECT id, type, summary, embedding_status FROM files").fetchone()
            self.assertEqual(row["type"], "claim")
            self.assertEqual(row["summary"], "Person A decided to confront Bram Thursday.")
            self.assertEqual(row["embedding_status"], "pending")
            fts_ids = {
                str(r["id"])
                for r in conn.execute("SELECT id FROM files_fts WHERE files_fts MATCH ?", ("Bram",)).fetchall()
            }
            self.assertIn(row["id"], fts_ids)

            doc = load_markdown(claim.path)
            fm = dict(doc.frontmatter)
            fm["summary"] = "Updated Bram decision summary."
            write_markdown(claim.path, fm, doc.body)
            self.assertTrue(index_single_record(claim.path, self.vault, conn))
            conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
            updated = conn.execute("SELECT summary FROM files WHERE id = ?", (row["id"],)).fetchone()
            self.assertEqual(updated["summary"], "Updated Bram decision summary.")
            # FTS upsert (delete-then-insert): re-indexing must not duplicate the row.
            fts_rows = conn.execute(
                "SELECT COUNT(*) FROM files_fts WHERE id = ?", (row["id"],)
            ).fetchone()[0]
            self.assertEqual(fts_rows, 1)
        finally:
            conn.close()

    def test_index_single_record_skip_rules_match_rebuild(self) -> None:
        draft = self.vault / "drafts" / "plain-draft.md"
        needs_revision = self.vault / "drafts" / "needs_revision-draft.md"
        transcript = self.vault / "transcripts" / "2026-06-15.md"
        write_markdown(
            draft,
            {"id": "draft.plain", "type": "draft", "created": "2026-06-15", "updated": "2026-06-15", "status": "pending", "summary": "Plain draft"},
            "Draft body",
        )
        write_markdown(
            needs_revision,
            {"id": "draft.needs_revision", "type": "draft", "created": "2026-06-15", "updated": "2026-06-15", "status": "needs_revision", "summary": "Needs revision draft"},
            "Draft body",
        )
        write_markdown(
            transcript,
            {"id": "transcript.today", "type": "transcript", "created": "2026-06-15", "updated": "2026-06-15", "status": "active", "summary": "Transcript"},
            "Transcript body",
        )

        conn = self._conn()
        try:
            self.assertFalse(index_single_record(draft, self.vault, conn))
            self.assertTrue(index_single_record(needs_revision, self.vault, conn))
            self.assertFalse(index_single_record(transcript, self.vault, conn))
            conn.commit()
            rows = conn.execute("SELECT id FROM files").fetchall()
            self.assertEqual([str(row["id"]) for row in rows], ["draft.needs_revision"])
        finally:
            conn.close()

    def test_capture_decision_is_retrievable_without_manual_sync(self) -> None:
        listener = {
            "worth_remembering": True,
            "mode": "extraction",
            "reason": ["memory-worthy"],
            "memory_events": [],
            "action": "full",
            "score": 9,
            "seed_score": 8,
            "narrative_score": 1,
            "memory_type": "decision",
        }
        writer = {
            "record_type": "decision",
            "summary": "Person A decided to confront Bram Thursday.",
            "significance": "medium",
            "frontmatter": {
                "summary": "Person A decided to confront Bram Thursday.",
                "domain_primary": "relational",
                "confidence": "low",
                "confidence_basis": "test writer",
            },
            "sections": {"decision": "Person A decided to confront Bram Thursday."},
            "questions": [],
            "significance_rationale": "test",
            "entities_to_create": [],
            "evidence_to_create": [],
            "claims_to_create": [],
            "state_updates": [],
            "open_loops_to_create": [],
            "decisions_to_create": [],
        }
        skeptic = {"approved": True, "recommended_action": "approve"}
        interlocutor = {
            "response": "Got it.",
            "questions": [],
            "recommended_action": "auto_commit",
            "updated_narrative_state": {"next_step": "Continue", "mode_status": "developing"},
        }

        with (
            patch.object(mp.AssemblerAgent, "run", return_value=SimpleNamespace(text="context")),
            patch.object(mp.ListenerAgent, "run_json", return_value=listener),
            patch.object(mp.WriterAgent, "run_json", return_value=writer),
            patch.object(mp.SkepticAgent, "run_json", return_value=skeptic),
            patch.object(mp.InterlocutorAgent, "run_json", return_value=interlocutor),
        ):
            result = capture_text(
                vault=self.vault,
                text="I decided to confront Bram Thursday.",
                conversation_id="demo",
                queue_background=False,
                db_path=self.db_path,
            )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT type, summary, embedding_status FROM files WHERE type = 'decision'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["embedding_status"], "pending")
        finally:
            conn.close()

        context = assemble_context("remind me what I decided about Bram", vault=self.vault, db_path=self.db_path)
        self.assertIn("Person A decided to confront Bram Thursday.", context)
        draft_doc = load_markdown(Path(result["draft_path"]))
        self.assertEqual(draft_doc.frontmatter.get("status"), "fanout_applied")
        self.assertEqual(_pending_drafts(self.vault), [])


if __name__ == "__main__":
    unittest.main()
