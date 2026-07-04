"""WO-5: the drive system's failure modes are mechanically impossible.

Closed loops never surface; cooldown and the one-per-session cap hold;
staked loops outrank unstaked; tension decays to zero; phrasing is
interrogative by construction; and the metrics markers land in the log.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from lisan.frontmatter import load_markdown, write_markdown
from lisan.tools.drive import loop_score, phrase_question, scored_loops, session_open_callback

NOW = date(2026, 7, 10)


@pytest.fixture(autouse=True)
def _fresh_logger():
    """The lisan logger is a module-global singleton bound to the first
    vault that touches it; rebind per test so log assertions see this
    test's vault."""
    import logging

    import lisan.tools.log as log_mod

    def reset():
        log_mod._logger = None
        logger = logging.getLogger("lisan")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    reset()
    yield
    reset()


def _loop(vault: Path, name: str, **overrides) -> Path:
    fm = {
        "id": f"open_loop.{name}",
        "type": "open_loop",
        "created": "2026-07-01",
        "updated": "2026-07-08",
        "status": "active",
        "significance": "medium",
        "priority": "medium",
        "summary": f"the {name} thing",
        "links": [],
    }
    fm.update(overrides)
    path = vault / "open_loops" / f"{name}.md"
    write_markdown(path, fm, "body")
    return path


def test_resolved_and_archived_loops_never_surface(tmp_path):
    _loop(tmp_path, "done", status="resolved")
    _loop(tmp_path, "gone", status="archived")
    assert scored_loops(tmp_path, NOW) == []
    assert session_open_callback(tmp_path, "c1", now=NOW) is None


def test_staked_loops_outrank_unstaked(tmp_path):
    _loop(tmp_path, "user-reminder")
    _loop(tmp_path, "my-own-thread", links=["self_episode.job-9"])
    loops = scored_loops(tmp_path, NOW)
    assert loops[0]["frontmatter"]["id"] == "open_loop.my-own-thread"
    assert loops[0]["score"] > loops[1]["score"]


def test_tension_decays_to_zero_without_refresh(tmp_path):
    stale = {"created": "2026-01-01", "updated": "2026-01-01"}
    assert loop_score({**stale, "significance": "high"}, NOW, max_age_days=45) == 0.0
    fresh = loop_score({"created": "2026-01-01", "updated": str(NOW), "significance": "high"}, NOW)
    assert fresh > 0


def test_question_phrasing_is_interrogative(tmp_path):
    q = phrase_question({"summary": "the fastembed failure we never confirmed."})
    assert q.endswith("?")
    assert "did that ever" in q.lower()
    assert "fastembed failure" in q


def test_agent_owned_loops_are_attributed_to_the_agent(tmp_path):
    q = phrase_question({"summary": "my provider keys were rotating oddly", "owner": "agent"})
    assert q.endswith("?")
    assert "note of my own" in q
    assert "you mentioned" not in q


def test_one_callback_per_session_and_cooldown_stamp(tmp_path):
    path = _loop(tmp_path, "alpha", links=["self_episode.job-1"])
    _loop(tmp_path, "beta")
    q1 = session_open_callback(tmp_path, "c1", now=NOW)
    assert q1 is not None and "alpha" in q1
    assert load_markdown(path).frontmatter["last_callback"]
    # Same "day", next session: alpha is in cooldown; beta is next-best.
    q2 = session_open_callback(tmp_path, "c2", now=NOW)
    assert q2 is not None and "beta" in q2
    # Both stamped now: a third session stays silent.
    assert session_open_callback(tmp_path, "c3", now=NOW) is None
    log = (tmp_path / "logs" / "lisan.log").read_text(encoding="utf-8")
    assert log.count("drive.callback.delivered") == 2
    assert "drive.callback.suppressed" in log and "reason=cooldown" in log


def test_cooldown_expires(tmp_path):
    _loop(tmp_path, "gamma", last_callback="2026-07-01")
    q = session_open_callback(tmp_path, "c1", now=NOW)  # 9 days later > 7
    assert q is not None and "gamma" in q


def test_exhausted_loops_retire_for_good(tmp_path):
    """Asked twice without resolution → never asked again, whatever the
    cooldown says (capstone cycle 1: three consecutive sessions of the same
    question is nagging)."""
    _loop(tmp_path, "epsilon", last_callback="2026-06-01", callback_count=2)
    assert session_open_callback(tmp_path, "c1", now=NOW) is None
    log = (tmp_path / "logs" / "lisan.log").read_text(encoding="utf-8")
    assert "reason=exhausted" in log


def test_callback_count_increments_on_delivery(tmp_path):
    path = _loop(tmp_path, "zeta", links=["self_episode.job-1"])
    session_open_callback(tmp_path, "c1", now=NOW)
    assert load_markdown(path).frontmatter["callback_count"] == 1


def test_below_threshold_stays_silent(tmp_path):
    _loop(tmp_path, "meh", significance="low", created=str(NOW), updated=str(NOW))
    assert session_open_callback(tmp_path, "c1", now=NOW, config={"drive": {"min_score": 2.0}}) is None


def test_disabled_drive_is_inert(tmp_path):
    _loop(tmp_path, "anything", links=["self_episode.job-1"])
    assert session_open_callback(tmp_path, "c1", now=NOW, config={"drive": {"enabled": False}}) is None


def test_injection_only_on_session_open(tmp_path, monkeypatch):
    """run_conversation_turn asks the drive only on the conversation's very
    first turn (the user's turn is already in the transcript by then), and
    passes the question through as unresolved_thread."""
    import lisan.tools.conversation as conv

    _loop(tmp_path, "delta", links=["self_episode.job-1"])
    captured: dict = {}

    class FakeAgent:
        last_tool_calls: list = []

        def __init__(self, vault=None):
            pass

        def run_json(self, user_input, **kwargs):
            captured.update(kwargs)
            return {"response": "ok"}

    monkeypatch.setattr("lisan.agents.conversation.ConversationAgent", FakeAgent)
    monkeypatch.setattr(conv, "_retrieval_context", lambda **k: "")
    monkeypatch.setattr(conv, "_queue_observation", lambda **k: None)
    monkeypatch.setattr(conv, "cached_capability_index", lambda: "caps")

    conv.run_conversation_turn(vault=tmp_path, text="hi", conversation_id="fresh")
    assert captured.get("unresolved_thread") and "delta" in captured["unresolved_thread"]

    captured.clear()
    conv.run_conversation_turn(vault=tmp_path, text="again", conversation_id="fresh")
    assert captured.get("unresolved_thread") is None  # second turn: session not open
