"""Durable multi-step plans, executed through the job queue.

A plan is how an ambiguous goal ("figure out what's wrong with the
hyperdrive") becomes tracked work: an ordered list of steps that survives
restarts, executes in the background one step at a time, and reports
honestly when it finishes or fails.

Mechanics: each step runs as its own `plan.run` job row so it inherits the
queue's retry and trace semantics; the payload carries the whole plan state
(goal, steps, log) forward, and completing a step enqueues the next. The
scheduler loop picks steps up within seconds. Completion and failure both
deliver a summary to the owner (owner-only Telegram delivery, same channel
as reminders) and write a report into the vault.

Approval model: the owner approves a plan once, at creation — that approval
covers its codex steps, because the firing runs unattended and creation is
the only moment anyone can say no.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable

from ..config import load_config
from ..paths import vault_root
from ..utils import utc_now_iso

STEP_KINDS = {"codex", "prompt", "note"}
_MAX_STEPS = 12
_RESULT_PREVIEW = 600


def create_plan(
    *,
    goal: str,
    steps: list[dict[str, str]],
    chat_id: int | None = None,
    conversation_id: str | None = None,
    working_directory: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Validate and enqueue a plan. The first step is claimable immediately."""
    from .jobs import enqueue_job

    goal = str(goal or "").strip()
    if not goal:
        raise ValueError("a plan needs a goal")
    if not steps:
        raise ValueError("a plan needs at least one step")
    if len(steps) > _MAX_STEPS:
        raise ValueError(f"too many steps ({len(steps)}); a plan may have at most {_MAX_STEPS}")

    normalized: list[dict[str, Any]] = []
    for i, step in enumerate(steps, start=1):
        kind = str(step.get("kind") or "codex").strip().lower()
        description = str(step.get("description") or "").strip()
        if kind not in STEP_KINDS:
            raise ValueError(f"step {i}: unknown kind {kind!r}; expected one of {sorted(STEP_KINDS)}")
        if not description:
            raise ValueError(f"step {i}: empty description")
        normalized.append({"kind": kind, "description": description, "status": "pending", "result": ""})

    plan_id = f"plan.{uuid.uuid4().hex[:10]}"
    payload: dict[str, Any] = {
        "plan_id": plan_id,
        "goal": goal,
        "steps": normalized,
        "current_step": 0,
        "created_at": utc_now_iso(),
    }
    if chat_id is not None:
        payload["chat_id"] = int(chat_id)
    if conversation_id:
        payload["conversation_id"] = str(conversation_id)
    if working_directory:
        payload["working_directory"] = str(working_directory)

    job_id = enqueue_job("plan.run", payload, db_path=db_path)
    return {"plan_id": plan_id, "job_id": job_id, "goal": goal, "steps": len(normalized)}


def run_plan_step(
    job: dict[str, Any],
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    config: dict[str, Any] | None = None,
    send_fn: Callable[[str, int | None], Any] | None = None,
) -> dict[str, Any]:
    """Execute exactly one step, then either enqueue the successor or finish.

    A step failure ends the plan honestly — remaining steps are marked
    skipped, the owner gets the failure report. Infra-level exceptions still
    propagate so the queue's own retry logic applies to the *same* step.
    """
    from .jobs import enqueue_job

    vault = vault or vault_root()
    payload = dict(job.get("payload") or {})
    steps = [dict(s) for s in payload.get("steps") or []]
    index = int(payload.get("current_step") or 0)
    if index >= len(steps):
        return {"plan_id": payload.get("plan_id"), "status": "completed", "note": "no steps remaining"}

    step = steps[index]
    outcome_text, ok = _execute_step(
        step, payload, vault=vault, db_path=db_path, provider=provider, model=model, config=config
    )
    step["status"] = "done" if ok else "failed"
    step["result"] = outcome_text[:_RESULT_PREVIEW]
    steps[index] = step
    payload["steps"] = steps

    if ok:
        payload["current_step"] = index + 1
    # Persist the post-step state onto this job row: the row that ran the
    # step must carry the truth about it, or plan progress becomes invisible
    # once the chain ends.
    _persist_payload(job, payload, db_path=db_path)

    if not ok:
        for later in steps[index + 1:]:
            later["status"] = "skipped"
        _finish_plan(payload, vault=vault, status="failed", send_fn=send_fn, config=config)
        return {"plan_id": payload["plan_id"], "status": "failed", "failed_step": index + 1, "result": outcome_text[:_RESULT_PREVIEW]}

    if payload["current_step"] >= len(steps):
        _finish_plan(payload, vault=vault, status="completed", send_fn=send_fn, config=config)
        return {"plan_id": payload["plan_id"], "status": "completed", "steps_done": len(steps)}

    next_job = enqueue_job("plan.run", payload, db_path=db_path)
    return {
        "plan_id": payload["plan_id"],
        "status": "step_done",
        "step": index + 1,
        "next_job": next_job,
        "result": outcome_text[:_RESULT_PREVIEW],
    }


