"""WO-GROUND Seam A: detected self-questions carry a GROUND_TRUTH block.

Definition-of-done test: with a stale self-claim in the vault AND a healthy
live state, a "what's your status?" turn must put generated ground truth in
front of the model before it answers. We mock the model and assert on the
block, not the wording.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.conversation import run_conversation_turn
from lisan.tools.jobs import enqueue_job, get_job, mark_job_failed
from lisan.tools.self_model import render_self_state, snapshot_self_state


class GroundTruthInjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "jobs.sqlite"
        self.seen_kwargs: dict = {}

    def tearDown(self):
        self.tmp.cleanup()

    def _turn(self, text: str) -> dict:
        def _capture(agent_self, user_input, **kwargs):
            self.seen_kwargs = kwargs
            return {"response": "ok"}

        with patch("lisan.agents.conversation.ConversationAgent.run_json", _capture):
            return run_conversation_turn(
                vault=self.vault,
                text=text,
                conversation_id="test-1",
                db_path=self.db,
                queue_capture=False,
            )

    def test_status_question_arrives_with_live_state(self):
        # A stale self-claim sits in the vault; the model must still see the
        # live snapshot, injected without it asking.
        claims = self.vault / "claims"
        claims.mkdir(parents=True, exist_ok=True)
        (claims / "2026-07-05-stale.md").write_text(
            "---\n{\"type\": \"claim\", \"summary\": "
            "\"The task processor is stalled due to a database issue.\"}\n---\n"
            "# Stale self-claim\n",
            encoding="utf-8",
        )
        self._turn("what's your current system status?")
        block = self.seen_kwargs.get("ground_truth")
        self.assertIsNotNone(block, "self-question must inject GROUND_TRUTH")
        self.assertIn("LIVE SELF-STATE", block)
        self.assertIn("history", block)

    def test_ordinary_turn_injects_nothing(self):
        self._turn("Ruth took the girls to the coast this weekend")
        self.assertIsNone(self.seen_kwargs.get("ground_truth"))

    def test_failing_series_caution_reaches_the_block(self):
        # The 2026-07-14 gap: 'scheduled correctly' said over a series with
        # eight terminal failures. The snapshot now carries the record.
        for _ in range(3):
            job_id = enqueue_job(
                "task.prompt", {"prompt": "one thing"}, max_attempts=1, db_path=self.db,
            )
            mark_job_failed(job_id, "boom", retry=False, db_path=self.db)
        enqueue_job(
            "task.prompt", {"prompt": "one thing"},
            scheduled_for="2099-01-01T00:00:00Z", db_path=self.db,
        )
        rendered = render_self_state(snapshot_self_state(self.vault, self.db))
        self.assertIn("CAUTION", rendered)
        self.assertIn("terminal", rendered)

        self._turn("is my daily prompt scheduled correctly?")
        block = self.seen_kwargs.get("ground_truth")
        self.assertIsNotNone(block)
        self.assertIn("CAUTION", block)


if __name__ == "__main__":
    unittest.main()
