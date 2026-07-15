"""The terminal-failure ladder: notify → one second chance → investigation.

Gate for the silent-failure class (owner policy, 2026-07-15): a job that
fails terminally must page the owner with the real error, get exactly one
more attempt, and — if that fails too — become an investigation open loop.
Nothing in the ladder may itself take the worker down.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.escalation import (
    SECOND_CHANCE_KEY,
    escalate_terminal_failure,
    failure_fingerprint,
)
from lisan.tools.jobs import enqueue_job, get_job, list_jobs, run_jobs_worker


class FingerprintTests(unittest.TestCase):
    def test_same_defect_different_run_same_fingerprint(self):
        a = failure_fingerprint("task.prompt", "job.20260706T132225.93a3cb5d failed: needs 3 things")
        b = failure_fingerprint("task.prompt", "job.20260707T121310.fd4ee330 failed: needs 7 things")
        self.assertEqual(a, b)

    def test_different_job_types_never_collide(self):
        self.assertNotEqual(
            failure_fingerprint("task.prompt", "boom"),
            failure_fingerprint("capture.observe", "boom"),
        )


class EscalationLadderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "jobs.sqlite"
        self.sent: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def _notify_patch(self):
        def _record(text, *, chat_id=None, config=None):
            self.sent.append(text)

        return patch("lisan.tools.scheduler._deliver_owner_message", _record)

    def _failed_job(self, **payload_extra) -> dict:
        job_id = enqueue_job(
            "task.reminder", {"message": "water the cats", **payload_extra}, db_path=self.db,
        )
        return get_job(job_id, db_path=self.db)

    def test_first_failure_notifies_and_enqueues_one_second_chance(self):
        job = self._failed_job()
        with self._notify_patch():
            out = escalate_terminal_failure(job, "boom", vault=self.vault, db_path=self.db)
        self.assertTrue(out["notified"])
        self.assertIn("boom", self.sent[0])
        clone = get_job(out["second_chance_id"], db_path=self.db)
        self.assertEqual(clone["job_type"], "task.reminder")
        self.assertEqual(clone["payload"][SECOND_CHANCE_KEY], job["id"])
        self.assertEqual(clone["max_attempts"], 1)
        self.assertIsNone(clone["recurrence"])
        self.assertIsNone(out["investigation"])

    def test_second_failure_files_investigation_not_a_third_try(self):
        job = self._failed_job()
        with self._notify_patch():
            first = escalate_terminal_failure(job, "boom", vault=self.vault, db_path=self.db)
            clone = get_job(first["second_chance_id"], db_path=self.db)
            queued_before = len(list_jobs(status="queued", db_path=self.db))
            second = escalate_terminal_failure(clone, "boom again", vault=self.vault, db_path=self.db)
        self.assertIsNotNone(second["investigation"])
        self.assertIsNone(second["second_chance_id"])
        self.assertEqual(len(list_jobs(status="queued", db_path=self.db)), queued_before)
        fm = load_markdown(Path(second["investigation"])).frontmatter
        self.assertEqual(fm["origin"], "self")
        self.assertEqual(fm["owner"], "agent")
        self.assertEqual(fm["status"], "active")
        self.assertTrue(fm["failure_fingerprint"])
        self.assertEqual(len(self.sent), 2)
        self.assertIn("boom again", self.sent[1])

    def test_repeat_failures_share_one_open_investigation(self):
        job = self._failed_job()
        clone_payload = {**job["payload"], SECOND_CHANCE_KEY: job["id"]}
        clone = dict(job, payload=clone_payload)
        with self._notify_patch():
            first = escalate_terminal_failure(clone, "same boom", vault=self.vault, db_path=self.db)
            second = escalate_terminal_failure(clone, "same boom", vault=self.vault, db_path=self.db)
        self.assertIsNotNone(first["investigation"])
        self.assertIsNone(second["investigation"])
        loops = list((self.vault / "open_loops").glob("*investigate*"))
        self.assertEqual(len(loops), 1)

    def test_unreenqueueable_payload_goes_straight_to_investigation(self):
        # A bodyless task payload cannot pass enqueue validation: the ladder
        # must skip the retry and file the investigation immediately.
        job = {"id": "job.legacy", "job_type": "task.prompt", "payload": {"chat_id": 7}}
        with self._notify_patch():
            out = escalate_terminal_failure(job, "requires a prompt", vault=self.vault, db_path=self.db)
        self.assertIsNone(out["second_chance_id"])
        self.assertIsNotNone(out["investigation"])
        self.assertIn("cannot be retried", self.sent[0])

    def test_escalation_never_raises(self):
        job = self._failed_job()
        with patch(
            "lisan.tools.scheduler._deliver_owner_message",
            side_effect=RuntimeError("telegram down"),
        ):
            out = escalate_terminal_failure(job, "boom", vault=self.vault, db_path=self.db)
        self.assertFalse(out["notified"])
        # the ladder continued past the failed notification
        self.assertIsNotNone(out["second_chance_id"])

    def test_worker_walks_full_ladder_end_to_end(self):
        # A dispatch that always fails: 3 attempts, notify, second chance
        # (1 attempt), notify again, investigation. Exactly two jobs total.
        enqueue_job("task.reminder", {"message": "doomed"}, db_path=self.db)
        with self._notify_patch(), patch(
            "lisan.tools.scheduler.run_task_job", side_effect=RuntimeError("wire fell out"),
        ):
            summary = run_jobs_worker(vault=self.vault, db_path=self.db)
        self.assertEqual(summary["failure_count"], 4)  # 3 + second chance
        failed = list_jobs(status="failed", db_path=self.db)
        self.assertEqual(len(failed), 2)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("wire fell out", self.sent[0])
        loops = list((self.vault / "open_loops").glob("*investigate*"))
        self.assertEqual(len(loops), 1)
        self.assertEqual(list_jobs(status="queued", db_path=self.db), [])


if __name__ == "__main__":
    unittest.main()
