"""WO-ADJUTANT step 2: additive schema — new types, optional task fields,
index columns and tables. The binding claims: existing vaults migrate by
doing nothing, and malformed task fields never reach the poller."""
from __future__ import annotations

import sqlite3

import pytest

from lisan.frontmatter import load_markdown, write_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.record_factory import new_confirmation, new_open_loop, new_schedule
from lisan.tools.rebuild_index import SCHEMA_SQL, ensure_index_schema, index_single_record
from lisan.tools.validator import validate_vault
from lisan.tools.db import connect as db_connect

NEW_COLUMNS = ["execute_asap", "task_kind", "task_status", "next_run", "expires"]
NEW_TABLES = ["adjutant_log", "task_runs", "confirmations"]


@pytest.fixture()
def vault(tmp_path):
    v = tmp_path / "vault"
    ensure_vault_layout(v)
    return v


def _validate(vault):
    report = validate_vault(vault)
    return report, [f"{i.severity}: {i.message}" for i in report.issues]


# ---------------------------------------------------------------------------
# New record types

def test_schedule_factory_produces_valid_record(vault):
    created = new_schedule(
        vault,
        "Monthly finance snapshot",
        task_kind="collect",
        cron="monthly:1@09:00",
        next_run="2026-08-01T09:00:00",
        payload={"paths": ["~/Documents/finance"]},
    )
    report, issues = _validate(vault)
    assert report.ok, issues
    fm = load_markdown(created.path).frontmatter
    assert fm["type"] == "schedule" and fm["cron"] == "monthly:1@09:00"


def test_schedule_factory_rejects_bad_vocabulary(vault):
    with pytest.raises(ValueError, match="task_kind"):
        new_schedule(vault, "x", task_kind="world_domination", cron="daily@09:00", next_run="2026-08-01")
    with pytest.raises(ValueError, match="cron"):
        new_schedule(vault, "x", task_kind="draft", cron="whenever", next_run="2026-08-01")


def test_schedule_validator_catches_bad_cron_and_kind(vault):
    created = new_schedule(vault, "ok", task_kind="draft", cron="daily@09:00", next_run="2026-08-01")
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm["cron"] = "every-so-often"
    fm["task_kind"] = "meddle"
    fm["payload"] = "not-an-object"
    write_markdown(created.path, fm, doc.body)
    report, issues = _validate(vault)
    assert not report.ok
    assert any("Invalid cron" in i for i in issues)
    assert any("Invalid task_kind" in i for i in issues)
    assert any("payload must be an object" in i for i in issues)


def test_confirmation_factory_produces_valid_record(vault):
    loop = new_open_loop(vault, "Send the invoice")
    loop_id = load_markdown(loop.path).frontmatter["id"]
    new_confirmation(
        vault,
        "Confirm outbound invoice message",
        task_id=loop_id,
        task_summary="Send invoice #42 to the client",
        planned_action="Telegram message to client: 'Invoice #42 attached, due 2026-08-15.'",
        risk="Outbound communication; wrong recipient is unrecallable.",
        expires="2026-07-30",
    )
    report, issues = _validate(vault)
    assert report.ok, issues


def test_confirmation_validator_requires_substance(vault):
    loop = new_open_loop(vault, "Anchor loop")
    loop_id = load_markdown(loop.path).frontmatter["id"]
    created = new_confirmation(
        vault, "c", task_id=loop_id, task_summary="s", planned_action="a", risk="r", expires="2026-07-30"
    )
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm["planned_action"] = "   "
    fm["expires"] = "soonish"
    fm["resolution"] = "shrugged"
    write_markdown(created.path, fm, doc.body)
    report, issues = _validate(vault)
    assert not report.ok
    assert any("planned_action must be non-empty" in i for i in issues)
    assert any("expires" in i for i in issues)
    assert any("Invalid resolution" in i for i in issues)


# ---------------------------------------------------------------------------
# Optional task fields on existing types: allowed, never required

