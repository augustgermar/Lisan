"""kill_tree: stop codex and everything it ran, and nothing else.

`codex exec` runs each command in its own process group, so group signals miss
the real work. These tests build that exact shape (a command in a NEW SESSION
below the child) and check it dies, while an unrelated process, an unrelated
"codex" lookalike, and our own ancestors never do.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from lisan.tools.proctree import descendants, kill_tree

PY = sys.executable


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # a zombie is dead for our purposes
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _gone(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _spawn_codexlike(tmp_path, tag: str):
    """sh (like codex) -> python (like its shell tool) -> sleep in a NEW session."""
    pidfile = tmp_path / f"{tag}.pid"
    script = tmp_path / f"{tag}.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"{PY} -c \"import subprocess,sys; p=subprocess.Popen(['sleep','340'], start_new_session=True); "
        f"open(r'{pidfile}','w').write(str(p.pid)); p.wait()\"\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    proc = subprocess.Popen([str(script)], start_new_session=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not pidfile.exists():
        time.sleep(0.05)
    time.sleep(0.1)
    return proc, int(pidfile.read_text())


def test_the_real_work_sits_in_a_different_process_group_than_its_parent(tmp_path):
    """The shape that made killpg insufficient — pinned so the premise is tested."""
    proc, worker = _spawn_codexlike(tmp_path, "shape")
    try:
        assert os.getpgid(worker) != os.getpgid(proc.pid)
        assert worker in descendants(proc.pid)  # but the parent link still finds it
    finally:
        kill_tree(proc.pid)
        proc.wait(timeout=5)


def test_kill_tree_reaches_a_command_in_its_own_group_and_session(tmp_path):
    proc, worker = _spawn_codexlike(tmp_path, "victim")
    assert _alive(worker)
    assert kill_tree(proc.pid) is True
    proc.wait(timeout=5)
    assert _gone(worker), "the command codex ran survived the kill"


def test_old_group_only_kill_would_have_missed_it(tmp_path):
    """The defect, demonstrated: a process-group kill leaves the worker running."""
    import signal

    proc, worker = _spawn_codexlike(tmp_path, "oldway")
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        time.sleep(0.3)
        assert _alive(worker)
    finally:
        os.kill(worker, signal.SIGKILL)


def test_kill_tree_leaves_unrelated_processes_alone(tmp_path):
    victim, victim_worker = _spawn_codexlike(tmp_path, "mine")
    bystander, bystander_worker = _spawn_codexlike(tmp_path, "theirs")  # same shape, not ours
    try:
        kill_tree(victim.pid)
        victim.wait(timeout=5)
        assert _gone(victim_worker)
        assert _alive(bystander.pid) and _alive(bystander_worker)
    finally:
        kill_tree(bystander.pid)
        bystander.wait(timeout=5)


def test_kill_tree_refuses_init_ourselves_and_our_ancestors():
    assert kill_tree(1) is False
    assert kill_tree(0) is False
    assert kill_tree(os.getpid()) is False
    assert kill_tree(os.getppid()) is False  # whatever launched this test run lives on
    assert _alive(os.getpid())


def test_kill_tree_of_a_missing_process_is_a_quiet_false():
    assert kill_tree(2**22 - 3) is False


# ── a tree that is still growing while it is being killed ───────────────────

def _spawn_forker(tmp_path):
    """A parent that keeps starting new commands, each in its OWN session (as codex
    does), faster than a process listing takes to read. The payload is a real file:
    an earlier version smuggled newlines through a shell string, Python rejected it,
    nothing ever forked, and the test killed an empty tree and passed."""
    payload = tmp_path / "forker.py"
    payload.write_text(
        "import subprocess, time\n"
        "while True:\n"
        "    subprocess.Popen(['sleep', '334'], start_new_session=True)\n"
        "    time.sleep(0.01)\n",
        encoding="utf-8",
    )
    script = tmp_path / "forker.sh"
    script.write_text(f"#!/bin/sh\n{PY} {payload}\n", encoding="utf-8")
    script.chmod(0o755)
    proc = subprocess.Popen([str(script)], start_new_session=True)
    time.sleep(0.5)  # let it build up children
    return proc


def _sleepers():
    out = subprocess.run(["pgrep", "-f", "sleep 334"], capture_output=True, text=True).stdout
    return {int(p) for p in out.split()}


def test_a_tree_that_keeps_forking_is_killed_completely_every_time(tmp_path):
    """The window between reading the tree and freezing it let a fresh fork escape,
    which hung a cancelled worker on its open pipe. Repeated, because one lucky
    pass proves nothing about a race."""
    leaked = set()
    try:
        for _ in range(12):
            before = _sleepers()
            proc = _spawn_forker(tmp_path)
            assert len(_sleepers() - before) >= 5, "the forker never forked: this test would be killing an empty tree"
            assert kill_tree(proc.pid) is True
            proc.wait(timeout=5)
            time.sleep(0.15)
            leaked |= _sleepers() - before
        assert leaked == set(), f"{len(leaked)} command(s) survived the kill"
    finally:
        for pid in _sleepers():
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def _spawn_outside_forker(tmp_path):
    """root -> a spawner in its OWN session (like a command codex ran) that keeps
    starting more commands. Freezing the root's process group does not reach it, so
    only freezing each descendant as it is found can stop it outrunning the kill."""
    (tmp_path / "spawner.py").write_text(
        "import subprocess, time\n"
        "while True:\n"
        "    subprocess.Popen(['sleep', '334'], start_new_session=True)\n"
        "    time.sleep(0.005)\n", encoding="utf-8")
    (tmp_path / "root.py").write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, r'{tmp_path / 'spawner.py'}'], start_new_session=True).wait()\n", encoding="utf-8")
    script = tmp_path / "root.sh"
    script.write_text(f"#!/bin/sh\n{PY} {tmp_path / 'root.py'}\n", encoding="utf-8")
    script.chmod(0o755)
    proc = subprocess.Popen([str(script)], start_new_session=True)
    time.sleep(0.6)
    return proc


def test_a_spawner_outside_the_roots_process_group_cannot_outrun_the_kill(tmp_path):
    leaked = set()
    try:
        for _ in range(10):
            before = _sleepers()
            proc = _spawn_outside_forker(tmp_path)
            assert len(_sleepers() - before) >= 5, "the spawner never spawned: this test would be killing an empty tree"
            assert kill_tree(proc.pid) is True
            proc.wait(timeout=5)
            time.sleep(0.2)
            leaked |= _sleepers() - before
        assert leaked == set(), f"{len(leaked)} command(s) outran the kill"
    finally:
        for pid in _sleepers():
            try:
                os.kill(pid, 9)
            except OSError:
                pass
