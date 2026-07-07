"""`lisan restart`: real (the agent once invented it), and disciplined —
it refuses to bounce the service over in-flight jobs unless forced (the
developer once did exactly that and orphaned seven claims)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.jobs import claim_next_job, enqueue_job
from lisan.tools.restart import render_restart_report, restart_service


def _db_with_running_job(root: Path) -> Path:
    ensure_repo_layout(root)
    db = root / "lisan.sqlite"
    enqueue_job("capture.observe", {"vault": str(vault_root(root)), "text": "x", "response": "y"}, db_path=db)
    claimed = claim_next_job("worker.test", db_path=db)
    assert claimed is not None
    return db


class RestartGuardTests(unittest.TestCase):
    def test_refuses_over_running_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db_with_running_job(Path(tmp))
            runner = MagicMock()
            report = restart_service(db_path=db, runner=runner, system="Darwin")
        runner.assert_not_called()
        self.assertFalse(report["restarted"])
        self.assertEqual(report["reason"], "jobs_in_flight")
        self.assertEqual(len(report["running_jobs"]), 1)
        rendered = render_restart_report(report)
        self.assertIn("Not restarting", rendered)
        self.assertIn("capture.observe", rendered)

    def test_force_restarts_and_names_the_jobs_it_ran_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db_with_running_job(Path(tmp))
            runner = MagicMock(return_value=MagicMock(returncode=0, stderr="", stdout=""))
            report = restart_service(db_path=db, force=True, runner=runner, system="Darwin")
        runner.assert_called_once()
        cmd = runner.call_args.args[0]
        self.assertEqual(cmd[:3], ["launchctl", "kickstart", "-k"])
        self.assertTrue(report["restarted"])
        self.assertEqual(len(report["forced_over_jobs"]), 1)

    def test_idle_queue_restarts_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            db = root / "lisan.sqlite"
            enqueue_job("task.reminder", {"message": "x"}, scheduled_for="2099-01-01T00:00:00Z", db_path=db)
            runner = MagicMock(return_value=MagicMock(returncode=0, stderr="", stdout=""))
            report = restart_service(db_path=db, runner=runner, system="Linux")
        cmd = runner.call_args.args[0]
        self.assertEqual(cmd, ["systemctl", "--user", "restart", "lisan-telegram.service"])
        self.assertTrue(report["restarted"])

    def test_service_manager_failure_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            runner = MagicMock(return_value=MagicMock(returncode=1, stderr="no such service", stdout=""))
            report = restart_service(db_path=root / "lisan.sqlite", runner=runner, system="Darwin")
        self.assertFalse(report["restarted"])
        self.assertIn("no such service", report["reason"])


if __name__ == "__main__":
    unittest.main()
