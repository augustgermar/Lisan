"""WO-ADJUTANT step 6: cadence math, jobs materialization, daemon lock,
halt edge-ping, fswatch.

Binding claims: cadence is computed from the record's cron string every
time (never stored in the DB); indexing a schedule materializes exactly
one alarm job; two daemons on one vault is an error while one lives and
a dead lock is reclaimed; a repeating halt pings the owner once, on the
edge; fswatch feeds capture and nothing else.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from lisan.frontmatter import dump_markdown, load_markdown, write_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.adjutant_common import next_cron_occurrence, next_cron_stamp
from lisan.tools.adjutant_daemon import (
    DaemonLockError,
    acquire_lock,
    lockfile_path,
    release_lock,
)
from lisan.tools.adjutant_runner import run_cycle
from lisan.tools.db import connect as db_connect
from lisan.tools.fswatch import fswatch_scan
from lisan.tools.intent import _record_known_hash, init_intent, intent_path
from lisan.tools.rebuild_index import ensure_index_schema, index_single_record
from lisan.tools.record_factory import new_schedule


@pytest.fixture()
def vault(tmp_path):
    v = tmp_path / "vault"
    ensure_vault_layout(v)
    return v


# ---------------------------------------------------------------------------
# Cadence math (local-tz aware; computed, never stored)

def _at(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_weekly_cadence_advances_to_named_weekday():
    # 2026-07-23 is a Thursday (UTC); next mon strictly after it.
    nxt = next_cron_occurrence("weekly:mon@08:00", after=_at(2026, 7, 23, 12, 0))
    local = nxt.astimezone()
    assert local.weekday() == 0
    assert (local.hour, local.minute) == (8, 0)
    # Firing exactly at the moment advances a full week.
    again = next_cron_occurrence("weekly:mon@08:00", after=nxt)
    assert (again - nxt).days == 7


def test_monthly_cadence_rolls_month_and_year():
    nxt = next_cron_occurrence("monthly:15@09:30", after=_at(2026, 7, 20, 12, 0))
    local = nxt.astimezone()
    assert (local.day, local.hour, local.minute) == (15, 9, 30)
    assert local.month in (8, 9)  # strictly after July 20 local
    december = next_cron_occurrence("monthly:15@09:30", after=_at(2026, 12, 20, 12, 0))
    assert december.astimezone().year == 2027


def test_every_and_daily_delegate_to_scheduler():
    nxt = next_cron_occurrence("every:30m", after=_at(2026, 7, 23, 12, 0))
    assert (nxt - _at(2026, 7, 23, 12, 0)).total_seconds() == 1800
    assert next_cron_stamp("daily@08:00")  # renders without error


def test_unknown_cadence_raises():
    with pytest.raises(ValueError):
        next_cron_occurrence("whenever-i-feel-like-it")


# ---------------------------------------------------------------------------
# Materialization: record -> one alarm job

def test_indexing_schedule_materializes_one_alarm_job(vault, tmp_path):
    from lisan.tools.rebuild_index import reindex_record

    db = tmp_path / "m.sqlite"
    created = new_schedule(
        vault, "weekly digest", task_kind="draft", cron="weekly:mon@08:00",
        next_run="2030-01-07T08:00:00", payload={"instructions": "digest"},
    )
    # Re-index three times: still exactly one queued alarm (coalesced).
    for _ in range(3):
        reindex_record(created.path, vault, db)
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM jobs WHERE job_type='adjutant.cycle' AND status='queued'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["scheduled_for"].startswith("2030-01-07")
    payload = json.loads(rows[0]["payload_json"])
    assert payload["schedule_id"] == "schedule.weekly-digest"
    conn.close()


def test_editing_schedule_earlier_pulls_the_alarm_forward(vault, tmp_path):
    """The direction an owner would notice and mind: editing next_run to
    fire SOONER must update the queued alarm, not leave the stale later
    one. (Coalescing keeps the earliest scheduled_for; the later-edit
    inverse costs one no-op cycle by ratified trade.)"""
    from lisan.tools.rebuild_index import reindex_record

    db = tmp_path / "e.sqlite"
    created = new_schedule(
        vault, "monthly sweep", task_kind="draft", cron="monthly:1@09:00",
        next_run="2030-06-01T09:00:00", payload={"instructions": "sweep"},
    )
    reindex_record(created.path, vault, db)
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm["next_run"] = "2030-02-01T09:00:00"  # owner wants it sooner
    write_markdown(created.path, fm, doc.body)
    reindex_record(created.path, vault, db)
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM jobs WHERE job_type='adjutant.cycle' AND status='queued'").fetchall()
    assert len(rows) == 1
    assert rows[0]["scheduled_for"].startswith("2030-02-01")
    conn.close()


def test_weekly_schedule_unparks_after_run(vault, tmp_path):
    """The step-4 parking behavior is gone: weekly cadences now advance."""
    init_intent(vault)
    path = intent_path(vault)
    doc = load_markdown(path)
    delegations = {
        "defaults": {"mode": "report_only"},
        # Deliberately the LEGACY "arenas" spelling: an authority document
        # the owner already adopted must keep resolving after the rename.
        "arenas": {"work": {"mode": "execute", "capabilities": ["read_files", "write_files"]}},
        "global": {"max_tasks_per_cycle": 5, "max_task_wall_seconds": 30},
    }
    body = doc.body
    start = body.index("```json")
    end = body.index("```", start + 7) + 3
    body = body[:start] + "```json\n" + json.dumps(delegations, indent=2) + "\n```" + body[end:]
    fm = dict(doc.frontmatter)
    fm.update(created="2026-07-23", updated="2026-07-23", review_after="2026-10-23")
    path.write_text(dump_markdown(fm, body), encoding="utf-8")
    _record_known_hash(vault)

    db = tmp_path / "u.sqlite"
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    ensure_index_schema(conn)
    created = new_schedule(
        vault, "weekly tick", task_kind="draft", cron="weekly:mon@08:00",
        next_run="2020-01-06T08:00:00", payload={"title": "t", "instructions": "tick"},
        scope="work",
    )
    index_single_record(created.path, vault, conn)
    conn.commit()
    conn.close()
    result = run_cycle(
        vault, db,
        config={"adjutant": {"enabled": True}},
        complete=lambda p: "tick content",
        capture=lambda **kw: {},
    )
    assert result["executed"][0]["ok"]
    next_run = load_markdown(created.path).frontmatter["next_run"]
    assert next_run != ""  # not parked
    assert next_run > "2026-01-01"  # advanced into the future


# ---------------------------------------------------------------------------
# Daemon lock

def test_second_daemon_on_one_vault_is_an_error(vault):
    acquire_lock(vault, pid=11111111)  # a pid this test controls...
    # ...except pid 11111111 is almost certainly dead, so simulate a live
    # owner with our own pid instead.
    import os

    acquire_lock(vault, pid=os.getpid())
    with pytest.raises(DaemonLockError, match="two daemons"):
        acquire_lock(vault, pid=os.getpid() + 1 if os.getpid() < 2**22 else 4242)
    release_lock(vault, pid=os.getpid())
    assert not lockfile_path(vault).exists()


def test_stale_lock_from_dead_pid_is_reclaimed(vault):
    lockfile_path(vault).write_text("999999999\n", encoding="utf-8")  # dead pid
    path = acquire_lock(vault, pid=123)
    assert path.read_text(encoding="utf-8").strip() == "123"


# ---------------------------------------------------------------------------
# Halt edge-ping

def test_repeated_halt_pings_owner_once(vault, tmp_path):
    init_intent(vault)  # sentinel template
    p = intent_path(vault)
    p.write_text(p.read_text(encoding="utf-8").replace("# Never", "# Nah"), encoding="utf-8")
    db = tmp_path / "h.sqlite"
    pings = []
    for _ in range(3):
        result = run_cycle(vault, db, config={"adjutant": {"enabled": True}}, deliver=pings.append)
        assert result["halted"]
    halts = [p_ for p_ in pings if "Adjutant halted" in p_]
    assert len(halts) == 1  # edge-triggered, not per-cycle
    # A different halt reason pings again.
    fixed = intent_path(vault)
    text = fixed.read_text(encoding="utf-8").replace("# Nah", "# Never")
    fixed.write_text(text, encoding="utf-8")
    result = run_cycle(vault, db, config={"adjutant": {"enabled": True}}, deliver=pings.append)
    assert result["halted"] and "sentinel" in result["reason"]
    halts = [p_ for p_ in pings if "Adjutant halted" in p_]
    assert len(halts) == 2


# ---------------------------------------------------------------------------
# fswatch: capture-only

def test_fswatch_new_and_changed_files_become_capture_turns(vault, tmp_path):
    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "notes.md").write_text("hello vault", encoding="utf-8")
    (watched / "photo.jpg").write_bytes(b"\xff\xd8binary")
    db = tmp_path / "f.sqlite"
    config = {"ingest": {"fswatch_paths": [str(watched)]}}
    turns = []

    def capture(**kw):
        turns.append(kw)
        return {}

    captured = fswatch_scan(vault, db, config=config, capture=capture)
    assert len(captured) == 2
    assert all(t["conversation_id"] == "fswatch" for t in turns)
    md_turn = next(t for t in turns if "notes.md" in t["text"])
    assert "hello vault" in md_turn["text"]
    assert "not an instruction" in md_turn["text"]

    # Unchanged: silent second pass.
    assert fswatch_scan(vault, db, config=config, capture=capture) == []
    # Changed: captured again.
    (watched / "notes.md").write_text("hello vault, revised and longer", encoding="utf-8")
    captured = fswatch_scan(vault, db, config=config, capture=capture)
    assert captured == [str(watched / "notes.md")]
    assert "changed file" in turns[-1]["text"]


def test_fswatch_without_paths_is_inert(vault, tmp_path):
    assert fswatch_scan(vault, tmp_path / "x.sqlite", config={"ingest": {"fswatch_paths": []}}, capture=None) == []


# ── executor reachability (2026-08-14) ────────────────────────────────────────

def test_executor_readiness_reports_an_unreachable_binary(monkeypatch):
    """The Adjutant is installed from a hand-written plist in
    docs/adjutant_daemon.md, which set LISAN_VAULT and no PATH. launchd's
    default is /usr/bin:/bin:/usr/sbin:/sbin, so codex in /usr/local/bin was
    invisible: this install polled, gated and logged verdicts for three weeks
    while being structurally unable to execute anything. It cost nothing only
    because cycles were dry.
    """
    from lisan.tools.adjutant_daemon import executor_readiness

    monkeypatch.setattr("shutil.which", lambda binary: None)
    state = executor_readiness({"routing": {"executor": {"medium": "codex"}}})
    assert state["checked"] is True
    assert state["reachable"] is False
    assert state["binary"] == "codex"


def test_executor_readiness_reports_a_reachable_binary(monkeypatch):
    from lisan.tools.adjutant_daemon import executor_readiness

    monkeypatch.setattr("shutil.which", lambda binary: "/usr/local/bin/codex")
    state = executor_readiness({"routing": {"executor": {"medium": "codex"}}})
    assert state["reachable"] is True
    assert state["path"] == "/usr/local/bin/codex"


def test_executor_readiness_honours_the_binary_env_override(monkeypatch):
    from lisan.tools.adjutant_daemon import executor_readiness

    monkeypatch.setenv("CODEX_BIN", "/opt/custom/codex")
    seen = {}

    def _which(binary):
        seen["binary"] = binary
        return binary

    monkeypatch.setattr("shutil.which", _which)
    state = executor_readiness({"routing": {"executor": {"medium": "codex"}}})
    assert seen["binary"] == "/opt/custom/codex"
    assert state["reachable"] is True


def test_a_non_codex_executor_is_not_probed_for_a_binary(monkeypatch):
    """Only the CLI-backed executor has a binary to find on PATH."""
    from lisan.tools.adjutant_daemon import executor_readiness

    state = executor_readiness({"routing": {"executor": {"medium": "rotato"}}})
    assert state["checked"] is False
    assert state["reachable"] is True


def test_the_shipped_plist_documents_a_PATH():
    """The doc is the artifact people copy — the fix has to live there too."""
    from pathlib import Path

    doc = Path(__file__).resolve().parents[1] / "docs" / "adjutant_daemon.md"
    text = doc.read_text(encoding="utf-8")
    assert "<key>PATH</key>" in text
    assert "/usr/local/bin" in text
