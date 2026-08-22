"""The two-lane browser: quiet work, loud handoff.

The owner could not use their own computer while the agent browsed —
every search raised the Chrome window and took the keyboard mid-sentence
(2026-08-21). The fix is architectural rather than a politeness tweak:
autonomous work moves to a browser with no window at all, and the visible
one appears only when the owner has to act.
"""
from __future__ import annotations

from lisan.tools.browser import (
    CDP_PORT,
    LANE_LOUD,
    LANE_QUIET,
    QUIET_CDP_PORT,
    QUIET_USER_AGENT,
    _copy_cookies,
    browser_handoff,
    lane_port,
    looks_like_login_wall,
    quiet_chrome_args,
)


def test_lanes_are_separate_browsers():
    assert lane_port(LANE_QUIET) == QUIET_CDP_PORT
    assert lane_port(LANE_LOUD) == CDP_PORT
    # An unrecognised lane must be visible, never silent.
    assert lane_port("") == CDP_PORT
    assert lane_port("nonsense") == CDP_PORT


def test_quiet_lane_is_headless_and_does_not_announce_itself():
    args = " ".join(quiet_chrome_args())
    assert "--headless=new" in args
    assert f"--remote-debugging-port={QUIET_CDP_PORT}" in args
    assert "browser-quiet-profile" in args
    # Chrome's default headless UA says "HeadlessChrome", which Google
    # answers with a CAPTCHA; the override is what makes the lane usable.
    assert "HeadlessChrome" not in QUIET_USER_AGENT
    assert f"--user-agent={QUIET_USER_AGENT}" in args


def test_login_wall_detection_is_conservative():
    """A false positive puts a window on the owner's screen for nothing."""
    assert looks_like_login_wall("https://www.google.com/sorry/index?continue=x")
    assert looks_like_login_wall("", "Sign in - Google Accounts")
    assert looks_like_login_wall("", "Just a moment...")
    assert looks_like_login_wall("", "", "Our systems have detected unusual traffic from your computer network")
    assert not looks_like_login_wall("https://www.youtube.com/@psychacks", "Orion Taraban - YouTube")
    # An article that merely discusses logging in is not a wall.
    assert not looks_like_login_wall(
        "https://example.com/guide",
        "How to log in to your bank account and manage transfers safely in 2026",
    )
    assert not looks_like_login_wall("", "", "You can sign in later to save your preferences" * 3)


class _FakeContext:
    def __init__(self, cookies=()):
        self._cookies = list(cookies)

    def cookies(self):
        return list(self._cookies)

    def add_cookies(self, payload):
        self._cookies.extend(payload)


def test_session_bridge_carries_the_owners_login_between_lanes():
    loud = _FakeContext([
        {"name": "SID", "value": "abc", "domain": ".google.com", "path": "/", "secure": True,
         "unexpected_field": "dropped"},
    ])
    quiet = _FakeContext([{"name": "old", "value": "1", "domain": ".example.com", "path": "/"}])
    result = _copy_cookies(loud, quiet, source=LANE_LOUD, target=LANE_QUIET)
    assert result == {"ok": True, "source": "loud", "target": "quiet", "copied": 1, "before": 1, "after": 2}
    carried = quiet.cookies()[-1]
    assert carried["name"] == "SID"
    # Only fields the CDP cookie API accepts survive the crossing.
    assert "unexpected_field" not in carried


def test_handoff_refuses_without_a_destination():
    assert browser_handoff("", "reason")["ok"] is False
    assert "url" in browser_handoff("", "reason")["error"]


def test_handoff_does_not_block_the_conversation_by_default():
    """The telegram bot handles one update at a time.

    A handoff that waited for the owner would make the agent deaf to the
    owner for the whole wait — unable to answer the question its own
    message invited.
    """
    import inspect

    from lisan.tools.browser import browser_handoff_finish

    signature = inspect.signature(browser_handoff)
    assert signature.parameters["wait_seconds"].default == 0.0
    assert callable(browser_handoff_finish)


def test_browser_tool_exposes_the_full_handoff_cycle():
    from lisan.tools.execution_tools import TOOLS

    tool = next(item for item in TOOLS if item["name"] == "browser")
    actions = tool["parameters"]["properties"]["action"]["enum"]
    for verb in ("search", "handoff", "handoff_finish", "sync_session"):
        assert verb in actions
    described = tool["description"]
    # The description is what actually teaches the flow; the model never
    # reads browser.py.
    assert "quiet" in described.lower() and "handoff_finish" in described
    assert "RETURNS IMMEDIATELY" in described


def test_irreversible_clicks_are_refused_until_the_owner_says_yes():
    """The agent is told to ask before committing the owner to something.

    Told is not enforced: the approval gate was removed on 2026-07-26, and
    a browser click can enrol the owner in billing or sign an agreement.
    This makes the asking structural, as gmail_send already is.
    """
    from lisan.tools.execution_tools import _irreversible_click_refusal as refusal

    for label in ("I Agree", "Accept terms", "Create project", "Upgrade to Blaze",
                  "Enable billing", "Sign agreement", "Delete", "Confirm purchase"):
        assert refusal("click", {"target": label}) is not None, label

    # Navigation and ordinary controls stay frictionless, and an
    # informational link that merely contains a scary word is not a
    # commitment.
    for label in ("Next", "Firestore Database", "Learn more about creating projects", "Cancel"):
        assert refusal("click", {"target": label}) is None, label

    # Only clicking is gated; reading a page never is.
    assert refusal("goto", {"target": "Accept"}) is None
    assert refusal("read", {}) is None

    # The owner's explicit yes is the key, and it is per-click.
    assert refusal("click", {"target": "Create project", "owner_approved": True}) is None


def test_tool_iteration_ceiling_is_configurable():
    """Ten tool calls suits a chat turn and starves a runbook."""
    from lisan.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["conversation"]["max_tool_iterations"] == 10
