"""Relationship edges live in markdown and survive reindex and rebuild."""
from __future__ import annotations

import sqlite3

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.entity_resolution import _create_relationship_edges
from lisan.tools.rebuild_index import index_single_record, open_index_connection, rebuild_index
from lisan.tools.record_factory import new_entity


def _edges(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT source_id, target_id, relationship_type FROM links "
            "WHERE source_id LIKE 'entity.%' AND target_id LIKE 'entity.%'"
        ).fetchall()
    finally:
        conn.close()


def test_edges_survive_reindex_and_full_rebuild(tmp_path):
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    db = tmp_path / "index.sqlite"
    robin = new_entity(vault, "Robin Vale")
    sam = new_entity(vault, "Sam Ortiz")
    rebuild_index(vault, db)
    conn = open_index_connection(db)
    try:
        writer = {"relationships_to_create": [
            {"entity_a": "Robin Vale", "entity_b": "Sam Ortiz", "relationship_type": "sibling_of"},
            {"entity_a": "Robin Vale", "entity_b": "Sam Ortiz", "relationship_type": "sibling_of"},
        ]}
        assert _create_relationship_edges(vault, writer, db_path=db, index_conn=conn) == 1
        fm = load_markdown(robin.path).frontmatter
        target = load_markdown(sam.path).frontmatter["id"]
        assert fm["relationships"] == [{"target": target, "type": "sibling_of"}]
        # touching the entity again used to delete its outgoing links
        index_single_record(robin.path, vault, conn)
        conn.commit()
    finally:
        conn.close()
    assert len(_edges(db)) == 1
    rebuild_index(vault, db)
    assert _edges(db) == [(fm["id"], target, "sibling_of")]
