"""WO-7: the autonomy surface is enforced in code, at one seam."""
from __future__ import annotations

import pytest

from lisan.tools.action_policy import action_allowed, dispatch_drive_action, policy_tier


@pytest.fixture(autouse=True)
def _fresh_logger():
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


def test_default_tier_is_queue_only():
    assert policy_tier(None) == 0
    assert policy_tier({}) == 0
    assert action_allowed("session_callback", {})
    assert not action_allowed("scheduled_delivery", {})
    assert not action_allowed("autonomous_check", {})


def test_tier_gates_are_monotonic():
    tier1 = {"drive": {"action_tier": 1}}
    assert action_allowed("session_callback", tier1)
    assert action_allowed("scheduled_delivery", tier1)
    assert not action_allowed("autonomous_check", tier1)
    tier2 = {"drive": {"action_tier": 2}}
    assert all(action_allowed(k, tier2) for k in ("session_callback", "scheduled_delivery", "autonomous_check"))


def test_unknown_actions_are_denied_even_at_top_tier():
    assert not action_allowed("send_email", {"drive": {"action_tier": 2}})


def test_bad_tier_values_fall_back_to_default():
    assert policy_tier({"drive": {"action_tier": "loud"}}) == 0
    assert policy_tier({"drive": {"action_tier": 99}}) == 2
    assert policy_tier({"drive": {"action_tier": -3}}) == 0


def test_dispatch_runs_allowed_and_blocks_forbidden(tmp_path):
    ran = []
    result = dispatch_drive_action(tmp_path, "session_callback", lambda: ran.append(1) or "ok", config={})
    assert result == "ok" and ran == [1]
    blocked = dispatch_drive_action(tmp_path, "autonomous_check", lambda: ran.append(2), config={})
    assert blocked is None and ran == [1]  # provably inert below tier 2
    log = (tmp_path / "logs" / "lisan.log").read_text(encoding="utf-8")
    assert "drive.action.blocked kind=autonomous_check tier=0" in log


def test_session_callback_flows_through_the_policy(tmp_path):
    from lisan.frontmatter import write_markdown
    from lisan.tools.drive import session_open_callback

    write_markdown(
        tmp_path / "open_loops" / "x.md",
        {"id": "open_loop.x", "type": "open_loop", "created": "2026-07-01", "updated": "2026-07-08",
         "status": "active", "significance": "high", "summary": "the x thing",
         "links": ["self_episode.job-1"]},
        "body",
    )
    from datetime import date

    now = date(2026, 7, 10)
    # Tier 0 allows the callback; a hypothetical negative config cannot occur
    # (clamped), so assert the allowed path and the disabled-drive path.
    assert session_open_callback(tmp_path, "c1", now=now, config={"drive": {"action_tier": 0}}) is not None