def handle_terminal_failure(job: dict[str, Any], *, vault: Path | None = None, db_path: Path | None = None) -> None:
    """A plan.run job that exhausted its retries died on infrastructure, not
    on a step outcome — the plan must still end honestly: steps marked,
    owner notified, report written."""
    vault = vault or vault_root()
    payload = dict(job.get("payload") or {})
    steps = [dict(s) for s in payload.get("steps") or []]
    index = int(payload.get("current_step") or 0)
    if not payload.get("plan_id") or not steps:
        return
    if index < len(steps):
        steps[index]["status"] = "failed"
        steps[index]["result"] = f"job error: {str(job.get('error') or 'unknown')[:300]}"
        for later in steps[index + 1:]:
            later["status"] = "skipped"
    payload["steps"] = steps
    _persist_payload(job, payload, db_path=db_path)
    _finish_plan(payload, vault=vault, status="failed", send_fn=None, config=None)


def _persist_payload(job: dict[str, Any], payload: dict[str, Any], *, db_path: Path | None) -> None:
    from ..utils import json_dumps_stable
    from .db import connect

    try:
        conn = connect(db_path)
        try:
            conn.execute("UPDATE jobs SET payload_json = ? WHERE id = ?", (json_dumps_stable(payload), job.get("id")))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _execute_step(
    step: dict[str, Any],
    payload: dict[str, Any],
    *,
    vault: Path,
    db_path: Path | None,
    provider: str | None,
    model: str | None,
    config: dict[str, Any] | None,
) -> tuple[str, bool]:
    kind = step["kind"]
    description = step["description"]
    context = _plan_context(payload)

    if kind == "note":
        return description, True

    if kind == "codex":
        # Approved once, at plan creation — the step never blocks on a prompt
        # nobody is present to answer. The provider is called directly so a
        # failure is an exception, not a string to be sniffed.
        from ..providers.codex import CodexClient
        from .execution_tools import _build_codex_prompt

        wd = Path(str(payload.get("working_directory") or "~")).expanduser()
        prompt = _build_codex_prompt(
            task=f"{description}\n\n{context}", working_directory=wd, vault=vault, db_path=db_path
        )
        try:
            response = CodexClient(config or load_config()).complete(
                prompt, agent="codex", significance="medium", working_directory=wd
            )
            return response.text.strip(), True
        except Exception as exc:
            return f"{exc.__class__.__name__}: {exc}", False

    if kind == "prompt":
        from .chat import _process_chat_turn

        turn = _process_chat_turn(
            vault=vault,
            conversation_id=str(payload.get("conversation_id") or f"plan-{payload['plan_id']}"),
            text=f"{description}\n\n{context}",
            provider=provider,
            model=model,
            db_path=db_path,
        )
        response = str(turn.get("response") or "").strip()
        if response:
            return response, True
        return str(turn.get("error") or "the pipeline produced no response"), False

    return f"unknown step kind {kind!r}", False


