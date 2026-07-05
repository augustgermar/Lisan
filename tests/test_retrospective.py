"""The retrospective sweep: transcript vs observe-ledger diff, stateless and
idempotent — an exchange that never got its observe job gets one; covered
exchanges are never re-captured."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lisan.tools.jobs import ensure_jobs_table
from lisan.tools.retrospective import sweep_missed_captures
from lisan.tools.transcripts import append_transcript


class RetrospectiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "transcripts").mkdir(parents=True)
        self.db = Path(self.tmp.name) / "lisan.sqlite"
        conn = sqlite3.connect(self.db)
        ensure_jobs_table(conn)
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _exchange(self, conversation: str, user: str, reply: str) -> None:
        append_transcript(vault=self.vault, conversation_id=conversation, speaker="USER", text=user)
        append_transcript(vault=self.vault, conversation_id=conversation, speaker="LISAN", text=reply)

    def _observe_jobs(self) -> list[dict]:
        conn = sqlite3.connect(self.db)
        rows = conn.execute(
            "SELECT payload_json FROM jobs WHERE job_type='capture.observe'"
        ).fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows]

    def test_missed_exchange_is_enqueued(self) -> None:
        self._exchange("c1", "Ruth is teaching me beekeeping.", "Noted — that sounds like a good project.")
        result = sweep_missed_captures(self.vault, self.db, days=2)
        self.assertEqual(result["missed"], 1)
        self.assertEqual(result["enqueued"], 1)
        jobs = self._observe_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["text"], "Ruth is teaching me beekeeping.")
        self.assertTrue(jobs[0]["retrospective"])

    def test_covered_exchange_is_not_recaptured(self) -> None:
        self._exchange("c1", "Ruth is teaching me beekeeping.", "Noted.")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO jobs (id, job_type, status, payload_json, created_at, attempts, max_attempts, priority, coalesced_count) "
            "VALUES ('j1', 'capture.observe', 'succeeded', ?, '2026-07-05T00:00:00Z', 1, 3, 100, 0)",
            (json.dumps({"conversation_id": "c1", "text": "Ruth is teaching me beekeeping.", "response": "Noted."}),),
        )
        conn.commit()
        conn.close()
        result = sweep_missed_captures(self.vault, self.db, days=2)
        self.assertEqual(result["missed"], 0)
        self.assertEqual(len(self._observe_jobs()), 1)  # only the pre-existing one

    def test_sweep_is_idempotent(self) -> None:
        self._exchange("c1", "The hive stand is finished.", "Good — one thing off the list.")
        first = sweep_missed_captures(self.vault, self.db, days=2)
        second = sweep_missed_captures(self.vault, self.db, days=2)
        self.assertEqual(first["enqueued"], 1)
        self.assertEqual(second["missed"], 0)
        self.assertEqual(len(self._observe_jobs()), 1)

    def test_unanswered_turn_is_skipped(self) -> None:
        append_transcript(vault=self.vault, conversation_id="c2", speaker="USER", text="Still there?")
        result = sweep_missed_captures(self.vault, self.db, days=2)
        self.assertEqual(result["exchanges"], 0)
        self.assertEqual(result["enqueued"], 0)

    def test_conversations_do_not_cross_pair(self) -> None:
        append_transcript(vault=self.vault, conversation_id="a", speaker="USER", text="Question in a.")
        self._exchange("b", "Question in b.", "Answer in b.")
        result = sweep_missed_captures(self.vault, self.db, days=2)
        self.assertEqual(result["exchanges"], 1)  # only b's pair; a's is still open
        self.assertEqual(self._observe_jobs()[0]["conversation_id"], "b")


if __name__ == "__main__":
    unittest.main()
