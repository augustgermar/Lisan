from __future__ import annotations

import sqlite3

import pytest

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.rebuild_index import rebuild_index
from lisan.tools.record_factory import new_entity
from lisan.tools.relationship_backfill_migration import (
    MigrationError,
    apply_prepared_migration,
    prepare_migration,
    rollback_migration,
)


def test_backfill_writes_markdown_edges_that_a_rebuild_indexes(tmp_path):
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    a = new_entity(vault, "Robin Vale")
    b = new_entity(vault, "Sam Vale")
    ida, idb = (load_markdown(x.path).frontmatter["id"] for x in (a, b))
    before = a.path.read_text(encoding="utf-8")
    bundle = tmp_path / "bundle"
    record = prepare_migration(vault, [{"source": ida, "target": idb, "type": "sibling_of",
                                        "symmetric": True, "basis": "test"}], bundle)
    assert record["edges_added"] == 2 and a.path.read_text(encoding="utf-8") == before
    apply_prepared_migration(vault, bundle)
    assert load_markdown(a.path).frontmatter["relationships"][0]["target"] == idb
    db = tmp_path / "i.sqlite"
    rebuild_index(vault, db)
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT source_id, target_id FROM links WHERE relationship_type='sibling_of'").fetchall()
    conn.close()
    assert sorted(rows) == sorted([(ida, idb), (idb, ida)])
    rollback_migration(vault, bundle)
    assert a.path.read_text(encoding="utf-8") == before


def test_unknown_ids_are_refused(tmp_path):
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    with pytest.raises(MigrationError, match="unknown entity"):
        prepare_migration(vault, [{"source": "entity.nobody", "target": "entity.none", "type": "x"}],
                          tmp_path / "b")
