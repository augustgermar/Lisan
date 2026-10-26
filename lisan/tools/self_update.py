"""Auto-update: a periodic check for a newer commit on the configured
remote branch, with two owner-chosen postures.

``update.mode`` starts unresolved (``null``). The periodic check never
runs until the owner answers once — at ``lisan init`` or with
``lisan update config --mode ...`` — same deny-by-default posture as
``adjutant.enabled`` and the action tiers. The two postures:

- ``automatic``: fast-forward only, verified by the full test suite
  before the resident service is ever touched, and rolled back
  (``git reset --hard``) on any failure. Refuses outright on a dirty
  working tree, a local branch that has diverged from the remote, or a
  checkout that isn't on the configured branch — those are judgment
  calls for the owner, not for an unattended job.
- ``notify``: pages the owner once per newly seen remote commit and
  applies nothing; the owner runs ``lisan update apply`` by hand.

Dispatched as the recurring job type ``self_update.check`` (see
``jobs.py``); the job re-enqueues its own next occurrence via the jobs
table's ``recurrence`` field, so a network blip or a transient failure
never kills the series.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..paths import config_path, repo_root
from .db import connect as _connect

MODES = {"automatic", "notify", "off"}
DEFAULT_CHECK_INTERVAL_HOURS = 12
_GIT_TIMEOUT = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── State (ephemeral, observed — not owner-authored config) ─────────────────

def ensure_update_state_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS update_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            checked_at TEXT,
            local_commit TEXT,
            remote_commit TEXT,
            update_available INTEGER NOT NULL DEFAULT 0,
            notified_commit TEXT,
            applied_commit TEXT,
            last_error TEXT
        )
        """
    )
    conn.commit()


def read_update_state(db_path: Path | None = None) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        ensure_update_state_table(conn)
        row = conn.execute(
            "SELECT checked_at, local_commit, remote_commit, update_available, "
            "notified_commit, applied_commit, last_error FROM update_state WHERE id = 1"
        ).fetchone()
        if not row:
            return {}
        return {
            "checked_at": row[0],
            "local_commit": row[1],
            "remote_commit": row[2],
            "update_available": bool(row[3]),
            "notified_commit": row[4],
            "applied_commit": row[5],
            "last_error": row[6],
        }
    finally:
        conn.close()


def _write_update_state(db_path: Path | None, **fields: Any) -> None:
    if not fields:
        return
    conn = _connect(db_path)
    try:
        ensure_update_state_table(conn)
        exists = conn.execute("SELECT 1 FROM update_state WHERE id = 1").fetchone()
        if exists:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            conn.execute(f"UPDATE update_state SET {assignments} WHERE id = 1", list(fields.values()))
        else:
            columns = ", ".join(fields)
            placeholders = ", ".join("?" for _ in fields)
            conn.execute(
                f"INSERT INTO update_state (id, {columns}) VALUES (1, {placeholders})",
                list(fields.values()),
            )
        conn.commit()
    finally:
        conn.close()


# ── Config (owner-authored, persisted to config.json) ───────────────────────

