"""Ranking: disputed records are demoted; learned associations come only
from the owner's conversations, minus exploratory picks."""
from __future__ import annotations

import json
import sqlite3

from lisan.tools.learned_edges import learning_signal_rows


def _log(conn, loaded, purpose=None, exploratory=None):
    conn.execute(
        "INSERT INTO retrieval_log (files_loaded, purpose, exploratory_ids) VALUES (?, ?, ?)",
        (json.dumps(loaded), purpose, json.dumps(exploratory or [])),
    )


def test_only_conversation_rows_minus_serendipity_teach(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.sqlite")
    conn.execute("CREATE TABLE retrieval_log (id INTEGER PRIMARY KEY, files_loaded TEXT, purpose TEXT, exploratory_ids TEXT)")
    _log(conn, ["a", "b", "z"], "conversation", ["z"])
    _log(conn, ["a", "c"], "maintenance")
    _log(conn, ["d", "e"], None)  # legacy row: counts as conversation
    conn.commit()
    rows = learning_signal_rows(conn, 10)
    assert {"a", "b"} in rows and {"d", "e"} in rows
    assert all("z" not in r and "c" not in r for r in rows)


def test_legacy_log_without_purpose_column_still_works(tmp_path):
    conn = sqlite3.connect(tmp_path / "y.sqlite")
    conn.execute("CREATE TABLE retrieval_log (id INTEGER PRIMARY KEY, files_loaded TEXT)")
    conn.execute("INSERT INTO retrieval_log (files_loaded) VALUES (?)", (json.dumps(["a", "b"]),))
    conn.commit()
    assert learning_signal_rows(conn, 10) == [{"a", "b"}]


def test_disputed_is_demoted_not_boosted():
    from collections import defaultdict

    from lisan.tools.retrieval_layers import _sql_metadata_score

    def row(status):
        r = defaultdict(lambda: None)
        r.update({"id": "claim.x", "type": "claim", "status": status, "domain_primary": "work",
                  "summary": "x", "significance": "low", "updated": None})
        return r

    confirmed = _sql_metadata_score(row("confirmed"), "work", "zzz")
    disputed = _sql_metadata_score(row("disputed"), "work", "zzz")
    assert disputed < confirmed - 1.0
