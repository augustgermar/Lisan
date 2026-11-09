from __future__ import annotations

import sqlite3

import pytest

from lisan.tools.public_rationale import assert_no_vault_leakage, build_public_rationale
from lisan.tools.rebuild_index import ensure_index_schema
from lisan.tools.self_repair import SelfRepairRefused


def test_build_public_rationale_uses_class_specific_template():
    rationale = build_public_rationale(
        deviation_class="stale", changed_paths=["lisan/tools/deviations.py"], record_count=2, install_id="anon-1",
    )
    assert rationale.used_generic_template is False
    assert "stale" in rationale.title
    assert "lisan/tools/deviations.py" in rationale.body
    assert "install-anon-1" in rationale.body


def test_build_public_rationale_degrades_to_generic_for_unknown_class():
    rationale = build_public_rationale(
        deviation_class="not_a_real_class_yet", changed_paths=["lisan/tools/x.py"], install_id="anon-1",
    )
    assert rationale.used_generic_template is True
    assert "not_a_real_class_yet" in rationale.body
    assert "generic structural fallback" in rationale.body
    # The generic fallback still describes nothing but the closed field set.
    assert "**Changed files:**" in rationale.body


def test_build_public_rationale_requires_class_and_paths():
    with pytest.raises(SelfRepairRefused):
        build_public_rationale(deviation_class="", changed_paths=["a.py"])
    with pytest.raises(SelfRepairRefused):
        build_public_rationale(deviation_class="stale", changed_paths=[])


def test_assert_no_vault_leakage_allows_clean_text(tmp_path):
    db = tmp_path / "index.sqlite"
    conn = sqlite3.connect(db)
    ensure_index_schema(conn)
    conn.close()
    assert_no_vault_leakage("self-repair: stale (1 file)", db_path=db)  # does not raise


def test_assert_no_vault_leakage_refuses_planted_entity_alias(tmp_path):
    db = tmp_path / "index.sqlite"
    conn = sqlite3.connect(db)
    ensure_index_schema(conn)
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias, context) VALUES (?, ?, ?)",
        ("entity.agent.bram", "Bram Carrington", None),
    )
    conn.commit()
    conn.close()
    with pytest.raises(SelfRepairRefused, match="vault-index term"):
        assert_no_vault_leakage("a proposal mentions Bram Carrington somewhere", db_path=db)


def test_assert_no_vault_leakage_refuses_planted_vault_path(tmp_path):
    db = tmp_path / "index.sqlite"
    conn = sqlite3.connect(db)
    ensure_index_schema(conn)
    conn.execute(
        "INSERT INTO files (id, type, path, created, updated, status) VALUES (?, ?, ?, ?, ?, ?)",
        ("entity.agent.bram", "entity", "entities/agents/bram.md", "2026-01-01", "2026-01-01", "active"),
    )
    conn.commit()
    conn.close()
    with pytest.raises(SelfRepairRefused, match="vault-index term"):
        assert_no_vault_leakage("see entities/agents/bram.md for context", db_path=db)


def test_assert_no_vault_leakage_refuses_email_and_phone():
    with pytest.raises(SelfRepairRefused, match="email"):
        assert_no_vault_leakage("contact jane@example.com")
    with pytest.raises(SelfRepairRefused, match="phone"):
        assert_no_vault_leakage("call 555-123-4567")