def set_update_mode(mode: str, *, path: Path | None = None) -> Path:
    """Write ``update.mode`` into config.json, preserving everything else."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    from copy import deepcopy

    from ..config import DEFAULT_CONFIG

    path = path or config_path()
    cfg: dict[str, Any] = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception:
            cfg = {}
    if not cfg:
        cfg = deepcopy(DEFAULT_CONFIG)
    cfg.setdefault("update", {})
    cfg["update"]["mode"] = mode
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return path


# ── Git plumbing ─────────────────────────────────────────────────────────────

def _run_git(repo: Path, args: list[str], *, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _rev_parse(repo: Path, ref: str) -> str | None:
    result = _run_git(repo, ["rev-parse", ref])
    return result.stdout.strip() if result.returncode == 0 else None


def _current_branch(repo: Path) -> str:
    result = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else ""


def _dirty(repo: Path) -> bool:
    result = _run_git(repo, ["status", "--porcelain"])
    return bool(result.stdout.strip())


def check_remote(repo: Path | None = None, *, remote: str = "origin", branch: str = "main") -> dict[str, Any]:
    """Fetch the remote and compare it against local HEAD. Never mutates the
    working tree or any branch — applying a fast-forward is a separate,
    deliberate step in ``apply_update``."""
    repo = repo or repo_root()
    fetch = _run_git(repo, ["fetch", "--quiet", remote, branch])
    if fetch.returncode != 0:
        return {"ok": False, "error": (fetch.stderr or fetch.stdout).strip() or "git fetch failed"}

    local = _rev_parse(repo, "HEAD")
    remote_ref = _rev_parse(repo, f"{remote}/{branch}")
    if not local or not remote_ref:
        return {"ok": False, "error": "could not resolve local or remote HEAD"}

    if local == remote_ref:
        return {
            "ok": True, "local": local, "remote": remote_ref,
            "behind": 0, "ahead": 0, "update_available": False, "diverged": False,
        }

    behind = _run_git(repo, ["rev-list", "--count", f"{local}..{remote_ref}"])
    ahead = _run_git(repo, ["rev-list", "--count", f"{remote_ref}..{local}"])
    behind_n = int(behind.stdout.strip()) if behind.returncode == 0 and behind.stdout.strip().isdigit() else None
    ahead_n = int(ahead.stdout.strip()) if ahead.returncode == 0 and ahead.stdout.strip().isdigit() else None

    return {
        "ok": True,
        "local": local,
        "remote": remote_ref,
        "behind": behind_n,
        "ahead": ahead_n,
        "update_available": bool(behind_n),
        "diverged": bool(ahead_n),
    }


def _venv_python(repo: Path) -> Path:
    """Managed install layout: ``<install>/repo`` and ``<install>/venv`` are
    siblings. Fall back to the interpreter running this process (a source
    checkout, or a dev shell already inside the venv)."""
    candidate = repo.parent / "venv" / "bin" / "python3"
    if candidate.exists():
        return candidate
    import sys

    return Path(sys.executable)


def _pip_install_editable(repo: Path, python: Path, *, timeout: int = 600) -> dict[str, Any]:
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--editable", str(repo)],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return {"ok": result.returncode == 0, "stderr": (result.stderr or "").strip()}


def _run_tests(repo: Path, python: Path, *, timeout: int = 1800) -> dict[str, Any]:
    result = subprocess.run(
        [str(python), "-m", "pytest", "tests/", "-q"],
        cwd=str(repo), capture_output=True, text=True, timeout=timeout, check=False,
    )
    tail = "\n".join((result.stdout or "").splitlines()[-20:])
    return {"ok": result.returncode == 0, "tail": tail}


def _default_restart() -> dict[str, Any]:
    from .restart import restart_service

    return restart_service()


def apply_update(
    repo: Path | None = None,
    *,
    remote: str = "origin",
    branch: str = "main",
    restart: bool = True,
    run_tests: bool = True,
    test_runner: Callable[[Path, Path], dict[str, Any]] | None = None,
    dependency_installer: Callable[[Path, Path], dict[str, Any]] | None = None,
    restart_fn: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fast-forward only, verified, and reversible. Never attempts a rebase
    or merge over local history — a diverged branch is the owner's call."""
    repo = repo or repo_root()
    test_runner = test_runner or _run_tests
    dependency_installer = dependency_installer or _pip_install_editable
    restart_fn = restart_fn or _default_restart

    if _current_branch(repo) != branch:
        return {"applied": False, "reason": "not_on_target_branch"}
    if _dirty(repo):
        return {"applied": False, "reason": "dirty_working_tree"}

    status = check_remote(repo, remote=remote, branch=branch)
    if not status.get("ok"):
        return {"applied": False, "reason": "fetch_failed", "error": status.get("error")}
    if status.get("diverged"):
        return {"applied": False, "reason": "diverged", **status}
    if not status.get("update_available"):
        return {"applied": False, "reason": "up_to_date", **status}

    previous_commit = str(status["local"])
    target_commit = str(status["remote"])

    merge = _run_git(repo, ["merge", "--ff-only", f"{remote}/{branch}"])
    if merge.returncode != 0:
        return {"applied": False, "reason": "merge_failed", "error": (merge.stderr or merge.stdout).strip()}

    deps_diff = _run_git(
        repo, ["diff", "--name-only", previous_commit, target_commit, "--", "pyproject.toml", "uv.lock"]
    )
    python = _venv_python(repo)

    if deps_diff.stdout.strip():
        pip_result = dependency_installer(repo, python)
        if not pip_result.get("ok"):
            _run_git(repo, ["reset", "--hard", previous_commit])
            return {
                "applied": False, "reason": "dependency_install_failed", "rolled_back": True,
                "error": pip_result.get("stderr"), "from": previous_commit, "to": target_commit,
            }

    if run_tests:
        test_result = test_runner(repo, python)
        if not test_result.get("ok"):
            _run_git(repo, ["reset", "--hard", previous_commit])
            return {
                "applied": False, "reason": "tests_failed", "rolled_back": True,
                "from": previous_commit, "to": target_commit, "test_output": test_result.get("tail"),
            }

    result: dict[str, Any] = {"applied": True, "from": previous_commit, "to": target_commit}
    if restart:
        result["restart"] = restart_fn()
    return result


# ── Notification (best-effort, same primitive as escalation/learning_notice) ─

def _notify(vault: Path, text: str) -> bool:
    try:
        from .escalation import _notify_owner

        return bool(_notify_owner(text, chat_id=None, vault=vault))
    except Exception:
        return False


# ── The recurring job ────────────────────────────────────────────────────────

