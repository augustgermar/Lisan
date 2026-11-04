"""Local-time schedule math must follow daylight saving, not today's offset.

The system zone used to be captured as a fixed UTC offset
(``datetime.now().astimezone().tzinfo``). A weekly 08:00 computed in winter
for a summer date then landed at 09:00 local, and vice versa — every
cadence that crossed a DST boundary fired an hour off. These tests pin the
process to a DST-observing zone so they fail on any machine, in any season.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest

from lisan.tools import scheduler
from lisan.tools.adjutant_common import next_cron_occurrence, next_cron_stamp

PACIFIC = "America/Los_Angeles"


@pytest.fixture
def pacific(monkeypatch):
    monkeypatch.setenv("TZ", PACIFIC)
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_local_tz_follows_dst_rather_than_todays_offset(pacific):
    tz = scheduler._local_tz()
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=tz)
    winter = datetime(2026, 12, 15, 12, 0, tzinfo=tz)
    assert summer.utcoffset().total_seconds() == -7 * 3600
    assert winter.utcoffset().total_seconds() == -8 * 3600


def test_weekly_cadence_keeps_wall_clock_across_fall_back(pacific):
    # Thursday 2026-10-29 (PDT); the next Monday is 2026-11-02, after the
    # 2026-11-01 fall-back, so it is PST.
    nxt = next_cron_occurrence("weekly:mon@08:00", after=_utc(2026, 10, 29, 19, 0))
    assert nxt == _utc(2026, 11, 2, 16, 0)  # 08:00 PST
    assert next_cron_stamp("weekly:mon@08:00", after=_utc(2026, 10, 29, 19, 0)) == "2026-11-02T08:00:00"


def test_weekly_cadence_keeps_wall_clock_across_spring_forward(pacific):
    nxt = next_cron_occurrence("weekly:mon@08:00", after=_utc(2026, 3, 5, 20, 0))
    assert nxt == _utc(2026, 3, 9, 15, 0)  # 08:00 PDT, after 2026-03-08


def test_monthly_cadence_keeps_wall_clock_across_dst(pacific):
    nxt = next_cron_occurrence("monthly:15@09:30", after=_utc(2026, 10, 20, 19, 0))
    assert nxt == _utc(2026, 11, 15, 17, 30)  # 09:30 PST


def test_daily_recurrence_keeps_wall_clock_across_dst(pacific):
    nxt = scheduler.next_occurrence("daily@07:00", after=_utc(2026, 11, 1, 6, 0))
    # 2026-10-31 23:00 PDT -> next 07:00 local is 2026-11-01 07:00 PST.
    assert nxt == _utc(2026, 11, 1, 15, 0)


def test_annual_recurrence_uses_the_dates_own_offset(pacific):
    nxt = scheduler.next_occurrence("annual@07-04@09:00", after=_utc(2026, 12, 1, 12, 0))
    assert nxt == _utc(2027, 7, 4, 16, 0)  # 09:00 PDT


def test_naive_when_is_read_with_that_dates_offset(pacific):
    fired = scheduler.parse_when("2027-07-04 09:00", now=_utc(2026, 12, 1, 12, 0))
    assert fired == _utc(2027, 7, 4, 16, 0)


# ---------------------------------------------------------------------------
# Zone detection from /etc/localtime (issue #5). The tests above pin $TZ, which
# skips this lookup, so CI (UTC, no DST) never exercised it.

@pytest.mark.parametrize(
    "target, expected",
    [
        ("/usr/share/zoneinfo/Europe/Berlin", "Europe/Berlin"),                      # Linux
        ("/usr/share/zoneinfo.default/America/Los_Angeles", "America/Los_Angeles"),  # macOS
        ("/var/db/timezone/tz/2026f.1.0/zoneinfo/Asia/Kolkata", "Asia/Kolkata"),    # macOS versioned tree
        ("/usr/share/zoneinfo/America/Argentina/Buenos_Aires", "America/Argentina/Buenos_Aires"),
    ],
)
def test_zone_name_is_read_from_any_zoneinfo_tree(target, expected):
    assert scheduler._zone_name_from_path(target) == expected


@pytest.mark.parametrize(
    "target",
    ["/etc/localtime", "/usr/share/zoneinfo/Not/AZone", "/opt/tzdata/America/Chicago"],
)
def test_unrecognised_localtime_target_yields_no_zone(target):
    assert scheduler._zone_name_from_path(target) is None


def test_macos_localtime_gives_a_dst_aware_zone(monkeypatch):
    # The exact failure from issue #5: TZ unset, /etc/localtime resolving into
    # zoneinfo.default. The zone must still follow DST.
    monkeypatch.delenv("TZ", raising=False)
    real_realpath = os.path.realpath
    monkeypatch.setattr(
        os.path,
        "realpath",
        lambda path, *a, **k: "/usr/share/zoneinfo.default/America/Los_Angeles"
        if path == "/etc/localtime"
        else real_realpath(path, *a, **k),
    )
    assert scheduler._local_zone_name() == PACIFIC
    tz = scheduler._local_tz()
    assert datetime(2026, 7, 15, 12, tzinfo=tz).utcoffset().total_seconds() == -7 * 3600
    assert datetime(2026, 12, 15, 12, tzinfo=tz).utcoffset().total_seconds() == -8 * 3600
