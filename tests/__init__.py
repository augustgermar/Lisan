"""Test-suite package init: the one file BOTH runners import.

pytest loads conftest.py; ``python -m unittest discover`` does not — and
that gap is exactly how three full-suite runs on 2026-07-26 delivered a
test fixture's deliberate ingest failure to the owner's real Telegram,
twice per run. conftest's ``no_real_telegram`` stub only guards pytest.
This package init runs under either runner, so the outbound kill switch
is set before any test module (or the code under test) can reach
``_deliver_owner_message``.
"""
import os

os.environ.setdefault("LISAN_NO_OUTBOUND", "1")