def ensure_self_update_job(*, db_path: Path | None = None, config: dict[str, Any] | None = None) -> str | None:
    """Seed the recurring check if the owner has resolved a posture and no
    instance of it is currently active. Safe to call on every service
    startup: it is a no-op once the series exists."""
    from ..config import load_config
    from .jobs import enqueue_job, ensure_jobs_table

    config = config or load_config()
    update_cfg = config.get("update") or {}
    mode = update_cfg.get("mode")
    if mode not in ("automatic", "notify"):
        return None
    interval_hours = max(1, int(update_cfg.get("check_interval_hours") or DEFAULT_CHECK_INTERVAL_HOURS))

    conn = _connect(db_path)
    try:
        ensure_jobs_table(conn)
        row = conn.execute(
            "SELECT id FROM jobs WHERE job_type = 'self_update.check' "
            "AND status IN ('queued', 'running', 'retry_wait') LIMIT 1"
        ).fetchone()
        if row:
            return str(row[0])
    finally:
        conn.close()

    return enqueue_job("self_update.check", {}, recurrence=f"every:{interval_hours}h", db_path=db_path)


def run_periodic_check(
    vault: Path | None = None, *, db_path: Path | None = None, repo: Path | None = None,
) -> dict[str, Any]:
    """The ``self_update.check`` job body. Non-fatal by design: a network
    blip or a failed apply is reported, never raised — the recurrence field
    requeues the next instance regardless (see jobs._requeue_recurring)."""
    from ..config import load_config
    from ..paths import vault_root

    vault = vault or vault_root()
    config = load_config()
    update_cfg = config.get("update") or {}
    mode = update_cfg.get("mode")
    if mode not in ("automatic", "notify"):
        return {"skipped": True, "reason": "mode_unresolved"}

    remote = str(update_cfg.get("remote") or "origin")
    branch = str(update_cfg.get("branch") or "main")
    repo = repo or repo_root()

    status = check_remote(repo, remote=remote, branch=branch)
    if not status.get("ok"):
        _write_update_state(db_path, checked_at=_now_iso(), last_error=str(status.get("error") or ""))
        return {"checked": True, "ok": False, "error": status.get("error")}

    _write_update_state(
        db_path,
        checked_at=_now_iso(),
        local_commit=status.get("local"),
        remote_commit=status.get("remote"),
        update_available=1 if status.get("update_available") else 0,
        last_error="",
    )

    if not status.get("update_available"):
        return {"checked": True, "ok": True, "update_available": False}

    if status.get("diverged"):
        state = read_update_state(db_path)
        if state.get("notified_commit") != status.get("remote"):
            _notify(
                vault,
                f"⚠️ Lisan: local commits have diverged from {remote}/{branch} — "
                "auto-update cannot fast-forward. Resolve manually (rebase or merge).",
            )
            _write_update_state(db_path, notified_commit=status.get("remote"))
        return {"checked": True, "ok": True, "diverged": True}

    if mode == "notify":
        state = read_update_state(db_path)
        if state.get("notified_commit") != status.get("remote"):
            behind = status.get("behind")
            local_short = str(status["local"])[:7]
            remote_short = str(status["remote"])[:7]
            _notify(
                vault,
                f"⬆️ Lisan update available: {behind} commit(s) behind on {branch} "
                f"({local_short} → {remote_short}). Run `lisan update apply`.",
            )
            _write_update_state(db_path, notified_commit=status.get("remote"))
        return {"checked": True, "ok": True, "update_available": True, "notified": True}

    # mode == "automatic"
    result = apply_update(repo, remote=remote, branch=branch)
    if result.get("applied"):
        _write_update_state(db_path, applied_commit=result.get("to"))
        _notify(vault, f"✓ Lisan updated {result['from'][:7]} → {result['to'][:7]} and restarted.")
    else:
        _write_update_state(db_path, last_error=str(result.get("reason") or ""))
        _notify(
            vault,
            f"✗ Lisan auto-update failed ({result.get('reason')}); still on "
            f"{str(status['local'])[:7]}. Check logs.",
        )
    return {"checked": True, "ok": True, **result}


def render_update_status(config: dict[str, Any], state: dict[str, Any]) -> str:
    update_cfg = config.get("update") or {}
    mode = update_cfg.get("mode")
    lines = [f"Update mode: {mode or 'unresolved (run `lisan update config --mode automatic|notify`)'}"]
    if mode in ("automatic", "notify"):
        lines.append(
            f"  checking {update_cfg.get('remote') or 'origin'}/{update_cfg.get('branch') or 'main'} "
            f"every {update_cfg.get('check_interval_hours') or DEFAULT_CHECK_INTERVAL_HOURS}h"
        )
    if state.get("checked_at"):
        lines.append(f"  last checked: {state['checked_at']}")
        local = str(state.get("local_commit") or "")[:7]
        remote = str(state.get("remote_commit") or "")[:7]
        lines.append(f"  local {local}  remote {remote}")
        if state.get("update_available"):
            lines.append("  an update is available")
        if state.get("last_error"):
            lines.append(f"  last error: {state['last_error']}")
    else:
        lines.append("  never checked")
    return "\n".join(lines)
