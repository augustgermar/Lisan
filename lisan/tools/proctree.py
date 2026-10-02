"""Kill a process and everything it started.

`codex exec` runs each shell command it is asked to in its own process group
(measured: a `sleep` it launched had pgid == its own pid, not codex's), so
signalling codex's process group — what a timeout or a cancel used to do —
leaves the real work running: a build, an ssh session, a database dump. The
only link that survives is parentage, so the tree is found by walking parent
pids, never by matching names (an unrelated codex daemon may be running on the
same machine and must not be touched).
"""
from __future__ import annotations

import os
import signal
import subprocess


def _parent_map() -> dict[int, int]:
    """pid -> ppid for every process, from one `ps` call."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    parents: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            parents[int(parts[0])] = int(parts[1])
    return parents


def descendants(pid: int, parents: dict[int, int] | None = None) -> list[int]:
    """Every process below `pid`, children before grandchildren."""
    parents = _parent_map() if parents is None else parents
    children: dict[int, list[int]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    found: list[int] = []
    queue = [pid]
    while queue:
        for child in children.get(queue.pop(0), []):
            if child not in found and child != pid:
                found.append(child)
                queue.append(child)
    return found


def _protected(pid: int, parents: dict[int, int]) -> bool:
    """Never signal init, ourselves, or anything we are running under."""
    if pid <= 1:
        return True
    ancestor, hops = os.getpid(), 0
    while ancestor > 1 and hops < 64:
        if ancestor == pid:
            return True
        ancestor = parents.get(ancestor, 0)
        hops += 1
    return False


def kill_tree(pid: int) -> bool:
    """SIGKILL `pid` and all its descendants. True if anything was signalled.

    Killing a parent reparents its children to init, which loses the only link
    back to them, and a process that is still running can fork while the tree is
    being read. So: freeze first, read second, and keep freezing. The root's
    process group is stopped, then the tree is read; every descendant found is
    stopped too (a command codex ran sits in its OWN group, so the root's freeze
    does not reach it) and the tree is read again, until a pass finds nothing
    new. Only then is everything killed, deepest first, and the group last.

    The first version read the tree before freezing it: a command that forked in
    that gap escaped the kill, and a cancelled worker then hung on its still-open
    output pipe. It never showed on a quiet machine.
    """
    parents = _parent_map()
    if _protected(pid, parents) or pid not in parents:
        return False
    try:
        os.killpg(pid, signal.SIGSTOP)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    frozen: list[int] = []
    for _ in range(_MAX_PASSES):
        parents = _parent_map()
        fresh = [v for v in descendants(pid, parents) if v not in frozen and not _protected(v, parents)]
        if not fresh:
            break
        for victim in fresh:
            try:
                os.kill(victim, signal.SIGSTOP)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            frozen.append(victim)
    signalled = False
    for victim in reversed(frozen):  # deepest first
        try:
            os.kill(victim, signal.SIGKILL)
            signalled = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for sig_target in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
        try:
            sig_target()
            signalled = True
            break
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return signalled


_MAX_PASSES = 8
