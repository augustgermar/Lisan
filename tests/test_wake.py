"""hold_awake: the machine stays awake exactly while the agent works.
On a Mac that services messages in ~45s darkwakes, a turn or job that
outgrows its window freezes mid-flight (2026-07-06: a 15-minute frozen
reply read as a crashed agent). The context pins the machine awake for
the duration of the work — and must never itself become a reason work
fails."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from lisan.tools.wake import hold_awake


class HoldAwakeTests(unittest.TestCase):
    def test_darwin_spawns_scoped_caffeinate_and_terminates_it(self):
        with patch("lisan.tools.wake.subprocess.Popen") as popen, \
                patch("platform.system", return_value="Darwin"):
            with hold_awake("turn", cap_seconds=120):
                popen.assert_called_once()
                args = popen.call_args.args[0]
                self.assertIn("caffeinate", args[0])
                self.assertIn("-s", args)      # hold off system sleep (AC)
                self.assertIn("120", args)     # capped, never unbounded
            popen.return_value.terminate.assert_called_once()

    def test_terminates_even_when_the_work_raises(self):
        with patch("lisan.tools.wake.subprocess.Popen") as popen, \
                patch("platform.system", return_value="Darwin"):
            with self.assertRaises(RuntimeError):
                with hold_awake("job"):
                    raise RuntimeError("work failed")
            popen.return_value.terminate.assert_called_once()

    def test_non_darwin_is_a_no_op(self):
        with patch("lisan.tools.wake.subprocess.Popen") as popen, \
                patch("platform.system", return_value="Linux"):
            with hold_awake("turn"):
                pass
        popen.assert_not_called()

    def test_caffeinate_failure_never_blocks_the_work(self):
        with patch("lisan.tools.wake.subprocess.Popen", side_effect=OSError("no caffeinate")), \
                patch("platform.system", return_value="Darwin"):
            done = False
            with hold_awake("turn"):
                done = True
            self.assertTrue(done)


if __name__ == "__main__":
    unittest.main()
