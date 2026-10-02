from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from lisan.tools import learning_notice as N


def _applied(skill="gmail_search", op="patch"):
    return SimpleNamespace(applied=SimpleNamespace(skill=skill, op=op), op=SimpleNamespace(rationale="needs a non-empty query"))


def test_skill_changes_and_lifecycle_become_one_short_message(tmp_path):
    with patch("lisan.tools.escalation._notify_owner", return_value=True) as notify:
        assert N.skills_learned(tmp_path, "r1", [_applied(), SimpleNamespace(applied=None, op=None)],
                                [{"skill": "log-rotation", "to": "established", "reason": "3 clean uses"}], config={})
    (text,), kwargs = notify.call_args
    assert text.count("\n") == 2 and "updated skill gmail_search: needs a non-empty query" in text
    assert "now trusts its skill log-rotation" in text and kwargs["vault"] == tmp_path


def test_nothing_learned_sends_nothing(tmp_path):
    with patch("lisan.tools.escalation._notify_owner") as notify:
        assert not N.skills_learned(tmp_path, "r1", [SimpleNamespace(applied=None, op=None)], [], config={})
    notify.assert_not_called()


def test_it_can_be_turned_off(tmp_path):
    with patch("lisan.tools.escalation._notify_owner") as notify:
        assert not N.aches(tmp_path, [("thin", "x")], config={"learning": {"notify": False}})
    notify.assert_not_called()


def test_long_lines_and_long_lists_are_cut(tmp_path):
    found = [("near_dup", "x" * 500)] + [("thin", f"person {i}") for i in range(6)]
    with patch("lisan.tools.escalation._notify_owner", return_value=True) as notify:
        N.aches(tmp_path, found, config={})
    text = notify.call_args.args[0]
    assert len(text.splitlines()) == 1 + N.MAX_LINES + 1 and text.endswith("…and 3 more")
    assert all(len(line) <= N.MAX_LINE + 2 for line in text.splitlines()[1:N.MAX_LINES + 1])


def test_a_failed_send_never_raises(tmp_path):
    with patch("lisan.tools.escalation._notify_owner", side_effect=RuntimeError("telegram down")):
        assert N.beliefs_revised(tmp_path, ["I am reliable."], config={}) is False
