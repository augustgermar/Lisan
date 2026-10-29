"""The same title on a later day is either the same live record or a new
one with a free id — never a rejected write that silently drops it."""
from __future__ import annotations

import pytest

from lisan.frontmatter import load_markdown, write_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools import record_factory
from lisan.tools.record_factory import new_evidence, new_open_loop


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    ensure_vault_layout(v)
    monkeypatch.setenv("LISAN_DATA_HOME", str(tmp_path / "data"))
    return v


def _on(monkeypatch, day):
    monkeypatch.setattr(record_factory, "today_iso", lambda: day)


def test_a_live_loop_mentioned_again_is_the_same_loop(vault, monkeypatch):
    _on(monkeypatch, "2026-10-01")
    first = new_open_loop(vault=vault, title="Book dentist", next_action="Call the office")
    _on(monkeypatch, "2026-10-02")
    with pytest.raises(FileExistsError):
        new_open_loop(vault=vault, title="Book dentist", next_action="Call the office")
    assert len(list((vault / "open_loops").glob("*.md"))) == 1
    assert first.path.exists()


def test_a_resolved_loop_does_not_block_a_new_one(vault, monkeypatch):
    _on(monkeypatch, "2026-10-01")
    first = new_open_loop(vault=vault, title="Book dentist", next_action="Call the office")
    doc = load_markdown(first.path)
    write_markdown(first.path, {**doc.frontmatter, "status": "resolved"}, doc.body)
    _on(monkeypatch, "2026-11-01")
    second = new_open_loop(vault=vault, title="Book dentist", next_action="Call the office")
    assert load_markdown(second.path).frontmatter["id"] == "open_loop.book-dentist-2"


def test_recurring_evidence_gets_a_fresh_id(vault, monkeypatch):
    _on(monkeypatch, "2026-10-01")
    new_evidence(vault, "Check-in recorded", source_type="conversation")
    _on(monkeypatch, "2026-10-02")
    second = new_evidence(vault, "Check-in recorded", source_type="conversation")
    assert load_markdown(second.path).frontmatter["id"] == "evidence.check-in-recorded-2"
