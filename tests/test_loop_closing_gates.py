from __future__ import annotations

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

