from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lisan.frontmatter import load_markdown, write_markdown
from lisan.tools.validation_gate import compare_to_baseline, write_baseline
from lisan.tools.validator import validate_vault
from lisan.tools.write_boundary import RecordWriteRejected


def _valid(record_id: str, **changes):
    value = {
        "id": record_id,
        "type": "knowledge",
        "created": "2026-10-01",
        "updated": "2026-10-01",
        "status": "active",
        "significance": "low",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "summary": "A valid record.",
        "links": [],
        "confidence": "high",
        "confidence_basis": "Boundary test",
        "last_confirmed": "2026-10-01",
        "review_after": "2027-10-01",
    }
    value.update(changes)
    return value


@pytest.fixture()
def isolated_data(monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setenv("LISAN_DATA_HOME", str(data))
    monkeypatch.delenv("LISAN_TEST_RAW_RECORD_WRITES", raising=False)
    return data


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda fm: fm.pop("summary"), "Missing required frontmatter"),
        (lambda fm: fm.update(type="made_up"), "Unsupported type"),
        (lambda fm: fm.update(created="October 1"), "Invalid ISO date"),
        (lambda fm: fm.update(links="knowledge.target"), "links must be a list"),
        (lambda fm: fm.update(links=["knowledge.absent"]), "target does not exist"),
        (lambda fm: fm.update(links=["This is prose, not a record id."]), "entry is prose"),
    ],
)
def test_known_bad_new_record_shapes_are_rejected(isolated_data, tmp_path, mutate, message):
    path = tmp_path / "vault" / "knowledge" / "bad.md"
    fm = _valid("knowledge.bad")
    mutate(fm)
    with pytest.raises(RecordWriteRejected, match=message):
        write_markdown(path, fm, "# Bad\n")
    assert not path.exists()


def test_duplicate_id_is_rejected_and_original_survives(isolated_data, tmp_path):
    vault = tmp_path / "vault"
    first = vault / "knowledge" / "first.md"
    second = vault / "knowledge" / "second.md"
    write_markdown(first, _valid("knowledge.same"), "# First\n")
    with pytest.raises(RecordWriteRejected, match="Duplicate id"):
        write_markdown(second, _valid("knowledge.same"), "# Second\n")
    assert load_markdown(first).body.startswith("# First")
    assert not second.exists()


def test_concurrent_id_allocation_has_exactly_one_winner(isolated_data, tmp_path, monkeypatch):
    from lisan.tools import write_boundary

    vault = tmp_path / "vault"
    barrier = threading.Barrier(2)
    real_new_errors = write_boundary._new_errors

    def validate_then_wait(*args, **kwargs):
        result = real_new_errors(*args, **kwargs)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(write_boundary, "_new_errors", validate_then_wait)

    def attempt(name):
        try:
            write_markdown(
                vault / "knowledge" / f"{name}.md",
                _valid("knowledge.concurrent"),
                f"# {name}\n",
            )
            return "written"
        except RecordWriteRejected:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["one", "two"]))
    assert sorted(outcomes) == ["rejected", "written"]
    assert len(list((vault / "knowledge").glob("*.md"))) == 1


def test_id_reservation_is_committed_transactionally(isolated_data, tmp_path):
    vault = tmp_path / "vault"
    path = vault / "knowledge" / "one.md"
    write_markdown(path, _valid("knowledge.one"), "# One\n")
    conn = sqlite3.connect(isolated_data / "lisan.sqlite")
    try:
        row = conn.execute(
            "SELECT record_path, state FROM record_id_reservations WHERE record_id=?",
            ("knowledge.one",),
        ).fetchone()
    finally:
        conn.close()
    assert row == ("knowledge/one.md", "committed")


