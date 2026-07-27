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
import tempfile

os.environ.setdefault("LISAN_NO_OUTBOUND", "1")

# Same containment principle, second seam: any test that resolves a data path
# ambiently (rather than passing an explicit tmp_path) used to write into the
# developer's live install. On 2026-07-27 a full-suite run silently replaced a
# populated production embeddings.bin — 971 vectors — with an empty stub, and
# the ambient sqlite_path had already been logged as a deviation candidate.
# Point the mutable-data root at a throwaway directory before any test module
# imports lisan.paths. Read-only resources (prompts, schemas) still resolve
# from the real repo, so nothing that legitimately reads the package breaks.
os.environ.setdefault(
    "LISAN_DATA_HOME",
    tempfile.mkdtemp(prefix="lisan-test-data-"),
)
