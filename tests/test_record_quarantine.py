from __future__ import annotations

import json

from lisan.frontmatter import load_markdown
from lisan.tools.record_factory import new_knowledge
from lisan.tools.record_quarantine import (
    apply_manifest,
    build_manifest,
    rollback_manifest,
    write_manifest,
)
from lisan.tools.rebuild_index import rebuild_index
from lisan.tools.retrieval import retrieve_context


def test_quarantine_is_default_hidden_explicitly_queryable_and_reversible(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    db = tmp_path / "lisan.sqlite"
    monkeypatch.setenv("LISAN_DATA_HOME", str(tmp_path))
    good = new_knowledge(vault, "ordinary companion", summary="ordinary companion context")
    bad = new_knowledge(vault, "load bearing recall", summary="quasar-specific load bearing recall")

    # Deliberate legacy damage for the manifest builder to discover.
    doc = load_markdown(bad.path)
    fm = dict(doc.frontmatter)
    fm["links"] = ["entity.does-not-exist"]
    bad.path.write_text(
        "---\n" + json.dumps(fm, indent=2) + "\n---\n\n" + doc.body,
        encoding="utf-8",
    )
    rebuild_index(vault, db_path=db)

    manifest_path = tmp_path / "quarantine.json"
    manifest = build_manifest(vault, db, migration_id="test-quarantine")
    assert manifest["record_count"] == 1
    write_manifest(manifest_path, manifest)
    apply_manifest(manifest_path, vault, db)

    hidden = retrieve_context("quasar-specific", vault=vault, db_path=db)
    explicit = retrieve_context(
        "quasar-specific", vault=vault, db_path=db, include_quarantined=True
    )
    bad_id = load_markdown(bad.path).frontmatter["id"]
    assert bad_id not in {item.id for item in hidden.loaded}
    assert bad_id in {item.id for item in hidden.rejected}
    assert bad_id in {item.id for item in explicit.loaded}
    assert good.path.exists() and bad.path.exists(), "quarantine never moves or deletes files"

    rollback_manifest(manifest_path, db)
    restored = retrieve_context("quasar-specific", vault=vault, db_path=db)
    assert bad_id in {item.id for item in restored.loaded}
