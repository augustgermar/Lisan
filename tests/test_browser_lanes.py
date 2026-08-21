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
