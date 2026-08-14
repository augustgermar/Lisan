"""The Adjutant daemon: cycles on an interval, one per vault.

The lockfile lives in the vault root and carries the daemon's pid. Two
daemons on one vault is an error, not a race: the second refuses to
start while the first's pid is alive, and a lockfile whose pid is dead
is stale and reclaimed. Halts inside the loop stay loud through the
runner's edge-triggered owner ping; the daemon itself never swallows a
cycle failure silently.
"""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

from ..config import load_config
from ..paths import sqlite_path, vault_root

LOCKFILE_NAME = ".adjutant.lock"


class DaemonLockError(RuntimeError):
    pass


def lockfile_path(vault: Path) -> Path:
    return vault / LOCKFILE_NAME


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(vault: Path, *, pid: int | None = None) -> Path:
    """Take the vault's daemon lock or raise DaemonLockError naming the
    living owner. A dead owner's lock is stale and reclaimed."""
    pid = pid or os.getpid()
    path = lockfile_path(vault)
    if path.exists():
        try:
            existing = int(path.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            existing = 0
        if existing and existing != pid and _pid_alive(existing):
            raise DaemonLockError(
                f"another adjutant daemon (pid {existing}) holds {path}; "
                "two daemons on one vault is an error"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")
    return path


def release_lock(vault: Path, *, pid: int | None = None) -> None:
    pid = pid or os.getpid()
    path = lockfile_path(vault)
    try:
        if path.exists() and int(path.read_text(encoding="utf-8").strip() or 0) == pid:
            path.unlink()
    except (ValueError, OSError):
        pass


def executor_readiness(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Can the executor actually run anything from inside this process?

    The Adjutant is the one service installed from a hand-written plist in
    ``docs/adjutant_daemon.md`` rather than through ``service_install``, and
    that documented plist sets ``LISAN_VAULT`` and nothing else. launchd's
    default PATH is ``/usr/bin:/bin:/usr/sbin:/sbin``, so ``codex`` — which
    lives in ``/usr/local/bin`` on a normal install — is invisible to it. Every
    copy of that plist has the same hole.

    It has cost nothing so far only because the daemon ships dry: cycles log
    verdicts and execute nothing. The day ``adjutant.enabled`` becomes true,
    every task needing the executor would fail inside a service whose PATH
    nobody thinks about. That is the shape of failure this project keeps
    finding, so the daemon says it out loud at startup instead of waiting.
    """
    import shutil

    config = config or load_config()
    routing = (config.get("routing") or {}).get("executor") or {}
    provider = str(routing.get("medium") or "codex")
    if provider != "codex":
        return {"provider": provider, "reachable": True, "checked": False}
    binary_env = ((config.get("providers") or {}).get("codex") or {}).get("binary_env", "CODEX_BIN")
    binary = os.environ.get(binary_env) or "codex"
    resolved = shutil.which(binary)
    return {
        "provider": provider,
        "binary": binary,
        "path": resolved,
        "reachable": bool(resolved),
        "checked": True,
        "search_path": os.environ.get("PATH", ""),
    }


def run_daemon(
    vault: Path | None = None,
    db_path: Path | None = None,
    *,
    config: dict[str, Any] | None = None,
    max_cycles: int | None = None,
) -> int:
    """The loop: fswatch scan, then a cycle, then sleep the configured
    interval. ``max_cycles`` bounds the loop for tests; production runs
    until signalled."""
    from .adjutant_runner import run_cycle
    from .fswatch import fswatch_scan

    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    config = config or load_config()
    interval = max(1, int((config.get("adjutant") or {}).get("interval_minutes", 15) or 15)) * 60

    acquire_lock(vault)

    # Startup notice, flushed: launchd block-buffers stdout, and a diagnostic
    # nobody can read on time is not a diagnostic (2026-07-27).
    adjutant_cfg = config.get("adjutant") or {}
    posture = ("enabled" if adjutant_cfg.get("enabled") else
               "calibration (dry)" if adjutant_cfg.get("calibration") else "dry")
    ready = executor_readiness(config)
    print(f"⚕ Adjutant daemon — vault {vault}, every {interval // 60}m, {posture}")
    if ready["checked"] and not ready["reachable"]:
        print(f"  ! executor unreachable: {ready['binary']!r} is not on this service's PATH")
        print(f"    PATH={ready['search_path']}")
        print("    Cycles will report but can never execute. Add PATH to the service's")
        print("    EnvironmentVariables (see docs/adjutant_daemon.md) and reload it.")
    elif ready["checked"]:
        print(f"  ✓ executor {ready['provider']} at {ready['path']}")
    try:
        import sys as _sys

        _sys.stdout.flush()
    except Exception:
        pass

    stop = {"flag": False}

    def _stop(signum, frame):  # noqa: ARG001
        stop["flag"] = True

    previous_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous_handlers[sig] = signal.signal(sig, _stop)
        except ValueError:
            pass  # not the main thread (tests)

    cycles = 0
    try:
        while not stop["flag"]:
            try:
                fswatch_scan(vault, db_path=db_path, config=config)
            except Exception:
                pass  # fswatch is an adapter; its failure never stops cycles
            result = run_cycle(vault, db_path, config=config)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            if result.get("halted"):
                # Stay resident: the owner fixing intent.md should not need
                # to restart the daemon. The halt ping already fired once.
                pass
            slept = 0.0
            while not stop["flag"] and slept < interval:
                time.sleep(min(1.0, interval - slept))
                slept += 1.0
        return 0
    finally:
        release_lock(vault)
        for sig, handler in previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except ValueError:
                pass
