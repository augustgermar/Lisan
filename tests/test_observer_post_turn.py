"""Gate: the observer capture path plans post-turn maintenance jobs.

Between 2026-07-05 and 2026-07-15 the analyst, dreamer, weekly self-eval,
and daily deviation scan never ran once — which_jobs_for_turn was only
called from the legacy capture_text path, and the live capture.observe
path bypassed it. The drives layer must hang off BOTH capture paths.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.jobs import dispatch_job, list_jobs
from lisan.tools.memory_pipeline import MemoryPipelineResult


class ObserverPostTurnJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "jobs.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_result(self, action: str = "full") -> MemoryPipelineResult:
        return MemoryPipelineResult(
            transcript_path=self.vault / "transcripts" / "2026-07-15.md",
            draft_path=None,
            listener={"action": action, "mode": "structured"},
            writer={},
            skeptic=None,
            interlocutor=None,
            action=action,
            mode="structured",
        )

    def _observe_job(self) -> dict:
        return {
            "id": "job.test",
            "job_type": "capture.observe",
            "payload": {
                "vault": str(self.vault),
                "text": "a substantial turn about the user's ongoing plans",
                "response": "Noted and recorded.",
                "conversation_id": "telegram-42-2026-07-15",
            },
        }

    def test_observe_plans_maintenance_drives(self):
        with patch("lisan.tools.memory_pipeline.run_memory_pipeline", return_value=self._fake_result()):
            out = dispatch_job(self._observe_job(), vault=self.vault, db_path=self.db)
        queued_types = {j["job_type"] for j in list_jobs(status="queued", db_path=self.db, limit=100)}
        # On a fresh database every never-ran drive is due immediately.
        for expected in ("dreamer.maintenance", "self.evaluate", "deviation.scan"):
            self.assertIn(expected, queued_types)
        self.assertTrue(out["post_turn_jobs_queued"])

    def test_skipped_turn_plans_nothing(self):
        with patch("lisan.tools.memory_pipeline.run_memory_pipeline", return_value=self._fake_result(action="skip")):
            out = dispatch_job(self._observe_job(), vault=self.vault, db_path=self.db)
        self.assertEqual(out["post_turn_jobs_queued"], [])
        self.assertEqual(list_jobs(status="queued", db_path=self.db), [])


if __name__ == "__main__":
    unittest.main()
