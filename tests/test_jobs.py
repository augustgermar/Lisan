from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import sqlite3

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.job_policy import which_jobs_for_turn
from lisan.tools.jobs import (
    audit_jobs,
    cancel_job,
    claim_next_job,
    enqueue_job,
    format_job_audit,
    get_job,
    list_jobs,
    mark_job_failed,
    mark_job_succeeded,
    reap_stuck_jobs,
    retry_job,
    run_jobs_worker,
)


class JobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_enqueue_claim_and_succeed(self) -> None:
        job_id = enqueue_job(
            "index.rebuild_record",
            {"vault": str(self.vault), "reason": "test"},
            db_path=self.db_path,
        )
        job = claim_next_job("worker-a", db_path=self.db_path)
        self.assertIsNotNone(job)
        self.assertEqual(job["id"], job_id)
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["attempts"], 1)

        mark_job_succeeded(job_id, result={"files": 1}, result_ref="reports/index.md", db_path=self.db_path)
        finished = get_job(job_id, db_path=self.db_path)
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["result"], {"files": 1})
        self.assertEqual(finished["result_ref"], "reports/index.md")

    def test_analyst_and_dreamer_coalesce_into_one_queued_job(self) -> None:
        analyst_a = enqueue_job("analyst.scan", {"vault": str(self.vault), "reason": "one"}, db_path=self.db_path)
        analyst_b = enqueue_job("analyst.scan", {"vault": str(self.vault), "reason": "two"}, db_path=self.db_path)
        dreamer_a = enqueue_job("dreamer.maintenance", {"vault": str(self.vault), "task": "compress"}, db_path=self.db_path)
        dreamer_b = enqueue_job("dreamer.maintenance", {"vault": str(self.vault), "task": "primer"}, db_path=self.db_path)

        self.assertEqual(analyst_a, analyst_b)
        self.assertEqual(dreamer_a, dreamer_b)
        self.assertEqual(len(list_jobs(status="queued", db_path=self.db_path)), 2)

        analyst = get_job(analyst_a, db_path=self.db_path)
        dreamer = get_job(dreamer_a, db_path=self.db_path)
        self.assertEqual(analyst["coalesced_count"], 1)
        self.assertEqual(dreamer["coalesced_count"], 1)

    def test_index_rebuild_record_coalesces_per_record_id(self) -> None:
        first = enqueue_job("index.rebuild_record", {"record_id": "claim.alpha", "vault": str(self.vault)}, db_path=self.db_path)
        second = enqueue_job("index.rebuild_record", {"record_id": "claim.alpha", "vault": str(self.vault), "reason": "update"}, db_path=self.db_path)
        other = enqueue_job("index.rebuild_record", {"record_id": "claim.beta", "vault": str(self.vault)}, db_path=self.db_path)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(len(list_jobs(status="queued", db_path=self.db_path)), 2)

    def test_writer_extract_turn_does_not_coalesce(self) -> None:
        first = enqueue_job("writer.extract_turn", {"text": "turn one", "vault": str(self.vault)}, db_path=self.db_path)
        second = enqueue_job("writer.extract_turn", {"text": "turn two", "vault": str(self.vault)}, db_path=self.db_path)
        self.assertNotEqual(first, second)
        self.assertEqual(len(list_jobs(status="queued", db_path=self.db_path)), 2)

    def test_running_maintenance_allows_one_follow_up_only(self) -> None:
        first = enqueue_job("dreamer.maintenance", {"vault": str(self.vault), "task": "compress"}, db_path=self.db_path)
        claimed = claim_next_job("worker-maint", db_path=self.db_path)
        self.assertEqual(claimed["id"], first)
        follow_one = enqueue_job("dreamer.maintenance", {"vault": str(self.vault), "task": "primer"}, db_path=self.db_path)
        follow_two = enqueue_job("dreamer.maintenance", {"vault": str(self.vault), "task": "confidence"}, db_path=self.db_path)
        self.assertEqual(follow_one, follow_two)
        queued = list_jobs(status="queued", db_path=self.db_path)
        running = list_jobs(status="running", db_path=self.db_path)
        self.assertEqual(len(queued), 1)
        self.assertEqual(len(running), 1)
        self.assertEqual(queued[0]["replaces_job_id"], first)

    def test_failed_job_does_not_block_new_job(self) -> None:
        failed = enqueue_job("analyst.scan", {"vault": str(self.vault), "reason": "first"}, db_path=self.db_path)
        mark_job_failed(failed, "boom", retry=False, db_path=self.db_path)
        fresh = enqueue_job("analyst.scan", {"vault": str(self.vault), "reason": "second"}, db_path=self.db_path)
        self.assertNotEqual(failed, fresh)
        self.assertEqual(len(list_jobs(status="failed", db_path=self.db_path)), 1)
        self.assertEqual(len(list_jobs(status="queued", db_path=self.db_path)), 1)

    def test_canceled_job_is_not_claimed_and_future_jobs_wait(self) -> None:
        canceled_id = enqueue_job(
            "index.rebuild_record",
            {"vault": str(self.vault)},
            db_path=self.db_path,
        )
        cancel_job(canceled_id, db_path=self.db_path)

        future_id = enqueue_job(
            "index.rebuild_record",
            {"vault": str(self.vault)},
            scheduled_for="2099-01-01T00:00:00Z",
            db_path=self.db_path,
        )
        self.assertIsNone(claim_next_job("worker-b", db_path=self.db_path))
        self.assertEqual(get_job(canceled_id, db_path=self.db_path)["status"], "canceled")
        self.assertEqual(get_job(future_id, db_path=self.db_path)["status"], "queued")

        due_id = enqueue_job(
            "index.rebuild_record",
            {"vault": str(self.vault)},
            scheduled_for=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            db_path=self.db_path,
        )
        claimed = claim_next_job("worker-b", db_path=self.db_path)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], due_id)

    def test_failed_job_retries_until_max_attempts(self) -> None:
        job_id = enqueue_job(
            "analyst.scan",
            {"vault": str(self.vault)},
            max_attempts=2,
            db_path=self.db_path,
        )
        with patch("lisan.tools.analyst_ops.run_analyst_scan", side_effect=[RuntimeError("boom"), {
            "report_path": str(self.root / "reports" / "analyst.md"),
            "pattern_paths": [],
            "review_paths": [],
            "response": {"summary": "ok"},
        }]):
            summary = run_jobs_worker(vault=self.vault, db_path=self.db_path, worker_id="worker-c")
        job = get_job(job_id, db_path=self.db_path)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["attempts"], 2)
        self.assertEqual(summary["failure_count"], 1)
        self.assertEqual(summary["success_count"], 1)

    def test_job_handler_dispatches_analyst_and_dreamer(self) -> None:
        analyst_job = enqueue_job("analyst.scan", {"vault": str(self.vault)}, db_path=self.db_path)
        dreamer_job = enqueue_job("dreamer.maintenance", {"vault": str(self.vault)}, db_path=self.db_path)
        with patch("lisan.tools.analyst_ops.run_analyst_scan", return_value={
            "report_path": str(self.root / "reports" / "analyst.md"),
            "pattern_paths": [],
            "review_paths": [],
            "response": {"summary": "ok"},
        }) as analyst_mock, patch("lisan.tools.dreamer_ops.run_dreamer_task", return_value=self.root / "reports" / "dreamer.md") as dreamer_mock:
            summary = run_jobs_worker(vault=self.vault, db_path=self.db_path, worker_id="worker-d")
        self.assertGreaterEqual(summary["processed_count"], 2)
        self.assertTrue(analyst_mock.called)
        self.assertTrue(dreamer_mock.called)
        self.assertEqual(get_job(analyst_job, db_path=self.db_path)["status"], "succeeded")
        self.assertEqual(get_job(dreamer_job, db_path=self.db_path)["status"], "succeeded")

    def test_audit_output_shows_failures_and_waiting_index_jobs(self) -> None:
        failed_id = enqueue_job("writer.extract_turn", {"text": "hello", "vault": str(self.vault)}, priority=50, db_path=self.db_path)
        retry_wait_id = enqueue_job("writer.extract_turn", {"text": "hello again", "vault": str(self.vault)}, priority=100, db_path=self.db_path)
        index_wait_id = enqueue_job("index.rebuild_record", {"vault": str(self.vault)}, priority=200, db_path=self.db_path)

        claimed = claim_next_job("worker-e", db_path=self.db_path)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], failed_id)
        mark_job_failed(claimed["id"], "boom", retry=False, db_path=self.db_path)

        claimed = claim_next_job("worker-e", db_path=self.db_path)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], retry_wait_id)
        mark_job_failed(claimed["id"], "try again", retry=True, db_path=self.db_path)

        report = audit_jobs(vault=self.vault, db_path=self.db_path)
        text = format_job_audit(report)

        self.assertIn("Failed jobs:", text)
        self.assertIn(failed_id, text)
        self.assertIn("boom", text)
        self.assertIn("Retry-wait jobs:", text)
        self.assertIn(retry_wait_id, text)
        self.assertIn("Memory records waiting for index rebuild:", text)
        self.assertIn(index_wait_id, text)
        self.assertEqual(report["queued_by_type"].get("index.rebuild_record"), 1)

    def test_audit_reports_stuck_running_jobs(self) -> None:
        job_id = enqueue_job("analyst.scan", {"vault": str(self.vault)}, db_path=self.db_path)
        claimed = claim_next_job("worker-stuck", db_path=self.db_path)
        self.assertEqual(claimed["id"], job_id)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE jobs SET started_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00Z", job_id),
            )
            conn.commit()
        finally:
            conn.close()
        report = audit_jobs(vault=self.vault, db_path=self.db_path)
        text = format_job_audit(report)
        self.assertIn("Stuck jobs", text)
        self.assertIn(job_id, text)
        self.assertTrue(report["stuck_jobs"])

    def test_reap_stuck_jobs_moves_running_job_back_to_retry_wait(self) -> None:
        job_id = enqueue_job("analyst.scan", {"vault": str(self.vault)}, db_path=self.db_path)
        claim_next_job("worker-reap", db_path=self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE jobs SET started_at = ? WHERE id = ?", ("2000-01-01T00:00:00Z", job_id))
            conn.commit()
        finally:
            conn.close()
        result = reap_stuck_jobs(db_path=self.db_path, timeout_minutes=15, retry=True)
        self.assertEqual(result["reaped_count"], 1)
        self.assertEqual(get_job(job_id, db_path=self.db_path)["status"], "retry_wait")

    def test_list_jobs_includes_payload_and_retry_state(self) -> None:
        job_id = enqueue_job(
            "index.rebuild_record",
            {"vault": str(self.vault), "reason": "test"},
            db_path=self.db_path,
        )
        retry_job(job_id, db_path=self.db_path)
        jobs = list_jobs(db_path=self.db_path)
        self.assertEqual(jobs[0]["id"], job_id)
        self.assertEqual(jobs[0]["payload"]["reason"], "test")

    def test_policy_skips_analyst_and_dreamer_on_trivial_short_turns(self) -> None:
        jobs = which_jobs_for_turn(
            {
                "text": "ok",
                "action": "skip",
                "mode": "skip",
                "vault": str(self.vault),
                "db_path": str(self.db_path),
            },
            db_path=self.db_path,
        )
        self.assertEqual(jobs, [])


if __name__ == "__main__":
    unittest.main()
