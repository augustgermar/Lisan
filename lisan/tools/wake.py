"""Hold the machine awake while the agent is actually working.

The owner's Mac idle-sleeps aggressively and services messages during
~45-second darkwakes every ~15 minutes (2026-07-06 incident: a turn that
outgrew its darkwake froze mid-reply for 15 minutes and read as a crashed
agent). macOS's own background work — Time Machine in Power Nap — solves
this by taking a power assertion for the duration of the task; this module
does the same with `caffeinate`, scoped to a turn or job and capped, so the
machine stays awake exactly as long as the agent is working and not a
second longer. This is deliberately NOT a keep-awake daemon: whether the
machine may sleep at all is the owner's power policy, not the agent's.

`caffeinate -s` holds system sleep off while on AC power; on battery macOS
may still force sleep — an accepted limit (the agent should not be the
reason a laptop dies in a bag).
"""
from __future__ import annotations

import contextlib
import subprocess
from typing import Iterator

DEFAULT_CAP_SECONDS = 900


@contextlib.contextmanager
def hold_awake(reason: str, cap_seconds: int = DEFAULT_CAP_SECONDS) -> Iterator[None]:
    """Best-effort: never let power management become a reason work fails."""
    import platform

    proc = None
    if platform.system() == "Darwin":
        try:
            proc = subprocess.Popen(
                ["/usr/bin/caffeinate", "-s", "-i", "-t", str(int(cap_seconds))],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            proc = None
    try:
        yield
    finally:
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
