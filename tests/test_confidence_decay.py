"""`lisan decay`: the deterministic confidence-decay report (spec 10.2).

Each rule is a plain SQL or frontmatter predicate, so each gets a record that
must be reported and a near-miss that must not.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from lisan.frontmatter import write_markdown
from lisan.tools.confidence_decay import detect_decay_candidates
from lisan.tools.rebuild_index import ensure_index_schema


def _days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "state").mkdir(parents=True)
    return root


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "lisan.sqlite"
    conn = sqlite3.connect(path)
    ensure_index_schema(conn)
    conn.commit()
    conn.close()
    return path


def _add_file(db_path: Path, record_id: str, **fields: object) -> None:
    row = {
        "id": record_id,
        "type": "knowledge",
        "path": f"knowledge/{record_id}.md",
        "created": _days_ago(10),
        "updated": _days_ago(10),
        "status": "active",
        "summary": f"summary of {record_id}",
    }
    row.update(fields)
    conn = sqlite3.connect(db_path)
    conn.execute(
        f"INSERT INTO files ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )
    conn.commit()
    conn.close()


def _add_claim(db_path: Path, claim_id: str, episode_id: str, status: str, created: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO claims (id, episode_id, claim_text, claim_type, confidence, status, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (claim_id, episode_id, "a claim", "factual", "medium", status, created),
    )
    conn.commit()
    conn.close()


def test_missing_index_is_reported_not_raised(vault: Path, tmp_path: Path) -> None:
    assert detect_decay_candidates(vault=vault, db_path=tmp_path / "absent.sqlite") == "SQLite index not found.\n"


def test_empty_index_reports_no_candidates(vault: Path, db_path: Path) -> None:
    report = detect_decay_candidates(vault=vault, db_path=db_path)
    assert report.startswith("# Confidence Decay Candidates\n")
    assert f"generated: {date.today().isoformat()}" in report
    assert "No decay candidates found." in report
    assert "## Rule" not in report


def test_rule1_flags_unconfirmed_medium_and_high_confidence(vault: Path, db_path: Path) -> None:
    _add_file(db_path, "old-high", confidence="high", last_confirmed=_days_ago(200))
    _add_file(db_path, "never-confirmed", confidence="medium", last_confirmed=None)
    _add_file(db_path, "recent", confidence="high", last_confirmed=_days_ago(30))
    _add_file(db_path, "low-conf", confidence="low", last_confirmed=_days_ago(400))
    _add_file(db_path, "archived", confidence="high", last_confirmed=_days_ago(400), status="archived")

    report = detect_decay_candidates(vault=vault, db_path=db_path)

    assert "## Rule 1" in report
    assert "`old-high` | knowledge | high" in report
    assert "`never-confirmed` | knowledge | medium | last_confirmed=None" in report
    assert "summary of old-high" in report
    for untouched in ("`recent`", "`low-conf`", "`archived`"):
        assert untouched not in report
    assert "No decay candidates found." not in report


def test_rule1_truncates_long_summaries(vault: Path, db_path: Path) -> None:
    _add_file(db_path, "wordy", confidence="high", last_confirmed=_days_ago(200), summary="x" * 200)
    report = detect_decay_candidates(vault=vault, db_path=db_path)
    assert "x" * 80 in report
    assert "x" * 81 not in report


def test_rule2_flags_disputed_and_long_unresolved_claims(vault: Path, db_path: Path) -> None:
    _add_file(db_path, "ep-1", type="episode", confidence="low")
    _add_claim(db_path, "claim-disputed", "ep-1", "disputed", _days_ago(5))
    _add_claim(db_path, "claim-old-unresolved", "ep-1", "unresolved", _days_ago(120))
    _add_claim(db_path, "claim-new-unresolved", "ep-1", "unresolved", _days_ago(10))
    _add_claim(db_path, "claim-settled", "ep-1", "confirmed", _days_ago(400))

    report = detect_decay_candidates(vault=vault, db_path=db_path)

    assert "## Rule 2" in report
    assert "`claim-disputed` | factual | medium | disputed | file: `ep-1`" in report
    assert "`claim-old-unresolved`" in report
    assert "`claim-new-unresolved`" not in report
    assert "`claim-settled`" not in report


def test_rule2_reports_standalone_claims_with_no_linked_episode(vault: Path, db_path: Path) -> None:
    """A claim record with no linked episode has an empty episode_id; it must still surface."""
    _add_claim(db_path, "claim-orphan", "", "disputed", _days_ago(5))
    report = detect_decay_candidates(vault=vault, db_path=db_path)
    assert "`claim-orphan` | factual | medium | disputed | file: `none`" in report


def test_rule3_flags_state_files_past_their_ttl(vault: Path, db_path: Path) -> None:
    def state(name: str, **fm: object) -> None:
        write_markdown(vault / "state" / f"{name}.md", {"type": "state", **fm}, "body")

    state("expired", ttl_days=30, updated=_days_ago(45), confidence="medium")
    state("fresh", ttl_days=30, updated=_days_ago(5), confidence="medium")
    state("already-stale", ttl_days=30, updated=_days_ago(45), confidence="stale")
    state("no-ttl", updated=_days_ago(900), confidence="high")
    state("bad-date", ttl_days=30, updated="not-a-date", confidence="high")
    (vault / "state" / "unparseable.md").write_text("---\n{not json\n---\n", encoding="utf-8")

    report = detect_decay_candidates(vault=vault, db_path=db_path)

    assert "## Rule 3" in report
    assert "`expired.md` | age=45 | ttl=30 | current_confidence=medium" in report
    for untouched in ("fresh.md", "already-stale.md", "no-ttl.md", "bad-date.md", "unparseable.md"):
        assert untouched not in report


def test_rule4_flags_old_high_significance_episodes(vault: Path, db_path: Path) -> None:
    old = {"type": "episode", "significance": "high", "confidence": "high", "created": _days_ago(800)}
    _add_file(db_path, "ep-old", **old, last_confirmed=None)
    _add_file(db_path, "ep-reconfirmed", **old, last_confirmed=_days_ago(300))
    _add_file(db_path, "ep-minor", **{**old, "significance": "low"}, last_confirmed=None)
    _add_file(db_path, "ep-young", **{**old, "created": _days_ago(100)}, last_confirmed=None)

    report = detect_decay_candidates(vault=vault, db_path=db_path)
    rule4 = report.split("## Rule 4", 1)[1]

    assert f"`ep-old` | created={_days_ago(800)} | last_confirmed=None" in rule4
    for untouched in ("ep-reconfirmed", "ep-minor", "ep-young"):
        assert untouched not in rule4