def test_stale_renamed_reservation_is_healed_only_when_index_confirms_target(isolated_data, tmp_path):
    vault = tmp_path / "vault"
    path = vault / "knowledge" / "renamed.md"
    write_markdown(path, _valid("knowledge.renamed"), "# Original\n")
    conn = sqlite3.connect(isolated_data / "lisan.sqlite")
    try:
        conn.execute("CREATE TABLE files (id TEXT PRIMARY KEY, path TEXT NOT NULL)")
        conn.execute("INSERT INTO files (id, path) VALUES (?, ?)", ("knowledge.renamed", "knowledge/renamed.md"))
        conn.execute(
            "UPDATE record_id_reservations SET record_path=? WHERE record_id=?",
            ("knowledge/old-name.md", "knowledge.renamed"),
        )
        conn.commit()
    finally:
        conn.close()

    write_markdown(path, _valid("knowledge.renamed", summary="Updated."), "# Updated\n")

    conn = sqlite3.connect(isolated_data / "lisan.sqlite")
    try:
        row = conn.execute(
            "SELECT record_path, state FROM record_id_reservations WHERE record_id=?",
            ("knowledge.renamed",),
        ).fetchone()
    finally:
        conn.close()
    assert row == ("knowledge/renamed.md", "committed")
    assert load_markdown(path).frontmatter["summary"] == "Updated."


def test_finalize_failure_restores_file_and_releases_reservation(isolated_data, tmp_path, monkeypatch):
    from lisan.tools import write_boundary

    vault = tmp_path / "vault"
    path = vault / "knowledge" / "one.md"
    write_markdown(path, _valid("knowledge.one"), "# Original\n")
    original = path.read_bytes()
    real_finish = write_boundary._finish

    def fail_finish(_reservation):
        raise RuntimeError("simulated finalize failure")

    monkeypatch.setattr(write_boundary, "_finish", fail_finish)
    with pytest.raises(RuntimeError, match="simulated finalize"):
        write_markdown(path, _valid("knowledge.one", summary="Changed"), "# Changed\n")
    assert path.read_bytes() == original

    monkeypatch.setattr(write_boundary, "_finish", real_finish)
    # The prior committed reservation was restored, so a normal retry works.
    write_markdown(path, _valid("knowledge.one", summary="Changed"), "# Changed\n")
    assert load_markdown(path).frontmatter["summary"] == "Changed"


def test_existing_legacy_record_may_only_reduce_its_error_set(isolated_data, tmp_path):
    vault = tmp_path / "vault"
    path = vault / "knowledge" / "legacy.md"
    path.parent.mkdir(parents=True)
    # Deliberate pre-boundary fixture: missing summary.
    path.write_text(
        "---\n" + json.dumps({k: v for k, v in _valid("knowledge.legacy").items() if k != "summary"}) + "\n---\n\n# Legacy\n",
        encoding="utf-8",
    )
    unchanged = load_markdown(path)
    write_markdown(path, unchanged.frontmatter, unchanged.body + "\nMore detail.\n")
    worsened = dict(unchanged.frontmatter)
    worsened["created"] = "yesterday"
    with pytest.raises(RecordWriteRejected, match="Invalid ISO date"):
        write_markdown(path, worsened, unchanged.body)
    repaired = dict(unchanged.frontmatter)
    repaired["summary"] = "Repaired."
    write_markdown(path, repaired, unchanged.body)
    assert validate_vault(vault).ok


def test_validation_gate_allows_resolutions_but_rejects_new_errors(isolated_data, tmp_path):
    vault = tmp_path / "vault"
    baseline = tmp_path / "baseline.json"
    write_markdown(vault / "knowledge" / "good.md", _valid("knowledge.good"), "# Good\n")
    write_baseline(baseline, validate_vault(vault), vault)
    assert compare_to_baseline(vault, baseline).ok

    # Simulate damage outside the boundary so the gate itself is tested.
    bad = vault / "knowledge" / "bypass.md"
    bad.write_text("---\n{\"id\": \"knowledge.bypass\", \"type\": \"knowledge\"}\n---\n", encoding="utf-8")
    result = compare_to_baseline(vault, baseline)
    assert not result.ok
    assert sum(result.new_errors.values()) > 0
