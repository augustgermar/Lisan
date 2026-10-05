"""A capture must not lock itself out of the database.

2026-10-03: capture.observe jobs failed with "database is locked" three
retries running, each wait the full 30s busy timeout, while no other process
held the file. The cause was inside one process: the pipeline's shared index
connection was left with an open write transaction after indexing a new
record, and the next record's id reservation (a second connection,
BEGIN IMMEDIATE) waited on it.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import lisan.tools.db as db
from lisan.tools import write_boundary
from lisan.tools.rebuild_index import open_index_connection
from lisan.tools.record_fanout import index_created_record
from lisan.tools.record_factory import CreatedRecord


def _write_entity(vault: Path, name: str) -> Path:
    path = vault / "entities" / "people" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "id": f"entity.person.{name}", "type": "entity", "created": "2026-10-03",
        "updated": "2026-10-03", "status": "active", "significance": "low",
        "domain_primary": "cross_arena", "domain_secondary": [], "privacy": "personal",
        "disclosure": "private", "summary": name, "links": [], "confidence": "high",
        "confidence_basis": "test", "last_confirmed": "2026-10-03",
    }
    path.write_text("---\n" + json.dumps(fm, indent=2) + "\n---\n\n# " + name + "\n", encoding="utf-8")
    return path


class IndexConnSelfLockTests(unittest.TestCase):
    def test_second_record_reservation_is_not_blocked_by_first_records_index_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            db_path = Path(tmp) / "lisan.sqlite"
            first = _write_entity(vault, "fixture_one")
            second = _write_entity(vault, "fixture_two")
            # 1.5s rather than 30s: the failure mode is a timeout, not a deadlock.
            with patch.object(db, "BUSY_TIMEOUT_MS", 1500):
                idx = open_index_connection(db_path)
                try:
                    index_created_record(vault, CreatedRecord(path=first, created=True), idx)
                    self.assertFalse(idx.in_transaction, "index write left the write lock held")
                    reservation = write_boundary._reserve_id(second, vault, "entity.person.fixture_two", db_path)
                    self.assertIsNotNone(reservation)
                finally:
                    idx.close()


if __name__ == "__main__":
    unittest.main()
