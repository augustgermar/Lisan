"""WO-GROUND Seam B definition-of-done: the 2026-07-06 scenario, replayed.

The poison record — "the task processor is stalled" stored the night
before, retrieved the next morning, narrated as current fact — must now
arrive at the model wearing its self-report banner, generated at the
rendering layer. Both the class-stamped modern form and a legacy record
that predates the self_report class get the banner.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import load_markdown, write_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.rebuild_index import rebuild_index
from lisan.tools.record_factory import new_claim
from lisan.tools.retrieval import assemble_context


class SelfReportBannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "lisan.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_poison_claim(self) -> Path:
        created = new_claim(
            self.vault,
            "The reminder failure was caused by a stalled task processor due to a database issue.",
            owner="agent",
            claim_class="observation",
            confidence=1.0,
            confidence_basis="direct observation of its internal logs",
        )
        return created.path

    def test_reseeded_poison_claim_arrives_wearing_the_banner(self):
        self._seed_poison_claim()
        rebuild_index(vault=self.vault, db_path=self.db)
        context = assemble_context(
            "is the task processor stalled? reminder failure database",
            vault=self.vault,
            db_path=self.db,
        )
        self.assertIn("stalled task processor", context)
        self.assertIn("[agent self-report from", context)
        self.assertIn("for current state, self_state]", context)

    def test_legacy_record_without_the_class_still_gets_the_banner(self):
        # A claim written before the self_report class existed: owner agent,
        # operational subject, class 'observation' on disk. The rendering
        # layer banners it anyway.
        path = self._seed_poison_claim()
        doc = load_markdown(path)
        fm = dict(doc.frontmatter)
        fm["claim_class"] = "observation"  # simulate the pre-fix vault
        write_markdown(path, fm, doc.body)
        rebuild_index(vault=self.vault, db_path=self.db)
        context = assemble_context(
            "is the task processor stalled? reminder failure database",
            vault=self.vault,
            db_path=self.db,
        )
        self.assertIn("stalled task processor", context)
        self.assertIn("[agent self-report from", context)

    def test_user_claims_carry_no_banner(self):
        new_claim(
            self.vault,
            "The user prefers coffee before any serious conversation.",
            owner="user",
            claim_class="observation",
            confidence=0.9,
        )
        rebuild_index(vault=self.vault, db_path=self.db)
        context = assemble_context(
            "coffee serious conversation preference",
            vault=self.vault,
            db_path=self.db,
        )
        self.assertIn("coffee", context)
        self.assertNotIn("[agent self-report from", context)


if __name__ == "__main__":
    unittest.main()