def _plan_context(payload: dict[str, Any]) -> str:
    """Brief the step executor on the goal and what earlier steps produced."""
    lines = [f"This is one step of a larger plan. Overall goal: {payload['goal']}"]
    done = [s for s in payload.get("steps") or [] if s.get("status") == "done"]
    if done:
        lines.append("Results of earlier steps:")
        for i, s in enumerate(done, start=1):
            lines.append(f"{i}. {s['description']} -> {s.get('result') or '(no output)'}")
    return "\n".join(lines)


def _finish_plan(
    payload: dict[str, Any],
    *,
    vault: Path,
    status: str,
    send_fn: Callable[[str, int | None], Any] | None,
    config: dict[str, Any] | None,
) -> None:
    report_path = _write_plan_report(payload, vault=vault, status=status)
    summary = _summary_message(payload, status=status)
    chat_id = payload.get("chat_id")
    chat_id = int(chat_id) if chat_id is not None else None
    try:
        if send_fn is not None:
            send_fn(summary, chat_id)
        else:
            from .scheduler import _deliver_owner_message

            _deliver_owner_message(summary, chat_id=chat_id, config=config)
    except Exception:
        # Delivery is best-effort: the report file and the job result remain
        # the durable record either way.
        pass
    payload["report_path"] = str(report_path)


def _summary_message(payload: dict[str, Any], *, status: str) -> str:
    steps = payload.get("steps") or []
    done = sum(1 for s in steps if s.get("status") == "done")
    icon = "✅" if status == "completed" else "⚠️"
    lines = [f"{icon} Plan {status}: {payload['goal']}", f"{done}/{len(steps)} steps done."]
    for i, s in enumerate(steps, start=1):
        mark = {"done": "✓", "failed": "✗", "skipped": "–", "pending": "…"}.get(s.get("status"), "?")
        lines.append(f"{mark} {i}. {s['description']}")
        if s.get("status") == "failed" and s.get("result"):
            lines.append(f"   failure: {s['result'][:200]}")
    return "\n".join(lines)


def _write_plan_report(payload: dict[str, Any], *, vault: Path, status: str) -> Path:
    reports = vault / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{payload['plan_id']}.md"
    lines = [
        f"# Plan report: {payload['goal']}",
        "",
        f"- plan_id: {payload['plan_id']}",
        f"- status: {status}",
        f"- created: {payload.get('created_at')}",
        f"- finished: {utc_now_iso()}",
        "",
    ]
    for i, s in enumerate(payload.get("steps") or [], start=1):
        lines.append(f"## Step {i} ({s['kind']}, {s['status']})")
        lines.append("")
        lines.append(s["description"])
        if s.get("result"):
            lines.append("")
            lines.append("Result:")
            lines.append("")
            lines.append(s["result"])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── Visibility ───────────────────────────────────────────────────────────────

