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

    The root's process group is stopped first so it cannot start another
    command while the tree is being read (killing a parent reparents its
    children to init, which would lose the link), then everything found is
    killed, then the group is killed too in case it held anything the walk missed.
    """
    parents = _parent_map()
    if _protected(pid, parents) or pid not in parents:
        return False
    try:
        os.killpg(pid, signal.SIGSTOP)  # freeze: no new children while we look
    except (ProcessLookupError, PermissionError, OSError):
        pass
    victims = [v for v in descendants(pid, parents) if not _protected(v, parents)]
    signalled = False
    for victim in reversed(victims):  # deepest first
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