def test_plain_open_loop_still_validates_untouched(vault):
    new_open_loop(vault, "A loop with no task fields at all")
    report, issues = _validate(vault)
    assert report.ok, issues


def test_open_loop_task_fields_validate_when_present(vault):
    created = new_open_loop(vault, "Run the backup check")
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm.update(
        execute_asap=True,
        task_kind="run_script",
        task_payload={"script": "backup_check.sh", "args": []},
        task_status="pending",
    )
    write_markdown(created.path, fm, doc.body)
    report, issues = _validate(vault)
    assert report.ok, issues


def test_open_loop_malformed_task_fields_are_errors(vault):
    created = new_open_loop(vault, "Badly tasked loop")
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm.update(
        execute_asap="yes",
        task_kind="conquer",
        task_payload=["not", "an", "object"],
        task_status="vibing",
    )
    write_markdown(created.path, fm, doc.body)
    report, issues = _validate(vault)
    assert not report.ok
    assert any("execute_asap must be boolean" in i for i in issues)
    assert any("Invalid task_kind" in i for i in issues)
    assert any("task_payload must be an object" in i for i in issues)
    assert any("Invalid task_status" in i for i in issues)


def test_task_kind_without_status_is_unpollable(vault):
    created = new_open_loop(vault, "Half-tasked loop")
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm["task_kind"] = "research"
    write_markdown(created.path, fm, doc.body)
    report, issues = _validate(vault)
    assert not report.ok
    assert any("unpollable" in i for i in issues)


def test_decision_execution_steps_validate(vault):
    from lisan.tools.record_factory import new_decision

    created = new_decision(vault, "Adopt the new backup rotation")
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm["execution_steps"] = [
        {"step": "Write the rotation script", "task_kind": "draft", "status": "pending"},
        {"step": "", "task_kind": "sorcery", "status": "later", "task_payload": "nope"},
    ]
    write_markdown(created.path, fm, doc.body)
    report, issues = _validate(vault)
    assert not report.ok
    assert any("execution_steps[1] missing step description" in i for i in issues)
    assert any("execution_steps[1] invalid task_kind" in i for i in issues)
    assert any("execution_steps[1] invalid status" in i for i in issues)
    assert any("execution_steps[1] task_payload must be an object" in i for i in issues)
    assert not any("execution_steps[0]" in i for i in issues)


# ---------------------------------------------------------------------------
# Credential hygiene (spec §5): warn, never block

def test_credential_pattern_warns_but_does_not_fail(vault):
    created = new_open_loop(vault, "Rotate the leaked key")
    doc = load_markdown(created.path)
    write_markdown(created.path, dict(doc.frontmatter), doc.body + "\nFound AKIAIOSFODNN7EXAMPLE in the old config.\n")
    report, issues = _validate(vault)
    assert report.ok  # warning severity only
    assert any("Possible credential" in i for i in issues)


# ---------------------------------------------------------------------------
# Index: new columns, new tables, zero migration

def test_index_carries_task_columns(vault, tmp_path):
    created = new_open_loop(vault, "Indexed task loop")
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm.update(execute_asap=True, task_kind="run_script", task_payload={}, task_status="pending")
    write_markdown(created.path, fm, doc.body)
    sched = new_schedule(vault, "weekly digest", task_kind="draft", cron="weekly:mon@08:00", next_run="2026-07-27T08:00:00")

    db = tmp_path / "test.sqlite"
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    ensure_index_schema(conn)
    assert index_single_record(created.path, vault, conn)
    assert index_single_record(sched.path, vault, conn)
    conn.commit()
    row = conn.execute("SELECT * FROM files WHERE type='open_loop'").fetchone()
    assert row["execute_asap"] == 1 and row["task_kind"] == "run_script" and row["task_status"] == "pending"
    row = conn.execute("SELECT * FROM files WHERE type='schedule'").fetchone()
    assert row["next_run"] == "2026-07-27T08:00:00"
    conn.close()


