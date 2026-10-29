"""The first-person record says what happened — once."""
from __future__ import annotations

from lisan.frontmatter import write_markdown
from lisan.tools import kernel
from lisan.tools.self_episodes import ceremony_events


def test_an_unratified_extraction_is_not_narrated_as_ratified(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    write_markdown(reports / "voice-extraction-20260801.md", {"voice_extraction": {}}, "x")
    write_markdown(reports / "voice-extraction-20260901.md",
                   {"voice_extraction": {}, "ratified_at": "2026-09-02"}, "x")
    events = {e.event_id: e for e in ceremony_events(tmp_path)}
    assert events["ceremony-20260801"].outcome == "proposed"
    assert events["ceremony-20260901"].outcome == "ratified"


def test_kernel_provenance_also_counts_as_ratification(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    write_markdown(reports / "voice-extraction-20260801.md", {"voice_extraction": {}}, "x")
    core = tmp_path / "identity-core.md"
    core.write_text("## Voice Provenance\n\n- artifact: voice-extraction-20260801.md\n", encoding="utf-8")
    monkeypatch.setattr(kernel, "kernel_path", lambda vault: core)
    (event,) = ceremony_events(tmp_path)
    assert event.outcome == "ratified"


def test_one_hand_edit_is_one_drift_line(tmp_path):
    for _ in range(5):  # verify_kernel runs every turn
        kernel._record_drift(tmp_path, stored="a" * 64, actual="b" * 64)
    lines = [l for l in (tmp_path / "reports" / "kernel-drift.md").read_text().splitlines() if l.startswith("- ")]
    assert len(lines) == 1
    kernel._record_drift(tmp_path, stored="b" * 64, actual="c" * 64)  # a second, different edit
    lines = [l for l in (tmp_path / "reports" / "kernel-drift.md").read_text().splitlines() if l.startswith("- ")]
    assert len(lines) == 2
