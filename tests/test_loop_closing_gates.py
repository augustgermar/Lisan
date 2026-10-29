from __future__ import annotations

from lisan.tools.memory_pipeline import _skeptic_approves
from lisan.tools.record_fanout import _looks_like_completion, close_completed_open_loops


def test_completion_markers_match_whole_words_only():
    assert _looks_like_completion("I sent the form")
    assert not _looks_like_completion("I will present the form tomorrow")
    assert not _looks_like_completion("the plan was abandoned")
    assert not _looks_like_completion("she recalled the meeting")


def test_closing_never_raises_into_the_pipeline(tmp_path, monkeypatch):
    from lisan.tools import record_fanout

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(record_fanout, "_close_matching_open_loops", boom)
    assert close_completed_open_loops(tmp_path, "done with it", draft_rel="d.md") is None


def test_high_risk_revise_is_held_per_spec_10_1_8():
    assert _skeptic_approves({"recommended_action": "revise", "risk": "low", "approved": False})
    assert not _skeptic_approves({"recommended_action": "revise", "risk": "high", "approved": False})
    assert _skeptic_approves({"recommended_action": "approve", "risk": "high", "approved": True})