def test_new_tables_exist(tmp_path):
    conn = db_connect(tmp_path / "t.sqlite")
    ensure_index_schema(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in NEW_TABLES:
        assert table in tables
    conn.close()


def test_confirmations_mirror_rebuilds_from_records(vault, tmp_path):
    """Ruling 2026-07-23: the mirror is derived state. A rebuild reflects
    exactly what the records say — hand edits win, stale rows die."""
    from lisan.tools.rebuild_index import rebuild_index

    loop = new_open_loop(vault, "Anchor for mirror test")
    loop_id = load_markdown(loop.path).frontmatter["id"]
    created = new_confirmation(
        vault, "Mirror me", task_id=loop_id, task_summary="s",
        planned_action="do the thing", risk="low", expires="2026-07-30",
    )
    db = tmp_path / "mirror.sqlite"
    emb = tmp_path / "emb.bin"
    rebuild_index(vault, db, emb)
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM confirmations").fetchone()
    assert row["status"] == "pending" and row["expires"] == "2026-07-30"

    # A stale runtime-ish row injected by hand must not survive rebuild...
    conn.execute(
        "INSERT INTO confirmations (id, task_id, status, created_at, expires) "
        "VALUES ('confirmation.ghost', 'open_loop.gone', 'pending', '2026-01-01', '2026-01-08')"
    )
    # ...and a record edited out-of-band (owner denies by hand) must win.
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm["status"] = "resolved"
    fm["resolution"] = "denied"
    write_markdown(created.path, fm, doc.body)
    conn.commit()
    conn.close()

    rebuild_index(vault, db, emb)
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM confirmations").fetchall()
    assert len(rows) == 1
    assert rows[0]["resolution"] == "denied"
    conn.close()


def test_zero_migration_from_pre_adjutant_database(vault, tmp_path):
    """A database created before WO-ADJUTANT gains the new columns and
    tables from ensure_index_schema alone — no migration step, no data
    loss, and a task record then indexes cleanly into it."""
    old_sql = SCHEMA_SQL
    for column, decl in [
        ("execute_asap", "INTEGER"),
        ("task_kind", "TEXT"),
        ("task_status", "TEXT"),
        ("next_run", "TEXT"),
        ("expires", "TEXT"),
    ]:
        old_sql = old_sql.replace(f",\n    {column} {decl}", "")
    for table in NEW_TABLES:
        start = old_sql.index(f"CREATE TABLE IF NOT EXISTS {table} (")
        end = old_sql.index(");", start) + 2
        old_sql = old_sql[:start] + old_sql[end:]
    old_sql = "\n".join(line for line in old_sql.splitlines() if not line.strip().startswith("--"))
    assert "task_kind" not in old_sql and "adjutant_log" not in old_sql

    db = tmp_path / "old.sqlite"
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(old_sql)
    # Pre-existing data must survive.
    plain = new_open_loop(vault, "Pre-adjutant loop")
    index_columns_before = {str(r[1]) for r in conn.execute("PRAGMA table_info(files)")}
    assert "task_kind" not in index_columns_before
    conn.execute(
        "INSERT INTO files (id, type, path, created, updated, status) VALUES ('x', 'open_loop', 'p', '2026-01-01', '2026-01-01', 'active')"
    )
    conn.commit()

    ensure_index_schema(conn)
    columns_after = {str(r[1]) for r in conn.execute("PRAGMA table_info(files)")}
    for column in NEW_COLUMNS:
        assert column in columns_after
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in NEW_TABLES:
        assert table in tables
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1  # survived

    assert index_single_record(plain.path, vault, conn)
    conn.commit()
    row = conn.execute("SELECT task_kind, execute_asap FROM files WHERE type='open_loop' AND id != 'x'").fetchone()
    assert row["task_kind"] is None and row["execute_asap"] is None
    conn.close()
