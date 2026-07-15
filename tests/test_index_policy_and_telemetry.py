"""The 2026-07-15 lock collision, closed from both sides.

A post-turn 'index.rebuild_record' job — which the dispatcher runs as a
FULL vault rebuild — held the database writer for ~2 minutes while a
concurrent chat turn tried to log telemetry, timed out, and died inside
its own `finally`. Two invariants close the class:

1. The post-turn planner never queues a full rebuild: records index at
   write time, so only the incremental embed pass follows a turn; the
   full rebuild is a daily consistency net at the back of the queue.
2. Telemetry never fails the turn it describes: trace persistence and
   the LLM call log are strictly best-effort.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.job_policy import which_jobs_for_turn
from lisan.tools.jobs import enqueue_job, get_job, mark_job_succeeded


class PostTurnIndexPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "jobs.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def _metadata(self) -> dict:
        return {
            "vault": str(self.vault),
            "conversation_id": "c1",
            "text": "a substantial turn about the family schedule this week",
            "action": "full",
            "mode": "structured",
            "records_written": 2,
        }

    def test_record_writing_turn_queues_embed_not_full_rebuild(self):
        jobs = {j["job_type"] for j in which_jobs_for_turn(self._metadata(), db_path=self.db)}
        self.assertIn("index.embed_pending", jobs)
        self.assertNotIn("index.rebuild_record", jobs)

    def test_daily_rebuild_net_queues_once_then_rests(self):
        specs = which_jobs_for_turn(self._metadata(), db_path=self.db)
        rebuilds = [j for j in specs if j["job_type"] == "index.rebuild_all"]
        self.assertEqual(len(rebuilds), 1)
        # Back of the queue: user work and maintenance outrank it.
        self.assertEqual(rebuilds[0]["priority"], 95)
        # Mark one successful run; the net must rest for 24h.
        job_id = enqueue_job("index.rebuild_all", {"vault": str(self.vault)}, db_path=self.db)
        claimed = get_job(job_id, db_path=self.db)
        mark_job_succeeded(claimed["id"], result={}, db_path=self.db)
        jobs = {j["job_type"] for j in which_jobs_for_turn(self._metadata(), db_path=self.db)}
        self.assertNotIn("index.rebuild_all", jobs)


class TelemetryNeverFailsTheTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_trace_persist_failure_returns_the_trace(self):
        import sqlite3

        from lisan.tools import tracing

        trace, token = tracing.start_turn_trace("turn.test", "hello", "advice", False)
        try:
            with patch.object(tracing, "_persist_trace", side_effect=sqlite3.OperationalError("database is locked")):
                finalized = tracing.finalize_turn_trace(trace, db_path=self.root / "x.sqlite", vault=self.vault)
            self.assertIsNotNone(finalized.summary())
        finally:
            tracing.reset_current_turn_trace(token)

    def test_llm_call_log_failure_is_swallowed(self):
        import sqlite3

        from lisan.providers import base as providers_base

        with patch.object(
            providers_base, "_write_call_log",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            providers_base._log_call(
                self.root / "x.sqlite", "writer", "rotato", "m", "v1",
                None, None, "s1", 100, True,
            )  # must not raise


if __name__ == "__main__":
    unittest.main()
