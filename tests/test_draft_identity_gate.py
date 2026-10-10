from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.tools.drafts import _promote_to_entity


class DraftPersonIdentityGateTests(unittest.TestCase):
    def test_candidate_queue_records_are_not_added_to_memory_index(self) -> None:
        from lisan.tools.identity_quarantine import assess_person_identity, quarantine_identity_candidate
        from lisan.tools.rebuild_index import index_single_record, open_index_connection

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            evidence = assess_person_identity("Ruth Varga", "Ruth is my daughter.")
            candidate = quarantine_identity_candidate(
                vault, name="Ruth Varga", summary="candidate", source_ref="drafts/a.md",
                evidence=evidence, reason="insufficient_identity_evidence",
            )
            conn = open_index_connection(vault / "index.sqlite")
            try:
                self.assertFalse(index_single_record(candidate, vault, conn))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
            finally:
                conn.close()

    def test_weak_person_draft_is_quarantined_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            with self.assertRaisesRegex(ValueError, "candidate queued"):
                _promote_to_entity(
                    vault, {"subtype": "person"}, "Ruth Varga",
                    "Ruth is the project coordinator.", source_ref="drafts/ruth.md",
                )
            self.assertEqual(list((vault / "entities" / "people").glob("*.md")), [])
            queued = list((vault / "quarantine" / "identity-candidates").glob("*.md"))
            self.assertEqual(len(queued), 1)
            self.assertEqual(load_markdown(queued[0]).frontmatter["candidate_name"], "Ruth Varga")

    def test_full_name_and_corroborating_signal_promote_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            created = _promote_to_entity(
                vault, {"subtype": "person"}, "Ruth Varga",
                "Ruth Varga is my daughter.", source_ref="drafts/ruth.md",
            )
            self.assertTrue(created.exists())
            log = (vault / "quarantine" / "identity-candidates" / "decisions.jsonl").read_text()
            self.assertIn('"decision": "mint"', log)
            self.assertIn("Ruth Varga is my daughter", log)


if __name__ == "__main__":
    unittest.main()
