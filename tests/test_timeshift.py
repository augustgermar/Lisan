"""WO-9 tooling: longitudinal compression by aging records."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_EVALS = Path(__file__).resolve().parents[1] / "evals"


def _load():
    spec = importlib.util.spec_from_file_location("timeshift", _EVALS / "timeshift.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["timeshift"] = module
    spec.loader.exec_module(module)
    return module


timeshift = _load()


def test_shifts_record_dates_and_transcripts(tmp_path):
    from lisan.frontmatter import load_markdown, write_markdown

    write_markdown(
        tmp_path / "open_loops" / "a.md",
        {"id": "open_loop.a", "type": "open_loop", "created": "2026-07-04",
         "updated": "2026-07-04", "status": "active", "summary": "s", "links": []},
        "body",
    )
    (tmp_path / "transcripts").mkdir()
    write_markdown(tmp_path / "transcripts" / "2026-07-04.md", {"date": "2026-07-04"}, "## Conversation — 09:00\n\nUSER: hi\n")

    result = timeshift.shift_vault(tmp_path, 14)
    assert result["records_shifted"] == 1
    assert result["transcripts_shifted"] == 1
    fm = load_markdown(tmp_path / "open_loops" / "a.md").frontmatter
    assert fm["created"] == "2026-06-20"
    assert (tmp_path / "transcripts" / "2026-06-20.md").exists()
    assert not (tmp_path / "transcripts" / "2026-07-04.md").exists()


def test_shift_ages_loops_into_callback_range(tmp_path):
    from datetime import date

    from lisan.frontmatter import write_markdown
    from lisan.tools.drive import loop_score

    write_markdown(
        tmp_path / "open_loops" / "b.md",
        {"id": "open_loop.b", "type": "open_loop", "created": "2026-07-04",
         "updated": "2026-07-04", "status": "active", "significance": "low",
         "summary": "s", "links": []},
        "body",
    )
    now = date(2026, 7, 4)
    from lisan.frontmatter import load_markdown

    before = loop_score(load_markdown(tmp_path / "open_loops" / "b.md").frontmatter, now)
    timeshift.shift_vault(tmp_path, 14)
    after = loop_score(load_markdown(tmp_path / "open_loops" / "b.md").frontmatter, now)
    assert before < 2.0 <= after  # two weeks of simulated age earns the callback
    # (the tension window is real: by ~day 20 unrefreshed decay wins again)


def test_refuses_live_vault_without_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(timeshift, "LIVE_VAULT", tmp_path)
    with pytest.raises(RuntimeError, match="live vault"):
        timeshift.shift_vault(tmp_path, 7)
