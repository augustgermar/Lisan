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
DEFAULT_MAX_CHILDREN = 6  # per fan-out / per chat call

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


def normalize_child_spec(
    spec: dict[str, Any],
    *,
    ceiling: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Validate one child's request and fill its defaults. Raises ValueError
    with the reason. Used both when a child is queued and when a plan is
    created, so a bad fan-out fails at creation, not at 3am."""
    from .execution_tools import codex_workspace

    brief = str(spec.get("brief") or "").strip()
    if not brief:
        raise ValueError("a delegated task needs a brief")
    if ceiling not in _RANK:
        raise ValueError(f"unknown profile ceiling {ceiling!r}")

    profile = spec.get("profile")
    if profile is None:
        profile = profile_for_mode(ceiling)  # a child defaults to its parent's own authority
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {sorted(PROFILES)}")
    if _RANK[PROFILES[profile]] > _RANK[ceiling]:
        raise ValueError(
            f"profile {profile!r} exceeds the delegating caller's authority "
            f"({profile_for_mode(ceiling)!r}); a child can never have more than its parent"
        )

    timeout_seconds = spec.get("timeout_seconds")
    if timeout_seconds is None:
        timeout_seconds = _settings(config).get("default_timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        raise ValueError("timeout_seconds must be a whole number of seconds") from None
    if not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS} (the queue requeues "
            "jobs running past 45 minutes); split longer work into several children"
        )

    working_directory = spec.get("working_directory")
    wd = Path(working_directory).expanduser() if working_directory else Path(codex_workspace())
    if not wd.is_absolute():
        raise ValueError("working_directory must be an absolute path")

    normalized: dict[str, Any] = {
        "brief": brief,
        "profile": profile,
        "timeout_seconds": timeout_seconds,
        "working_directory": str(wd),
    }
    if spec.get("result_schema"):
        normalized["result_schema"] = spec["result_schema"]
    return normalized


def _refuse_if_intent_denies(vault: Path | None) -> None:
    if vault is None:
        return
    from .execution_tools import _chat_intent_verdict

    ruling = _chat_intent_verdict(vault)
    if ruling is not None and ruling[0].decision == "deny":
        verdict, version = ruling
        reasons = "; ".join(verdict.reasons or ["denied"])
        raise ValueError(f"intent.md (v{version}) forbids this: {verdict.rule} — {reasons}")


def _enqueue_child(
    normalized: dict[str, Any],
    *,
    ceiling: str,
    parent: dict[str, Any] | None,
    group_id: str | None,
    job_id: str | None,
    db_path: Path | None,
) -> dict[str, Any]:
    from .jobs import enqueue_job

    delegation_id = f"deleg.{uuid.uuid4().hex[:10]}"
    payload: dict[str, Any] = {
        "delegation_id": delegation_id,
        "group_id": group_id or delegation_id,
        **normalized,
        "profile_ceiling": ceiling,
        "parent": parent or {"kind": "cli"},
    }
    # max_attempts=1: never retried by the queue (see module docstring).
    queued_id = enqueue_job(JOB_TYPE, payload, max_attempts=1, db_path=db_path, job_id=job_id)
    return {
        "delegation_id": delegation_id,
        "group_id": payload["group_id"],
        "job_id": queued_id,
        "profile": normalized["profile"],
        "timeout_seconds": normalized["timeout_seconds"],
    }


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
    config = config if config is not None else load_config()
    ceiling = profile_ceiling or caller_ceiling_mode(config)
    normalized = normalize_child_spec(
        {
            "brief": brief, "profile": profile, "working_directory": working_directory,
            "timeout_seconds": timeout_seconds, "result_schema": result_schema,
        },
        ceiling=ceiling,
        config=config,
    )
    _refuse_if_intent_denies(vault)
    cap = int(_settings(config).get("max_outstanding") or DEFAULT_MAX_OUTSTANDING)
    if _count_active(db_path) >= cap:
        raise ValueError(f"{cap} delegated tasks are already queued or running; wait for some to finish")
    return _enqueue_child(
        normalized, ceiling=ceiling, parent=parent, group_id=group_id, job_id=None, db_path=db_path
    )


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
        # -ww: without it Linux procps truncates the command to 80 columns when
        # stdout is not a terminal, cutting the script name off a long path
        # (CI: the overdue-child test never killed its orphan).
        out = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
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


# ── Groups: durable fan-out and join ─────────────────────────────────────────
#
# A group is a set of children plus the one thing to do when they have all
# finished (its continuation): resume a plan, or report to the owner. The join
# is a row, not payload state, so it survives a crash anywhere in the sequence:
#
#   1. children are enqueued under deterministic ids (a re-run cannot double-launch)
#   2. the group row is written
#   3. settle_groups() enqueues the continuation under a deterministic id, THEN
#      marks the group settled. A crash between the two is repaired by the next
#      sweep, because enqueueing an existing id is a no-op.

REPORT_JOB_TYPE = "agent.delegate_report"
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled", "archived"})

_GROUPS_DDL = """
CREATE TABLE IF NOT EXISTS delegation_groups (
    group_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    parent_ref TEXT,
    child_job_ids TEXT NOT NULL,
    continuation_type TEXT NOT NULL,
    continuation_payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'waiting',
    created_at TEXT NOT NULL,
    settled_at TEXT
)
"""


def _groups_conn(db_path: Path | None):
    import sqlite3

    from .db import connect

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_GROUPS_DDL)
    return conn


def _utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def max_children(config: dict[str, Any] | None = None) -> int:
    try:
        return max(1, int(_settings(config if config is not None else load_config()).get("max_children", DEFAULT_MAX_CHILDREN)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHILDREN


def child_job_ids_for(group_id: str, count: int) -> list[str]:
    """Deterministic ids: re-running a half-finished launch re-uses them."""
    return [f"job.child.{group_id}.{i + 1}" for i in range(count)]


def launch_group(
    specs: list[dict[str, Any]],
    *,
    group_id: str,
    kind: str,
    parent: dict[str, Any],
    continuation_type: str,
    continuation_payload: dict[str, Any],
    ceiling: str,
    config: dict[str, Any] | None = None,
    vault: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Validate every child, enqueue them, record the group. All-or-nothing on
    validation (nothing is queued if any spec is bad); idempotent on re-run."""
    import json

    from .jobs import get_job

    config = config if config is not None else load_config()
    limit = max_children(config)
    if not specs:
        raise ValueError("a group needs at least one child")
    if len(specs) > limit:
        raise ValueError(f"too many children ({len(specs)}); at most {limit} per group (delegation.max_children)")
    normalized = [normalize_child_spec(spec, ceiling=ceiling, config=config) for spec in specs]
    _refuse_if_intent_denies(vault)

    ids = child_job_ids_for(group_id, len(normalized))
    fresh = sum(1 for job_id in ids if get_job(job_id, db_path=db_path) is None)
    cap = int(_settings(config).get("max_outstanding") or DEFAULT_MAX_OUTSTANDING)
    if fresh and _count_active(db_path) + fresh > cap:
        raise ValueError(
            f"{cap} delegated tasks may be queued or running at once and this would exceed that; "
            "wait for some to finish"
        )

    children = [
        _enqueue_child(spec, ceiling=ceiling, parent=parent, group_id=group_id, job_id=job_id, db_path=db_path)
        for spec, job_id in zip(normalized, ids)
    ]
    conn = _groups_conn(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO delegation_groups "
            "(group_id, kind, parent_ref, child_job_ids, continuation_type, continuation_payload, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'waiting', ?)",
            (
                group_id, kind, json.dumps(parent, sort_keys=True), json.dumps(ids),
                continuation_type, json.dumps(continuation_payload), _utc_stamp(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {"group_id": group_id, "child_job_ids": ids, "children": children}


def get_group(group_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    import json

    conn = _groups_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM delegation_groups WHERE group_id = ?", (group_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    group = dict(row)
    group["child_job_ids"] = json.loads(group["child_job_ids"])
    group["continuation_payload"] = json.loads(group["continuation_payload"])
    return group


def group_children(group_id: str, *, db_path: Path | None = None) -> list[dict[str, Any]]:
    """What each child did, in launch order."""
    from .jobs import get_job

    group = get_group(group_id, db_path=db_path)
    if group is None:
        return []
    out: list[dict[str, Any]] = []
    for job_id in group["child_job_ids"]:
        job = get_job(job_id, db_path=db_path)
        if job is None:
            out.append({"job_id": job_id, "status": "missing", "error": "the job is gone"})
            continue
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        out.append({
            "job_id": job_id,
            "delegation_id": payload.get("delegation_id"),
            "status": job.get("status"),
            "profile": payload.get("profile"),
            "brief": payload.get("brief") or "",
            "text": str(result.get("text") or ""),
            "data": result.get("data"),
            "error": str(job.get("error") or ""),
            "duration_s": result.get("duration_s"),
        })
    return out


def settle_groups(db_path: Path | None = None) -> list[str]:
    """Enqueue the continuation of every waiting group whose children have all
    finished; returns the group ids settled. Safe to call from any number of
    workers at once, and as often as you like."""
    import json

    from .jobs import enqueue_job

    conn = _groups_conn(db_path)
    try:
        waiting = conn.execute("SELECT * FROM delegation_groups WHERE state = 'waiting'").fetchall()
    finally:
        conn.close()
    settled: list[str] = []
    for row in waiting:
        if not all(child["status"] in _TERMINAL_STATUSES | {"missing"} for child in group_children(row["group_id"], db_path=db_path)):
            continue
        enqueue_job(
            row["continuation_type"],
            json.loads(row["continuation_payload"]),
            job_id=f"job.join.{row['group_id']}",  # deterministic: a second enqueue is a no-op
            db_path=db_path,
        )
        conn = _groups_conn(db_path)
        try:
            conn.execute(
                "UPDATE delegation_groups SET state = 'settled', settled_at = ? WHERE group_id = ? AND state = 'waiting'",
                (_utc_stamp(), row["group_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        settled.append(row["group_id"])
    return settled


def cancel_group(group_id: str, *, db_path: Path | None = None) -> int:
    """Cancel every unfinished child (killing running ones) and mark the group
    canceled so it never enqueues its continuation. Returns children canceled."""
    from .jobs import cancel_job, get_job

    group = get_group(group_id, db_path=db_path)
    if group is None:
        return 0
    conn = _groups_conn(db_path)
    try:
        conn.execute(
            "UPDATE delegation_groups SET state = 'canceled', settled_at = ? WHERE group_id = ? AND state = 'waiting'",
            (_utc_stamp(), group_id),
        )
        conn.commit()
    finally:
        conn.close()
    canceled = 0
    for job_id in group["child_job_ids"]:
        job = get_job(job_id, db_path=db_path)
        if job is not None and job.get("status") in _ACTIVE_STATUSES:
            cancel_job(job_id, db_path=db_path)
            canceled += 1
    return canceled


# ── Reporting a finished group ───────────────────────────────────────────────

def _first_line(text: str, limit: int) -> str:
    line = " ".join(str(text).strip().splitlines()[:1])
    return line if len(line) <= limit else line[: limit - 1] + "…"


def group_summary_message(goal: str, children: list[dict[str, Any]], *, text_limit: int = 600) -> str:
    """The owner-facing message for a finished group: every child's outcome and
    what it found, unsoftened. Failures carry their real error."""
    failed = [c for c in children if c["status"] != "succeeded"]
    icon = "✅" if not failed else "⚠️"
    head = f"{icon} Delegated work finished: {goal}" if goal else f"{icon} Delegated work finished"
    if failed:
        head += f" ({len(children) - len(failed)} of {len(children)} succeeded)"
    lines = [head]
    for i, child in enumerate(children, start=1):
        brief = _first_line(child.get("brief") or "", 80)
        if child["status"] == "succeeded":
            took = f", {child['duration_s']}s" if child.get("duration_s") is not None else ""
            lines.append(f"{i}. ✓ {brief} ({child.get('profile')}{took})")
            body = child.get("text") or ""
            if body:
                lines.append("   " + (body if len(body) <= text_limit else body[:text_limit] + "…").replace("\n", "\n   "))
        else:
            why = child.get("error") or child["status"]
            lines.append(f"{i}. ✗ {brief} — {child['status']}: {_first_line(why, 200)}")
    ids = [c.get("delegation_id") for c in children if c.get("delegation_id")]
    if ids:
        lines.append(f"Full results: lisan delegate show {ids[0]}" + (" (and the others)" if len(ids) > 1 else ""))
    return "\n".join(lines)


def run_delegation_report(
    job: dict[str, Any],
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
    send_fn: Any = None,
    capture: Any = None,
) -> dict[str, Any]:
    """The continuation of a chat group: one capture turn for the whole group
    (so the Skeptic reads what the workers claimed, once) and one message to
    the owner. Both are best-effort — the children's results stay on their job
    rows either way — but a failure to deliver is logged, not hidden."""
    from ..paths import vault_root
    from .adjutant_executor import ExecutionResult
    from .adjutant_reporter import report_result

    vault = vault or vault_root()
    payload = dict(job.get("payload") or {})
    group_id = str(payload.get("group_id") or "")
    goal = str(payload.get("goal") or "")
    children = group_children(group_id, db_path=db_path)
    if not children:
        raise RuntimeError(f"delegation group {group_id!r} has no children to report")
    failed = [c for c in children if c["status"] != "succeeded"]

    result = ExecutionResult(
        task_id=group_id,
        kind="delegation",
        ok=not failed,
        actions=[
            f"{c.get('profile') or '?'} worker: {_first_line(c.get('brief') or '', 140)} -> {c['status']}"
            for c in children
        ],
        findings=[
            {"delegation": c.get("delegation_id"), "result": (c.get("text") or "")[:1200]}
            for c in children if c["status"] == "succeeded" and c.get("text")
        ],
        errors=[
            f"{_first_line(c.get('brief') or '', 100)}: {c['status']} — {_first_line(c.get('error') or '', 300)}"
            for c in failed
        ],
    )
    try:
        report_result(
            vault, result,
            verdict_path=f"delegated from chat: {goal[:120]}" if goal else "delegated from chat",
            db_path=db_path, capture=capture,
        )
    except Exception as exc:
        _log_delegation_error(vault, "delegation report: capture failed", exc)

    try:
        from .learning import record_group_event

        record_group_event(
            group_id, goal, children, conversation_id=payload.get("conversation_id"), vault=vault, db_path=db_path,
            config=load_config(),
        )
    except Exception as exc:
        _log_delegation_error(vault, "delegation report: learning event failed", exc)

    message = group_summary_message(goal, children)
    chat_id = payload.get("chat_id")
    chat_id = int(chat_id) if chat_id is not None else None
    delivered = False
    try:
        if send_fn is not None:
            send_fn(message, chat_id)
        else:
            from .scheduler import _deliver_owner_message

            _deliver_owner_message(message, chat_id=chat_id, config=None)
        delivered = True
    except Exception as exc:
        _log_delegation_error(vault, "delegation report: delivery failed", exc)
    return {
        "group_id": group_id,
        "children": len(children),
        "succeeded": len(children) - len(failed),
        "delivered": delivered,
    }


def _log_delegation_error(vault: Path, what: str, exc: Exception) -> None:
    try:
        from .log import log_error

        log_error(vault, what, exc)
    except Exception:
        pass


def launch_chat_group(
    tasks: list[dict[str, Any]],
    *,
    goal: str = "",
    chat_id: int | None = None,
    conversation_id: str | None = None,
    config: dict[str, Any] | None = None,
    vault: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Start a set of children for the conversation agent; their results come
    back as one report when the last one finishes."""
    config = config if config is not None else load_config()
    group_id = f"grp.chat.{uuid.uuid4().hex[:10]}"
    continuation = {"group_id": group_id, "goal": goal, "conversation_id": conversation_id}
    if chat_id is not None:
        continuation["chat_id"] = int(chat_id)
    out = launch_group(
        tasks,
        group_id=group_id,
        kind="chat",
        parent={"kind": "chat", "ref": conversation_id or ""},
        continuation_type=REPORT_JOB_TYPE,
        continuation_payload=continuation,
        ceiling=caller_ceiling_mode(config),
        config=config,
        vault=vault,
        db_path=db_path,
    )
    out["goal"] = goal
    return out
