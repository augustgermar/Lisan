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
