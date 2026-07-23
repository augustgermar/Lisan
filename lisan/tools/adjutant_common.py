"""Shared Adjutant vocabulary: task kinds, statuses, schedule cadence.

One home for the enums the schema layer, validator, poller, gate, and
executor all agree on — the same single-source discipline as the
hypothesis gate terms. Deterministic; no LLM anywhere near this.
"""
from __future__ import annotations

import re

# v1 task kinds (spec §2.4 — deliberately small).
TASK_KINDS = {"run_script", "research", "collect", "draft", "notify"}

# Lifecycle of a task carried on an open_loop or decision step.
TASK_STATUSES = {"pending", "running", "blocked", "expired", "resolved"}

# Terminal states of a confirmation record.
CONFIRMATION_RESOLUTIONS = {"approved", "denied", "expired"}

# Schedule cadence. The first two forms are the existing scheduler's
# vocabulary (scheduler.normalize_recurrence); the weekly/monthly forms
# are the Adjutant's additions, taught to next_occurrence when schedule
# records materialize into jobs (step 6 of WO-ADJUTANT).
_CRON_RES = [
    re.compile(r"^every:\d+[smhdw]$"),
    re.compile(r"^daily@([01]?\d|2[0-3]):[0-5]\d$"),
    re.compile(r"^weekly:(mon|tue|wed|thu|fri|sat|sun)@([01]?\d|2[0-3]):[0-5]\d$"),
    re.compile(r"^monthly:([1-9]|1\d|2[0-8])@([01]?\d|2[0-3]):[0-5]\d$"),
]


def valid_cron(value: str | None) -> bool:
    if not value:
        return False
    text = str(value).strip().lower()
    return any(pattern.match(text) for pattern in _CRON_RES)


_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WEEKLY_RE = re.compile(r"^weekly:(mon|tue|wed|thu|fri|sat|sun)@(\d{1,2}):(\d{2})$")
_MONTHLY_RE = re.compile(r"^monthly:(\d{1,2})@(\d{1,2}):(\d{2})$")


def next_cron_occurrence(cron: str, *, after=None):
    """First fire time strictly after ``after`` (default now), as an aware
    UTC datetime. Handles all four cadence forms; every:/daily@ delegate to
    the existing scheduler (one home per form). The cadence is always
    computed from the record's cron string — it never migrates into the DB."""
    from datetime import timedelta

    from .scheduler import _local_tz, _now_utc, next_occurrence

    text = str(cron).strip().lower()
    if text.startswith("every:") or text.startswith("daily@"):
        return next_occurrence(text, after=after)
    after = after or _now_utc()
    local_after = after.astimezone(_local_tz())

    match = _WEEKLY_RE.match(text)
    if match:
        target_weekday = _WEEKDAYS.index(match.group(1))
        hour, minute = int(match.group(2)), int(match.group(3))
        candidate = local_after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        candidate += timedelta(days=(target_weekday - local_after.weekday()) % 7)
        if candidate <= local_after:
            candidate += timedelta(days=7)
        return candidate.astimezone(_utc())

    match = _MONTHLY_RE.match(text)
    if match:
        day, hour, minute = int(match.group(1)), int(match.group(2)), int(match.group(3))
        candidate = local_after.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_after:
            month = local_after.month + 1
            year = local_after.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            candidate = candidate.replace(year=year, month=month)
        return candidate.astimezone(_utc())

    raise ValueError(f"unsupported cadence {cron!r}")


def next_cron_stamp(cron: str, *, after=None) -> str:
    """next_cron_occurrence rendered as a LOCAL naive ISO stamp — the form
    schedule records store in next_run and the poller compares against
    local now. One renderer so the comparison convention lives here."""
    from .scheduler import _local_tz

    return next_cron_occurrence(cron, after=after).astimezone(_local_tz()).strftime("%Y-%m-%dT%H:%M:%S")


def _utc():
    from datetime import timezone

    return timezone.utc
