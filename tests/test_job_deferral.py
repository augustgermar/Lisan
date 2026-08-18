"""A deferral is not a failure.

The defect these pin (2026-08-16): `lisan restart` correctly refused to
bounce the service over jobs that were mid-run and said so — "wait for them
to finish". The self_repair.restart job handler raised that advice as an
ordinary error, and job retries carry no backoff, so the job re-fired three
times in two seconds while the very same jobs were still running, then took
its one second chance, then died. The applied repair never got its restart,
and the owner got a failure alarm for a guard working exactly as designed.

Waiting had to become a first-class outcome: rescheduled into the future,
no attempt spent, no alarm — but bounded, so a condition that never clears
still reaches the owner.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.jobs import (
    DEFERRAL_COUNT_KEY,
    MAX_DEFERRALS,
    JobDeferred,
    claim_next_job,
    defer_job,
    enqueue_job,
    get_job,
    run_jobs_worker,
)


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


class DeferJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _claimed_job(self) -> dict:
        enqueue_job("self_repair.restart", {"vault": str(self.vault)}, db_path=self.db_path)
        job = claim_next_job("worker.test", db_path=self.db_path)
        assert job is not None
        return job

    def test_deferral_reschedules_into_the_future_and_refunds_the_attempt(self) -> None:
        job = self._claimed_job()
        self.assertEqual(job["attempts"], 1)

        deferred = defer_job(job["id"], "waiting on mid-run jobs", retry_after_seconds=300, db_path=self.db_path)

        self.assertEqual(deferred["status"], "retry_wait")
        self.assertEqual(deferred["attempts"], 0, "a deferral must not spend an attempt")
        self.assertGreater(_parse(deferred["scheduled_for"]), datetime.now(timezone.utc))
        self.assertIn("waiting on mid-run jobs", deferred["error"])

    def test_deferral_is_not_immediately_reclaimable(self) -> None:
        """The 08-16 shape: zero backoff meant the retry re-fired while the
        condition it was waiting on was still true."""
        job = self._claimed_job()
        defer_job(job["id"], "still busy", retry_after_seconds=300, db_path=self.db_path)
        self.assertIsNone(claim_next_job("worker.test", db_path=self.db_path))

    def test_deferrals_accumulate_in_the_payload(self) -> None:
        job = self._claimed_job()
        for expected in (1, 2, 3):
            defer_job(job["id"], "busy", retry_after_seconds=1, db_path=self.db_path)
            self.assertEqual(get_job(job["id"], db_path=self.db_path)["payload"][DEFERRAL_COUNT_KEY], expected)
            claim_next_job("worker.test", db_path=self.db_path)

    def test_bounded_waiting_ends_in_a_terminal_failure(self) -> None:
        """Waiting forever is its own silent failure. Once the budget is
        spent the job fails for real and takes the escalation ladder."""
        job = self._claimed_job()
        for _ in range(MAX_DEFERRALS):
            defer_job(job["id"], "busy", retry_after_seconds=1, db_path=self.db_path)
            claim_next_job("worker.test", db_path=self.db_path)
        final = defer_job(job["id"], "busy", retry_after_seconds=1, db_path=self.db_path)
        self.assertEqual(final["status"], "failed")
        self.assertIn(f"deferred {MAX_DEFERRALS} times", final["error"])

    def test_unknown_job_returns_none(self) -> None:
        self.assertIsNone(defer_job("job.missing", "busy", db_path=self.db_path))


class RestartHandlerTests(unittest.TestCase):
    """The handler's own contract: which restart outcomes are deferrals and
    which are genuine failures."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _dispatch(self, restart_result: dict):
        from lisan.tools.jobs import dispatch_job

        job = {"id": "job.test", "job_type": "self_repair.restart", "payload": {"vault": str(self.vault)}}
        with patch("lisan.tools.restart.restart_service", return_value=restart_result):
            return dispatch_job(job, vault=self.vault, db_path=self.db_path)

    def test_jobs_in_flight_defers_and_names_the_blocking_jobs(self) -> None:
        with self.assertRaises(JobDeferred) as ctx:
            self._dispatch({
                "restarted": False,
                "reason": "jobs_in_flight",
                "running_jobs": [{"id": "job.busy", "job_type": "capture.observe"}],
                "hint": "wait",
            })
        self.assertIn("job.busy", str(ctx.exception))

    def test_a_real_restart_failure_is_still_a_failure(self) -> None:
        """Only the wait-and-see case is a deferral; a service manager that
        refuses must not be retried quietly forever."""
        with self.assertRaises(RuntimeError) as ctx:
            self._dispatch({"restarted": False, "reason": "launchctl: no such service"})
        self.assertNotIsInstance(ctx.exception, JobDeferred)
        self.assertIn("no such service", str(ctx.exception))

    def test_successful_restart_returns_the_report(self) -> None:
        result = self._dispatch({"restarted": True, "command": "launchctl kickstart -k"})
        self.assertTrue(result["restarted"])


class WorkerDeferralTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_worker_defers_without_alarming_the_owner(self) -> None:
        """The whole point: the owner heard "2 self-restart attempts failed"
        for a guard doing its job. A deferral raises no escalation."""
        job_id = enqueue_job("self_repair.restart", {"vault": str(self.vault)}, db_path=self.db_path)
        escalate = MagicMock()
        with (
            patch("lisan.tools.restart.restart_service", return_value={
                "restarted": False,
                "reason": "jobs_in_flight",
                "running_jobs": [{"id": "job.busy", "job_type": "capture.observe"}],
                "hint": "wait",
            }),
            patch("lisan.tools.escalation.escalate_terminal_failure", escalate),
        ):
            summary = run_jobs_worker(vault=self.vault, db_path=self.db_path)

        escalate.assert_not_called()
        self.assertEqual(summary["failure_count"], 0)
        self.assertEqual(summary["deferral_count"], 1)
        job = get_job(job_id, db_path=self.db_path)
        self.assertEqual(job["status"], "retry_wait")
        self.assertEqual(job["attempts"], 0)

    def test_worker_drain_terminates_rather_than_spinning_on_a_deferral(self) -> None:
        """A deferred job must not be re-claimed by the same drain."""
        enqueue_job("self_repair.restart", {"vault": str(self.vault)}, db_path=self.db_path)
        with patch("lisan.tools.restart.restart_service", return_value={
            "restarted": False,
            "reason": "jobs_in_flight",
            "running_jobs": [],
            "hint": "wait",
        }):
            summary = run_jobs_worker(vault=self.vault, db_path=self.db_path)
        self.assertEqual(summary["processed_count"], 1)


if __name__ == "__main__":
    unittest.main()
