"""Log separation: lisan.log holds everything; errors.log holds only
WARNING and above. "Show me just the problems" must be a tail, not an
archaeology dig through poll-retry tracebacks — the 2026-07-06 incident
was misdiagnosed twice partly because errors were buried in INFO noise."""
from __future__ import annotations

import importlib
import logging
import tempfile
import unittest
from pathlib import Path


def _fresh_logger(vault: Path) -> logging.Logger:
    """get_logger caches globally and the 'lisan' logger keeps handlers
    across tests — reset both so each test gets handlers bound to its own
    temp vault."""
    from lisan.tools import log as log_mod

    importlib.reload(log_mod)
    logging.getLogger("lisan").handlers.clear()
    return log_mod.get_logger(vault)


class LogSplitTests(unittest.TestCase):
    def test_errors_land_in_both_files_info_only_in_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            logger = _fresh_logger(vault)
            logger.info("routine heartbeat")
            logger.warning("rejected unauthorized user")
            logger.error("scheduler tick failed: database is locked")

            main = (vault / "logs" / "lisan.log").read_text(encoding="utf-8")
            errors = (vault / "logs" / "errors.log").read_text(encoding="utf-8")
            self.assertIn("routine heartbeat", main)
            self.assertIn("database is locked", main)
            self.assertNotIn("routine heartbeat", errors)
            self.assertIn("rejected unauthorized user", errors)
            self.assertIn("database is locked", errors)

    def test_tail_log_errors_only_reads_the_error_file(self):
        from lisan.tools import log as log_mod

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            logger = _fresh_logger(vault)
            logger.info("noise")
            logger.error("the one real problem")
            tail = log_mod.tail_log(vault, lines=10, errors_only=True)
            self.assertIn("the one real problem", tail)
            self.assertNotIn("noise", tail)

    def test_errors_only_tail_without_error_file_says_so(self):
        from lisan.tools import log as log_mod

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(log_mod.tail_log(Path(tmp), errors_only=True), "No errors logged.")


if __name__ == "__main__":
    unittest.main()
