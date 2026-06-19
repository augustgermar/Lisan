"""FIX C (2026-06-19 eval): capture drains the indexing/embedding jobs it
enqueues, in-process, non-fatally, bounded to index job types (never the
LLM-heavy analyst/dreamer maintenance jobs)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.capture import _drain_index_jobs
from lisan.tools.jobs import (
    INDEX_JOB_TYPES,
    claim_next_job,
    enqueue_job,
    list_jobs,
    run_jobs_worker,
)


class ClaimFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_claim_with_job_types_filter_skips_maintenance(self) -> None:
        enqueue_job("analyst.scan", {"vault": str(self.vault)}, db_path=self.db_path)
        idx_id = enqueue_job("index.rebuild_record",
                             {"record_id": "claim.alpha", "vault": str(self.vault)},
                             db_path=self.db_path)
        claimed = claim_next_job("w", db_path=self.db_path, job_types=set(INDEX_JOB_TYPES))
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], idx_id)
        self.assertEqual(claimed["job_type"], "index.rebuild_record")
        # analyst.scan is left queued for batch/cron.
        queued_types = {j["job_type"] for j in list_jobs(status="queued", db_path=self.db_path)}
        self.assertIn("analyst.scan", queued_types)
        self.assertNotIn("index.rebuild_record", queued_types)

    def test_worker_with_job_types_leaves_maintenance_queued(self) -> None:
        enqueue_job("analyst.scan", {"vault": str(self.vault)}, db_path=self.db_path)
        enqueue_job("dreamer.maintenance", {"vault": str(self.vault), "task": "compress"}, db_path=self.db_path)
        enqueue_job("index.rebuild_record", {"record_id": "claim.alpha", "vault": str(self.vault)}, db_path=self.db_path)

        # Dispatch is mocked so the test does not depend on index internals; the
        # point is *which* jobs the filtered worker claims.
        with patch("lisan.tools.jobs.dispatch_job", return_value={"ok": True}):
            result = run_jobs_worker(vault=self.vault, db_path=self.db_path,
                                     job_types=set(INDEX_JOB_TYPES))
        self.assertEqual(result["processed_count"], 1)
        self.assertEqual(result["success_count"], 1)
        queued_types = {j["job_type"] for j in list_jobs(status="queued", db_path=self.db_path)}
        self.assertEqual(queued_types, {"analyst.scan", "dreamer.maintenance"})


class DrainHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_drain_runs_only_for_index_jobs(self) -> None:
        queued = [{"job_type": "index.rebuild_record", "job_id": "j1"}]
        with patch("lisan.tools.jobs.run_jobs_worker") as worker:
            worker.return_value = {"processed_count": 1, "success_count": 1, "failure_count": 0}
            out = _drain_index_jobs(vault=self.vault, db_path=self.db_path, provider=None,
                                    model=None, queued_jobs=queued, drain_jobs=True)
        self.assertTrue(out["drained"])
        self.assertEqual(out["processed_count"], 1)
        # The worker was scoped to index job types only.
        _, kwargs = worker.call_args
        self.assertEqual(kwargs["job_types"], set(INDEX_JOB_TYPES))

    def test_drain_skipped_when_no_index_jobs(self) -> None:
        queued = [{"job_type": "analyst.scan", "job_id": "j1"}]
        with patch("lisan.tools.jobs.run_jobs_worker") as worker:
            out = _drain_index_jobs(vault=self.vault, db_path=self.db_path, provider=None,
                                    model=None, queued_jobs=queued, drain_jobs=True)
        self.assertFalse(out["drained"])
        self.assertEqual(out["reason"], "no_index_jobs")
        worker.assert_not_called()

    def test_drain_disabled_by_flag(self) -> None:
        queued = [{"job_type": "index.rebuild_record", "job_id": "j1"}]
        with patch("lisan.tools.jobs.run_jobs_worker") as worker:
            out = _drain_index_jobs(vault=self.vault, db_path=self.db_path, provider=None,
                                    model=None, queued_jobs=queued, drain_jobs=False)
        self.assertFalse(out["drained"])
        self.assertEqual(out["reason"], "disabled")
        worker.assert_not_called()

    def test_drain_is_non_fatal(self) -> None:
        """An embedder/index failure during drain must never propagate out of
        capture — it is logged and the job stays queued for the next drain."""
        queued = [{"job_type": "index.rebuild_record", "job_id": "j1"}]
        with patch("lisan.tools.jobs.run_jobs_worker", side_effect=RuntimeError("embedder down")):
            out = _drain_index_jobs(vault=self.vault, db_path=self.db_path, provider=None,
                                    model=None, queued_jobs=queued, drain_jobs=True)
        self.assertFalse(out["drained"])
        self.assertTrue(out["reason"].startswith("error:"))


if __name__ == "__main__":
    unittest.main()
