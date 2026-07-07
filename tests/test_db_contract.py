"""The shared-database contract: WAL journaling, a busy timeout on every
connection, and no side doors. Multiple processes share lisan.sqlite (the
telegram service and its threads, the hourly jobs worker, CLI invocations,
codex children running `lisan` subcommands); rollback-journal mode plus
timeout-less connections produced real "database is locked" crashes
(2026-07-05) and a false lock diagnosis that got a healthy process killed
(2026-07-06). These tests pin the fix."""
from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lisan.tools.db import BUSY_TIMEOUT_MS, connect

REPO = Path(__file__).resolve().parents[1]


class ConnectContractTests(unittest.TestCase):
    def test_connect_sets_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.sqlite"
            conn = connect(db)
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], BUSY_TIMEOUT_MS)
                self.assertIs(conn.row_factory, sqlite3.Row)
            finally:
                conn.close()

    def test_wal_survives_for_other_connections(self):
        # WAL is a property of the file: a later raw connection inherits it.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.sqlite"
            connect(db).close()
            raw = sqlite3.connect(db)
            try:
                self.assertEqual(raw.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            finally:
                raw.close()

    def test_readonly_connection_reads_but_cannot_write(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.sqlite"
            rw = connect(db)
            rw.execute("CREATE TABLE t (x)")
            rw.execute("INSERT INTO t VALUES (1)")
            rw.commit()
            rw.close()
            ro = connect(db, readonly=True)
            try:
                self.assertEqual(ro.execute("SELECT x FROM t").fetchone()[0], 1)
                with self.assertRaises(sqlite3.OperationalError):
                    ro.execute("INSERT INTO t VALUES (2)")
            finally:
                ro.close()


class NoSideDoorsTests(unittest.TestCase):
    def test_every_module_connects_through_db_connect(self):
        """Raw sqlite3.connect outside tools/db.py reintroduces a connection
        with no busy timeout — the exact seam yesterday's crash came from."""
        offenders = []
        for path in (REPO / "lisan").rglob("*.py"):
            if path.name == "db.py" and path.parent.name == "tools":
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\bsqlite3\.connect\(", line):
                    offenders.append(f"{path.relative_to(REPO)}:{i}")
        self.assertEqual(offenders, [], f"raw sqlite3.connect outside tools/db.py: {offenders}")


if __name__ == "__main__":
    unittest.main()
