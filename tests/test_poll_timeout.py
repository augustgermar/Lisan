"""Long-poll timeouts are churn, not errors; prompt delivery is the task."""
from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.jobs import enqueue_job, get_job
from lisan.tools.scheduler import run_task_job
from lisan.tools.telegram_bot import _is_poll_timeout


class PollTimeoutClassifierTests(unittest.TestCase):
    def test_raw_read_timeout_is_routine(self):
        self.assertTrue(_is_poll_timeout(TimeoutError("The read operation timed out")))

    def test_urlerror_wrapping_timeout_is_routine(self):
        self.assertTrue(_is_poll_timeout(urllib.error.URLError(TimeoutError())))

    def test_real_network_errors_are_not(self):
        self.assertFalse(_is_poll_timeout(urllib.error.URLError(OSError(8, "nodename nor servname"))))
        self.assertFalse(_is_poll_timeout(RuntimeError("boom")))


class PromptDeliveryIsTheTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "jobs.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_undelivered_prompt_response_fails_the_job(self):
        # The pipeline answered, but the owner never received it: that is a
        # job failure (retried, escalated), never a quiet success.
        job_id = enqueue_job("task.prompt", {"prompt": "one thing today?", "chat_id": 7}, db_path=self.db)
        job = get_job(job_id, db_path=self.db)

        def _dead_send(text, chat_id):
            raise RuntimeError("telegram unreachable")

        with patch("lisan.tools.chat._process_chat_turn", return_value={"response": "ok"}):
            with self.assertRaises(RuntimeError):
                run_task_job(job, vault=self.vault, db_path=self.db, send_fn=_dead_send)


if __name__ == "__main__":
    unittest.main()
