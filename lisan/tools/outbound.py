"""One kill switch for everything that leaves the machine.

Before 2026-10-29 only the scheduler's owner-message path honoured
``LISAN_NO_OUTBOUND``; ``send_email`` and the approval-gated skills (Gmail,
Drive) did not, so the switch the test suite relies on covered one channel
out of several. Every outbound path now calls :func:`assert_outbound_allowed`
immediately before the side effect.
"""
from __future__ import annotations

import os


class OutboundDisabled(RuntimeError):
    """Raised instead of silently succeeding: the message was NOT delivered."""


def assert_outbound_allowed(channel: str) -> None:
    if os.environ.get("LISAN_NO_OUTBOUND"):
        raise OutboundDisabled(f"outbound {channel} disabled by LISAN_NO_OUTBOUND")

    from ..paths import _looks_like_a_test_process

    if _looks_like_a_test_process() and not os.environ.get("LISAN_ALLOW_TEST_OUTBOUND"):
        raise OutboundDisabled(f"outbound {channel} refused: this is a test process")
