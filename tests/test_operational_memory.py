from __future__ import annotations

import sqlite3
from pathlib import Path

from lisan.frontmatter import write_markdown
from lisan.tools.operational_memory import migrate_operational_reports
from lisan.tools.rebuild_index import ensure_index_schema, index_single_record
from lisan.tools.retrieval import retrieve_context


def _report(vault: Path, name: str, record_id: str, summary: str, *, bundle_chars: int = 0) -> Path:
    path = vault / "reports" / name
    write_markdown(path, {
        "id": record_id,
        "type": "report",
        "created": "2026-10-01",
        "updated": "2026-10-01",
        "status": "active",
        "summary": summary,
        "task": "compress",
        "bundle_chars": bundle_chars,
    }, "maintenance details")
    return path


def test_operational_reports_are_excluded_but_explicit_lane_is_available(tmp_path: Path):
    vault = tmp_path / "vault"
    db = tmp_path / "lisan.sqlite"
    (vault / "reports").mkdir(parents=True)
    semantic = vault / "episodes" / "memory.md"
    write_markdown(semantic, {
        "id": "episode.memory",
        "type": "episode",
        "created": "2026-10-01",
        "updated": "2026-10-01",
        "status": "active",
        "summary": "The orchard ladder plan",
    }, "The orchard ladder plan")
    report = _report(vault, "maintenance.md", "report.maintenance", "Dreamer compress report")
    conn = sqlite3.connect(db)
    ensure_index_schema(conn)
    index_single_record(semantic, vault, conn)
    index_single_record(report, vault, conn)
    conn.commit()
    conn.close()

    default = retrieve_context("Dreamer compress report", vault=vault, db_path=db)
    explicit = retrieve_context("Dreamer compress report", vault=vault, db_path=db, include_operational=True)
    assert all(item.id != "report.maintenance" for item in default.loaded)
    assert any(item.id == "report.maintenance" for item in explicit.loaded)


def test_migration_archives_heavy_duplicate_and_leaves_manifest(tmp_path: Path):
    vault = tmp_path / "vault"
    db = tmp_path / "lisan.sqlite"
    (vault / "reports").mkdir(parents=True)
    first = _report(vault, "first.md", "report.first", "Dreamer compress report", bundle_chars=200_000)
    second = _report(vault, "second.md", "report.second", "Dreamer compress report", bundle_chars=0)
    conn = sqlite3.connect(db)
    ensure_index_schema(conn)
    index_single_record(first, vault, conn)
    index_single_record(second, vault, conn)
    conn.commit()
    conn.close()

    result = migrate_operational_reports(vault, db)
    assert result["archived"] == 1
    assert not first.exists() and second.exists()
    assert Path(result["archive_manifest"]).exists()
    conn = sqlite3.connect(db)
    assert conn.execute("select count(*) from files where archived_at is not null").fetchone()[0] == 1
    conn.close()
