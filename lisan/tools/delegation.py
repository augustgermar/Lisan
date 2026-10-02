"""Delegated workers: scoped, durable, parallel children of the agent.

A child is one `agent.delegate` job. It runs a fresh `codex exec` with its
own sandbox, timeout and brief, sees nothing but that brief (no memory, no
Lisan tools, so it cannot delegate further), and returns a result. The queue
makes it durable; the scheduler's delegate lane makes it parallel; the run
ledger records it (origin "delegate").

Authority: a child's profile may never exceed the ceiling its parent held,
and `intent.md` never-rules outrank everything, as for every other executor.
Children are never retried automatically — a run that failed or timed out may
have done part of its work, and doing that part twice is worse than asking.

Design: docs/delegation_workorder.md.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from ..config import load_config

JOB_TYPE = "agent.delegate"

# profile name -> codex sandbox mode, weakest first
PROFILES = {
    "read_only": "read-only",
    "workspace_write": "workspace-write",
    "full": "danger-full-access",
}
_RANK = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}

# The queue requeues any job `running` past 45 minutes (jobs.reclaim_stale_
# running_jobs); a child that could outlive that would be run twice.
MAX_TIMEOUT_SECONDS = 2400
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_MAX_OUTSTANDING = 12
DEFAULT_MAX_CONCURRENT = 3

_ACTIVE_STATUSES = ("queued", "running", "retry_wait")


def _settings(config: dict[str, Any] | None) -> dict[str, Any]:
    return (config or {}).get("delegation") or {}


def max_concurrent(config: dict[str, Any] | None = None) -> int:
    try:
        return max(1, int(_settings(config if config is not None else load_config()).get("max_concurrent", DEFAULT_MAX_CONCURRENT)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONCURRENT


def profile_for_mode(mode: str) -> str:
    return next(name for name, value in PROFILES.items() if value == mode)


def caller_ceiling_mode(config: dict[str, Any]) -> str:
    """The sandbox mode the delegating executor itself runs under — the most a
    child it spawns may have."""
    from ..providers.codex import _resolve_sandbox_mode

    codex_config = (config.get("providers") or {}).get("codex") or {}
    return _resolve_sandbox_mode("codex", codex_config)


def _count_active(db_path: Path | None) -> int:
    from .jobs import list_jobs

    return sum(
        1
        for status in _ACTIVE_STATUSES
        for job in list_jobs(status=status, limit=5000, db_path=db_path)
        if job.get("job_type") == JOB_TYPE
    )


def delegate(
    brief: str,
    *,
    profile: str | None = None,
    working_directory: str | None = None,
    timeout_seconds: int | None = None,
    result_schema: dict[str, Any] | None = None,
    parent: dict[str, Any] | None = None,
    group_id: str | None = None,
    profile_ceiling: str | None = None,
    config: dict[str, Any] | None = None,
    vault: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Validate and enqueue one child. Raises ValueError with the reason when
    the request is refused, so the caller can say so plainly."""
    from .execution_tools import _chat_intent_verdict, codex_workspace
    from .jobs import enqueue_job

    config = config if config is not None else load_config()
    brief = str(brief or "").strip()
    if not brief:
        raise ValueError("a delegated task needs a brief")

    ceiling = profile_ceiling or caller_ceiling_mode(config)
    if ceiling not in _RANK:
        raise ValueError(f"unknown profile ceiling {ceiling!r}")
    if profile is None:
        profile = profile_for_mode(ceiling)  # a child defaults to its parent's own authority
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {sorted(PROFILES)}")
    if _RANK[PROFILES[profile]] > _RANK[ceiling]:
        raise ValueError(
            f"profile {profile!r} exceeds the delegating caller's authority "
            f"({profile_for_mode(ceiling)!r}); a child can never have more than its parent"
        )

    settings = _settings(config)
    if timeout_seconds is None:
        timeout_seconds = int(settings.get("default_timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    timeout_seconds = int(timeout_seconds)
    if not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS} (the queue requeues "
            "jobs running past 45 minutes); split longer work into several children"
        )

    wd = Path(working_directory).expanduser() if working_directory else Path(codex_workspace())
    if not wd.is_absolute():
        raise ValueError("working_directory must be an absolute path")

    if vault is not None:
        ruling = _chat_intent_verdict(vault)
        if ruling is not None and ruling[0].decision == "deny":
            verdict, version = ruling
            reasons = "; ".join(verdict.reasons or ["denied"])
            raise ValueError(f"intent.md (v{version}) forbids this: {verdict.rule} — {reasons}")

    cap = int(settings.get("max_outstanding") or DEFAULT_MAX_OUTSTANDING)
    if _count_active(db_path) >= cap:
        raise ValueError(f"{cap} delegated tasks are already queued or running; wait for some to finish")

    delegation_id = f"deleg.{uuid.uuid4().hex[:10]}"
    payload: dict[str, Any] = {
        "delegation_id": delegation_id,
        "group_id": group_id or delegation_id,
        "brief": brief,
        "profile": profile,
        "profile_ceiling": ceiling,
        "working_directory": str(wd),
        "timeout_seconds": timeout_seconds,
        "parent": parent or {"kind": "cli"},
    }
    if result_schema:
        payload["result_schema"] = result_schema
    # max_attempts=1: never retried by the queue (see module docstring).
    job_id = enqueue_job(JOB_TYPE, payload, max_attempts=1, db_path=db_path)
    return {
        "delegation_id": delegation_id,
        "group_id": payload["group_id"],
        "job_id": job_id,
        "profile": profile,
        "timeout_seconds": timeout_seconds,
    }


def _child_prompt(brief: str, working_directory: str, result_schema: dict[str, Any] | None) -> str:
    lines = [
        "You are a delegated worker. You were handed one self-contained task by another agent.",
        f"Working directory: {working_directory}",
        "You have no access to the delegating agent's memory or conversation; everything you "
        "need is in the task below. Do the task, then report what you actually did and found.",
        "Be honest about the outcome: if you could not finish, or only part of it worked, say "
        "exactly that and why. Never claim success you did not verify.",
        "",
        "TASK:",
        brief,
    ]
    return "\n".join(lines)


def run_delegation(
    job: dict[str, Any],
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one child. Raises on any failure so the job ends `failed` with
    the real cause; success returns the result the parent will read."""
    import json

    from ..paths import vault_root
    from ..providers.codex import CodexClient
    from .execution_tools import _chat_intent_verdict
    from .jobs import set_child_pid
    from .run_ledger import begin_run, finish_run

    vault = vault or vault_root()
    config = config if config is not None else load_config()
    payload = dict(job.get("payload") or {})
    delegation_id = str(payload.get("delegation_id") or job.get("id"))
    profile = str(payload.get("profile") or "")
    if profile not in PROFILES:
        raise RuntimeError(f"delegation {delegation_id}: unknown profile {profile!r}")
    mode = PROFILES[profile]
    ceiling = str(payload.get("profile_ceiling") or caller_ceiling_mode(config))
    if _RANK.get(ceiling, -1) < _RANK[mode]:
        raise RuntimeError(f"delegation {delegation_id}: profile {profile!r} exceeds its parent's authority")

    ruling = _chat_intent_verdict(vault)  # re-checked now: intent.md may have changed since enqueue
    if ruling is not None and ruling[0].decision == "deny":
        verdict, version = ruling
        raise RuntimeError(
            f"refused: intent.md (v{version}) forbids this — {verdict.rule}: {'; '.join(verdict.reasons or ['denied'])}"
        )

    wd = Path(str(payload.get("working_directory") or "")).expanduser()
    schema = payload.get("result_schema") or None
    prompt = _child_prompt(str(payload.get("brief") or ""), str(wd), schema)
    timeout = int(payload.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)

    run_id = begin_run(db_path, delegation_id, 1, origin="delegate")
    started = time.monotonic()
    try:
        response = CodexClient(config).complete(
            prompt,
            schema=schema,
            agent="delegate",
            significance="medium",
            working_directory=wd,
            sandbox_mode=mode,
            timeout_seconds=timeout,
            on_start=lambda pid: set_child_pid(str(job["id"]), pid, db_path=db_path),
        )
    except Exception as exc:
        from .jobs import _job_was_canceled

        # The kill that cancel sends surfaces here as an exec failure; the
        # ledger should record what the owner did, not "exit code -9".
        if _job_was_canceled(job["id"], db_path):
            finish_run(db_path, run_id, ok=False, status="canceled", error="canceled by owner")
        else:
            finish_run(db_path, run_id, ok=False, error=f"{exc.__class__.__name__}: {exc}")
        raise
    finally:
        set_child_pid(str(job["id"]), None, db_path=db_path)
    finish_run(db_path, run_id, ok=True)

    result: dict[str, Any] = {
        "delegation_id": delegation_id,
        "group_id": payload.get("group_id"),
        "profile": profile,
        "status": "succeeded",
        "text": response.text.strip(),
        "duration_s": round(time.monotonic() - started, 1),
    }
    if schema:
        try:
            result["data"] = json.loads(response.text)
        except ValueError:
            pass
    return result


# ── Visibility and control ───────────────────────────────────────────────────

def _delegation_view(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    return {
        "delegation_id": payload.get("delegation_id") or job.get("id"),
        "group_id": payload.get("group_id"),
        "job_id": job.get("id"),
        "status": job.get("status"),
        "profile": payload.get("profile"),
        "brief": payload.get("brief"),
        "parent": payload.get("parent"),
        "timeout_seconds": payload.get("timeout_seconds"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "result": job.get("result"),
    }


def list_delegations(*, db_path: Path | None = None, limit: int = 50) -> list[dict[str, Any]]:
    from .jobs import list_jobs

    jobs = [j for j in list_jobs(limit=5000, db_path=db_path) if j.get("job_type") == JOB_TYPE]
    jobs.sort(key=lambda j: str(j.get("created_at") or ""), reverse=True)
    return [_delegation_view(j) for j in jobs[:limit]]


def _find_job(delegation_id: str, db_path: Path | None) -> dict[str, Any] | None:
    from .jobs import list_jobs

    for job in list_jobs(limit=5000, db_path=db_path):
        if job.get("job_type") != JOB_TYPE:
            continue
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        if delegation_id in (payload.get("delegation_id"), job.get("id")):
            return job
    return None


def show_delegation(delegation_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    job = _find_job(delegation_id, db_path)
    return _delegation_view(job) if job else None


def cancel_delegation(delegation_id: str, *, db_path: Path | None = None) -> bool:
    """Cancel a queued or running child; a running one has its process group killed."""
    from .jobs import cancel_job

    job = _find_job(delegation_id, db_path)
    if job is None or job.get("status") not in _ACTIVE_STATUSES:
        return False
    cancel_job(str(job["id"]), db_path=db_path)
    return True


def format_delegations(items: list[dict[str, Any] | None]) -> str:
    items = [i for i in items if i]
    if not items:
        return "No delegated tasks."
    lines = []
    for d in items:
        brief = str(d.get("brief") or "")
        brief = brief if len(brief) <= 60 else brief[:57] + "..."
        lines.append(f"{d['delegation_id']}  [{d['status']}]  {d.get('profile')}  — {brief}")
        if d.get("status") == "failed" and d.get("error"):
            lines.append(f"   error: {str(d['error'])[:200]}")
        result = d.get("result")
        if d.get("status") == "succeeded" and isinstance(result, dict):
            if isinstance(result.get("data"), dict):
                summary = ", ".join(
                    f"{k}: {len(v)} item(s)" if isinstance(v, list) else k for k, v in result["data"].items()
                )
                lines.append(f"   result: structured ({summary}) — `lisan delegate show {d['delegation_id']}`")
            elif result.get("text"):
                lines.append(f"   result: {str(result['text'])[:300]}")
    return "\n".join(lines)


# ── Crash recovery ───────────────────────────────────────────────────────────

# Past a child's own timeout the worker that enforces it must be gone. The
# grace covers process startup/teardown, not slack for a live child.
OVERDUE_GRACE_SECONDS = 120


def _looks_like_our_child(pid: int) -> bool:
    """Guard against pid reuse: only signal a process that is still codex.
    A recorded pid can outlive the process it named, and killing an unrelated
    process group is far worse than leaving an orphan to finish."""
    import os
    import subprocess

    names = {os.path.basename(os.environ.get("CODEX_BIN") or "codex"), "codex"}
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(name and name in out for name in names)


def reap_overdue_delegations(
    db_path: Path | None = None,
    *,
    vault: Path | None = None,
    now: float | None = None,
) -> list[str]:
    """Fail children whose worker died mid-run.

    A `running` child older than its timeout plus a grace has no live worker
    (the worker kills at the timeout). The queue's generic reclaim would
    requeue it and run it a second time; a child may have partly run, so it is
    failed with the real cause instead, any surviving orphan process is
    killed, and the owner is told once. Returns the ids reaped."""
    from datetime import datetime, timezone

    from .escalation import escalate_terminal_failure
    from .jobs import _kill_child_group, list_jobs, mark_job_failed, set_child_pid

    now = time.time() if now is None else now
    reaped: list[str] = []
    for job in list_jobs(status="running", limit=5000, db_path=db_path):
        if job.get("job_type") != JOB_TYPE or not job.get("started_at"):
            continue
        try:
            started = datetime.strptime(str(job["started_at"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        limit = int(payload.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS) + OVERDUE_GRACE_SECONDS
        if now - started < limit:
            continue
        pid = job.get("child_pid")
        killed = False
        if pid and _looks_like_our_child(int(pid)):
            killed = _kill_child_group(int(pid))
        error = (
            "the worker running this died mid-run (no result by its timeout + grace)"
            + ("; its orphaned process was killed" if killed else "")
            + ". It may have partly run, so it was not retried."
        )
        updated = mark_job_failed(str(job["id"]), error, retry=False, db_path=db_path)
        set_child_pid(str(job["id"]), None, db_path=db_path)
        escalate_terminal_failure(updated or job, error, vault=vault, db_path=db_path)
        reaped.append(str(payload.get("delegation_id") or job["id"]))
    return reaped