def list_plans(*, db_path: Path | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """One entry per plan_id: the most recent job row carries current state."""
    from .jobs import list_jobs

    latest: dict[str, dict[str, Any]] = {}
    for job in list_jobs(limit=5000, db_path=db_path):
        if job.get("job_type") != "plan.run":
            continue
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        plan_id = str(payload.get("plan_id") or "")
        if not plan_id:
            continue
        seen = latest.get(plan_id)
        if seen is None or _plan_progress_key(job) >= _plan_progress_key(seen):
            latest[plan_id] = job
    plans = []
    for plan_id, job in latest.items():
        payload = job.get("payload") or {}
        steps = payload.get("steps") or []
        plans.append({
            "plan_id": plan_id,
            "goal": payload.get("goal") or "",
            "job_status": job.get("status"),
            "steps_total": len(steps),
            "steps_done": sum(1 for s in steps if s.get("status") == "done"),
            "active": job.get("status") in {"queued", "running", "retry_wait"},
            "created_at": payload.get("created_at"),
            "job_id": job.get("id"),
            "result": job.get("result"),
        })
    plans.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
    return plans[:limit]


def _plan_progress_key(job: dict[str, Any]) -> tuple[int, str]:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    return (int(payload.get("current_step") or 0), str(job.get("created_at") or ""))


def active_plans(*, db_path: Path | None = None) -> list[dict[str, Any]]:
    return [p for p in list_plans(db_path=db_path) if p["active"]]


def format_plans(plans: list[dict[str, Any]]) -> str:
    if not plans:
        return "No plans."
    lines = []
    for p in plans:
        state = "active" if p["active"] else str(p["job_status"])
        goal = p["goal"] if len(p["goal"]) <= 70 else p["goal"][:67] + "..."
        lines.append(f"{p['plan_id']}  [{state}]  {p['steps_done']}/{p['steps_total']} steps  — {goal}")
    return "\n".join(lines)


def cancel_plan(plan_id: str, *, db_path: Path | None = None) -> bool:
    """Cancel the pending job row that carries this plan forward."""
    from .jobs import cancel_job, list_jobs

    for job in list_jobs(limit=5000, db_path=db_path):
        if job.get("job_type") != "plan.run":
            continue
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        if str(payload.get("plan_id")) == plan_id and job.get("status") in {"queued", "retry_wait"}:
            cancel_job(str(job.get("id")), db_path=db_path)
            return True
    return False
# ── Folder ingestion autopilot ───────────────────────────────────────────────

def build_folder_ingestion_plan(
    path: str | Path,
    *,
    batch_size: int = 6,
    limit: int | None = None,
    chat_id: int | None = None,
    conversation_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Turn "work through this folder" into a durable plan: batched codex
    steps that read and reference-ingest the notes, notice recurring people
    and projects, and collect questions only the owner can answer; a closing
    step reports it all back conversationally. This is the agent doing what a
    briefed codex session would do — because each step IS a briefed codex
    session, with the plan carrying the thread between them."""
    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise ValueError(f"not a directory: {folder}")
    files = sorted(f for f in folder.rglob("*.md") if f.is_file())
    if not files:
        raise ValueError(f"no markdown files under {folder}")
    if limit:
        files = files[: int(limit)]

    batch_size = max(1, int(batch_size))
    batches = [files[i: i + batch_size] for i in range(0, len(files), batch_size)]
    steps: list[dict[str, str]] = []
    for number, batch in enumerate(batches, start=1):
        file_list = "\n".join(f"- {f}" for f in batch)
        steps.append({
            "kind": "codex",
            "description": (
                f"Ingest batch {number}/{len(batches)} of the owner's notes into Lisan memory. "
                "For EACH file below, run: lisan ingest --reference '<file path>' "
                "(quote the path; it may contain spaces). Then read the ingested notes and report, compactly: "
                "(1) per file, the chunk count or any ingest warning; "
                "(2) people, places, and projects that appear repeatedly across this batch; "
                "(3) QUESTIONS: anything ambiguous only the owner can resolve — unclear references, "
                "possible duplicate people, notes that look stale or contradictory. "
                "STRICT LIMITS: run only `lisan ingest` commands and read files; never modify, move, or "
                "delete anything in the source folder.\n\nFiles:\n" + file_list
            ),
        })
    steps.append({
        "kind": "prompt",
        "description": (
            f"You just finished ingesting {len(files)} notes from {folder} into your memory "
            "(batch results are in the context below). Tell the owner, conversationally and briefly: "
            "what body of knowledge you now hold from this folder, the recurring people/projects you "
            "noticed, and then list the QUESTIONS the batches surfaced that only the owner can answer, "
            "as a numbered list they can reply to one by one."
        ),
    })
    return create_plan(
        goal=f"Autonomously ingest {len(files)} notes from {folder.name}/ into memory, surfacing questions as I go",
        steps=steps,
        chat_id=chat_id,
        conversation_id=conversation_id,
        db_path=db_path,
    )
