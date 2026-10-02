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
the only moment anyone can say no. intent.md never-rules still outrank that
approval: a codex step is refused, and the plan ends honestly, if the
standing authority document forbids the work.

Every step also leaves a row in the shared run ledger (run_ledger.task_runs,
origin "plan"), and a finished plan reports through the same capture front
door as the Adjutant, so the Skeptic reads plan outcomes like any claim.
Steps may opt into retries; a failed plan can be resumed from the step that
failed.

A `fanout` step runs several delegated workers in parallel and joins them
(docs/delegation_workorder.md): the step launches its children and ends, the
plan waits, and when the last child finishes the delegation group enqueues the
plan's continuation, which collects the results and carries on.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable

from ..config import load_config
from ..paths import vault_root
from ..utils import utc_now_iso

STEP_KINDS = {"codex", "prompt", "note", "fanout"}
_JOIN_POLICIES = {"all", "best_effort"}
_MAX_STEPS = 12
_MAX_STEP_RETRIES = 2
_RESULT_PREVIEW = 2400


def create_plan(
    *,
    goal: str,
    steps: list[dict[str, str]],
    chat_id: int | None = None,
    conversation_id: str | None = None,
    working_directory: str | None = None,
    db_path: Path | None = None,
    config: dict[str, Any] | None = None,
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
    ceiling: str | None = None  # resolved only if a fanout step needs it
    for i, step in enumerate(steps, start=1):
        kind = str(step.get("kind") or "codex").strip().lower()
        description = str(step.get("description") or "").strip()
        if kind not in STEP_KINDS:
            raise ValueError(f"step {i}: unknown kind {kind!r}; expected one of {sorted(STEP_KINDS)}")
        if not description:
            raise ValueError(f"step {i}: empty description")
        try:
            retries = int(step.get("retries") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"step {i}: retries must be a number") from None
        if not 0 <= retries <= _MAX_STEP_RETRIES:
            raise ValueError(f"step {i}: retries must be between 0 and {_MAX_STEP_RETRIES}")
        entry: dict[str, Any] = {
            "kind": kind, "description": description, "status": "pending", "result": "",
            "retries": retries, "attempts": 0,
        }
        if kind == "fanout":
            from .delegation import caller_ceiling_mode, max_children, normalize_child_spec

            config = config if config is not None else load_config()
            ceiling = ceiling or caller_ceiling_mode(config)
            children = step.get("children")
            if not isinstance(children, list) or not children:
                raise ValueError(f"step {i}: a fanout step needs a non-empty list of children")
            if len(children) > max_children(config):
                raise ValueError(
                    f"step {i}: too many children ({len(children)}); at most {max_children(config)} "
                    "per fan-out (delegation.max_children)"
                )
            join = str(step.get("join") or "all")
            if join not in _JOIN_POLICIES:
                raise ValueError(f"step {i}: join must be one of {sorted(_JOIN_POLICIES)}")
            if retries:
                raise ValueError(f"step {i}: fanout steps cannot retry; children are never retried automatically")
            try:
                entry["children"] = [normalize_child_spec(c, ceiling=ceiling, config=config) for c in children]
            except (ValueError, AttributeError) as exc:
                raise ValueError(f"step {i}: {exc}") from None
            entry["join"] = join
        normalized.append(entry)

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
    if ceiling:
        payload["profile_ceiling"] = ceiling  # the most any fanout child may have

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
    capture: Callable[..., Any] | None = None,
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

    from .run_ledger import ORIGIN_PLAN, begin_run, finish_run

    step = steps[index]
    if step["kind"] == "fanout":
        if step.get("status") == "waiting":
            if payload.get("join_of_step") != index:
                # A duplicate run of a launch job whose children are still out:
                # the group will enqueue the real continuation. Nothing to do.
                return {"plan_id": payload["plan_id"], "status": "waiting", "step": index + 1}
            run_id = begin_run(db_path, f"{payload['plan_id']}#step{index + 1}", step["attempts"], origin=ORIGIN_PLAN)
            outcome_text, ok = _collect_fanout(step, db_path=db_path)
            finish_run(db_path, run_id, ok=ok, error=None if ok else outcome_text)
        else:
            step["attempts"] = int(step.get("attempts") or 0) + 1
            try:
                launched = _launch_fanout(
                    job, payload, steps, index, vault=vault, db_path=db_path, config=config
                )
            except ValueError as exc:  # refused (intent never-rule, outstanding cap): the step fails, honestly
                outcome_text, ok = f"could not start the children: {exc}", False
            else:
                return launched
    else:
        step["attempts"] = int(step.get("attempts") or 0) + 1
        run_id = begin_run(db_path, f"{payload['plan_id']}#step{index + 1}", step["attempts"], origin=ORIGIN_PLAN)
        outcome_text, ok = _execute_step(
            step, payload, vault=vault, db_path=db_path, provider=provider, model=model, config=config
        )
        finish_run(db_path, run_id, ok=ok, error=None if ok else outcome_text)
    step["result"] = outcome_text[:_RESULT_PREVIEW]
    steps[index] = step
    payload["steps"] = steps

    if not ok and step["attempts"] <= int(step.get("retries") or 0) and _retry_safe(outcome_text):
        # An opted-in retry: the same step goes back on the queue, the plan
        # does not end. The failed attempt stays visible in the step result.
        step["status"] = "pending"
        _persist_payload(job, payload, db_path=db_path)
        next_job = enqueue_job("plan.run", payload, db_path=db_path)
        return {
            "plan_id": payload["plan_id"], "status": "step_retry", "step": index + 1,
            "attempt": step["attempts"], "next_job": next_job, "result": outcome_text[:_RESULT_PREVIEW],
        }

    step["status"] = "done" if ok else "failed"
    if ok:
        payload["current_step"] = index + 1
    # Persist the post-step state onto this job row: the row that ran the
    # step must carry the truth about it, or plan progress becomes invisible
    # once the chain ends.
    _persist_payload(job, payload, db_path=db_path)

    if not ok:
        for later in steps[index + 1:]:
            later["status"] = "skipped"
        _persist_payload(job, payload, db_path=db_path)  # the row must say what the report says
        _finish_plan(payload, vault=vault, status="failed", send_fn=send_fn, config=config, capture=capture)
        return {"plan_id": payload["plan_id"], "status": "failed", "failed_step": index + 1, "result": outcome_text[:_RESULT_PREVIEW]}

    if payload["current_step"] >= len(steps):
        _finish_plan(payload, vault=vault, status="completed", send_fn=send_fn, config=config, capture=capture)
        return {"plan_id": payload["plan_id"], "status": "completed", "steps_done": len(steps)}

    next_job = enqueue_job("plan.run", payload, db_path=db_path)
    return {
        "plan_id": payload["plan_id"],
        "status": "step_done",
        "step": index + 1,
        "next_job": next_job,
        "result": outcome_text[:_RESULT_PREVIEW],
    }


def _launch_fanout(
    job: dict[str, Any],
    payload: dict[str, Any],
    steps: list[dict[str, Any]],
    index: int,
    *,
    vault: Path,
    db_path: Path | None,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Start a fanout step's children and end this job. The plan now waits; the
    delegation group enqueues its continuation when the last child finishes.
    Raises ValueError if the children are refused (nothing is queued then)."""
    from .delegation import caller_ceiling_mode, child_job_ids_for, launch_group

    step = steps[index]
    plan_id = payload["plan_id"]
    group_id = f"grp.{plan_id}.s{index + 1}.a{step['attempts']}.r{int(payload.get('resume_count') or 0)}"
    step["status"] = "waiting"
    step["group_id"] = group_id
    step["child_job_ids"] = child_job_ids_for(group_id, len(step["children"]))
    payload["steps"] = steps
    config = config if config is not None else load_config()
    launch_group(
        step["children"],
        group_id=group_id,
        kind="plan",
        parent={"kind": "plan", "ref": plan_id, "step": index + 1},
        continuation_type="plan.run",
        # the plan as it stands, plus the marker that makes it a join
        continuation_payload={**payload, "join_of_step": index},
        ceiling=payload.get("profile_ceiling") or caller_ceiling_mode(config),
        config=config,
        vault=vault,
        db_path=db_path,
    )
    _persist_payload(job, payload, db_path=db_path)
    return {
        "plan_id": plan_id, "status": "waiting", "step": index + 1,
        "group_id": group_id, "children": len(step["children"]),
    }


def _collect_fanout(step: dict[str, Any], *, db_path: Path | None) -> tuple[str, bool]:
    """Read what the children did and decide the step's outcome per its join
    policy. `all`: every child must succeed. `best_effort`: at least one must,
    and failures are listed rather than fatal."""
    import json

    from .delegation import group_children

    children = group_children(str(step.get("group_id") or ""), db_path=db_path)
    if not children:
        return "the fanout's children could not be found", False
    succeeded = [c for c in children if c["status"] == "succeeded"]
    budget = max(200, (_RESULT_PREVIEW - 200) // len(children))
    lines = [f"{len(succeeded)} of {len(children)} workers succeeded."]
    for i, child in enumerate(children, start=1):
        brief = " ".join(str(child.get("brief") or "").split())[:70]
        if child["status"] == "succeeded":
            body = json.dumps(child["data"], ensure_ascii=True) if child.get("data") else child.get("text") or ""
            lines.append(f"[{i}] {brief} -> {body[:budget]}")
        else:
            why = " ".join(str(child.get("error") or child["status"]).split())
            lines.append(f"[{i}] {brief} -> FAILED ({child['status']}): {why[:budget]}")
    ok = len(succeeded) == len(children) if step.get("join", "all") == "all" else bool(succeeded)
    return "\n".join(lines), ok


def _retry_safe(outcome_text: str) -> bool:
    """A timed-out step may have done part of its work; running it again could
    do that part twice. Ambiguous failures are never retried automatically —
    the owner sees the failure and decides (see resume_plan)."""
    return "timed out" not in outcome_text.lower()


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
        from .execution_tools import _build_codex_prompt, _chat_intent_verdict

        from .execution_tools import codex_workspace

        # The approval above never outranks intent.md's never-rules — same
        # rail run_codex honours for a direct chat command.
        ruling = _chat_intent_verdict(vault)
        if ruling is not None and ruling[0].decision == "deny":
            verdict, version = ruling
            reasons = "; ".join(verdict.reasons or ["denied"])
            return f"refused: intent.md (v{version}) forbids this — {verdict.rule}: {reasons}", False

        wd = Path(str(payload.get("working_directory") or codex_workspace())).expanduser()
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
            conversation_id=f"plan-{payload['plan_id']}",
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
    capture: Callable[..., Any] | None = None,
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
    _report_to_memory(payload, vault=vault, status=status, capture=capture)


def _report_to_memory(
    payload: dict[str, Any],
    *,
    vault: Path,
    status: str,
    capture: Callable[..., Any] | None = None,
    db_path: Path | None = None,
) -> None:
    """Hand the plan's outcome to the capture front door, the same way the
    Adjutant reports a task, so the Skeptic reads it like any other claim.
    Best-effort: the report file and job result are the durable record."""
    try:
        from .adjutant_executor import ExecutionResult
        from .adjutant_reporter import report_result

        steps = payload.get("steps") or []
        failed = [s for s in steps if s.get("status") == "failed"]
        result = ExecutionResult(
            task_id=str(payload["plan_id"]),
            kind="plan",
            ok=status == "completed",
            actions=[f"{s['kind']}: {s['description'][:160]} -> {s['status']}" for s in steps],
            artifacts=[str(payload["report_path"])] if payload.get("report_path") else [],
            errors=[f"step failed: {s['description'][:120]} — {str(s.get('result') or '')[:300]}" for s in failed],
        )
        report_result(
            vault, result, verdict_path=f"plan approved at creation: {payload.get('goal', '')[:120]}",
            db_path=db_path, capture=capture,
        )
    except Exception:
        pass


def _summary_message(payload: dict[str, Any], *, status: str) -> str:
    steps = payload.get("steps") or []
    done = sum(1 for s in steps if s.get("status") == "done")
    icon = "✅" if status == "completed" else "⚠️"
    header = f"{icon} Plan {status}: {payload['goal']}"

    # When a completed plan closes with a prompt step, that step's output IS
    # the report the owner should read — a conversational summary written for
    # them — not a mechanical step checklist.
    if status == "completed" and steps and steps[-1].get("kind") == "prompt":
        closing = str(steps[-1].get("result") or "").strip()
        if closing:
            return f"{header}\n\n{closing}"

    lines = [header, f"{done}/{len(steps)} steps done."]
    for i, s in enumerate(steps, start=1):
        mark = {"done": "✓", "failed": "✗", "skipped": "–", "pending": "…"}.get(s.get("status"), "?")
        lines.append(f"{mark} {i}. {s['description'][:120]}")
        if s.get("status") == "failed" and s.get("result"):
            lines.append(f"   failure: {s['result'][:200]}")
    # A finished plan's answer is its last step's result; a checklist alone
    # tells the owner it ran, not what it found.
    if status == "completed" and steps and steps[-1].get("status") == "done":
        closing = str(steps[-1].get("result") or "").strip()
        if closing and steps[-1].get("kind") != "note":
            lines.append("Result: " + (closing if len(closing) <= 700 else closing[:700] + "…"))
    return "\n".join(lines)


def _write_plan_report(payload: dict[str, Any], *, vault: Path, status: str) -> Path:
    reports = vault / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{payload['plan_id']}.md"
    today = utc_now_iso()[:10]
    frontmatter = {
        "id": f"report.{payload['plan_id']}",
        "type": "report",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": "low",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "private",
        "summary": f"Plan report: {payload['goal']}"[:200],
        "links": [],
        "confidence": "low",
        "confidence_basis": "Generated plan execution report",
        "last_confirmed": today,
        "review_after": today,
    }
    lines = [
        "---",
        json.dumps(frontmatter, indent=2, ensure_ascii=True),
        "---",
        "",
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
        waiting = _is_waiting_on_children(job, steps, db_path=db_path)
        plans.append({
            "plan_id": plan_id,
            "goal": payload.get("goal") or "",
            "job_status": job.get("status"),
            "steps_total": len(steps),
            "steps_done": sum(1 for s in steps if s.get("status") == "done"),
            "waiting": waiting,
            # a plan blocked on its workers is working, even though no job of
            # its own is queued or running
            "active": waiting or job.get("status") in {"queued", "running", "retry_wait"},
            "created_at": payload.get("created_at"),
            "job_id": job.get("id"),
            "result": job.get("result"),
        })
    plans.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
    return plans[:limit]


def _is_waiting_on_children(job: dict[str, Any], steps: list[dict[str, Any]], *, db_path: Path | None) -> bool:
    """True when the plan's newest job is a finished fanout launch whose group
    is still waiting. Once the continuation is queued, running, or canceled it
    is the newest job instead, so this goes false on its own."""
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    index = int(payload.get("current_step") or 0)
    if job.get("status") != "succeeded" or index >= len(steps) or steps[index].get("status") != "waiting":
        return False
    try:
        from .delegation import get_group

        group = get_group(str(steps[index].get("group_id") or ""), db_path=db_path)
    except Exception:
        return False
    return bool(group) and group["state"] == "waiting"


def _plan_progress_key(job: dict[str, Any]) -> tuple[int, int, int, int, str]:
    """Orders the job rows of one plan, newest state last. A resumed chain
    outranks the failed chain it came from (resume_count), then step
    progress, then retries of the current step, then a fanout's join after
    its launch; timestamps only break what is left, since they tie within a
    second."""
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    steps = payload.get("steps") or []
    index = int(payload.get("current_step") or 0)
    attempts = int(steps[index].get("attempts") or 0) if index < len(steps) and isinstance(steps[index], dict) else 0
    joined = 1 if payload.get("join_of_step") is not None else 0
    return (int(payload.get("resume_count") or 0), index, attempts, joined, str(job.get("created_at") or ""))


def active_plans(*, db_path: Path | None = None) -> list[dict[str, Any]]:
    return [p for p in list_plans(db_path=db_path) if p["active"]]


def format_plans(plans: list[dict[str, Any]]) -> str:
    if not plans:
        return "No plans."
    lines = []
    for p in plans:
        state = "waiting on workers" if p.get("waiting") else ("active" if p["active"] else str(p["job_status"]))
        goal = p["goal"] if len(p["goal"]) <= 70 else p["goal"][:67] + "..."
        lines.append(f"{p['plan_id']}  [{state}]  {p['steps_done']}/{p['steps_total']} steps  — {goal}")
    return "\n".join(lines)


def cancel_plan(plan_id: str, *, db_path: Path | None = None, vault: Path | None = None) -> bool:
    """Cancel the pending job row that carries this plan forward, or — if the
    plan is waiting on delegated workers — cancel them (killing running ones)
    and end the plan as canceled."""
    from .jobs import cancel_job, list_jobs

    for job in list_jobs(limit=5000, db_path=db_path):
        if job.get("job_type") != "plan.run":
            continue
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        if str(payload.get("plan_id")) == plan_id and job.get("status") in {"queued", "retry_wait"}:
            cancel_job(str(job.get("id")), db_path=db_path)
            return True
    return _cancel_waiting_plan(plan_id, db_path=db_path, vault=vault)


def _cancel_waiting_plan(plan_id: str, *, db_path: Path | None, vault: Path | None) -> bool:
    from .delegation import cancel_group
    from .jobs import get_job

    plan = next((p for p in list_plans(db_path=db_path) if p["plan_id"] == plan_id and p.get("waiting")), None)
    if plan is None:
        return False
    job = get_job(str(plan["job_id"]), db_path=db_path) or {}
    payload = dict(job.get("payload") or {})
    steps = [dict(s) for s in payload.get("steps") or []]
    index = int(payload.get("current_step") or 0)
    stopped = cancel_group(str(steps[index].get("group_id") or ""), db_path=db_path)
    steps[index]["status"] = "failed"
    steps[index]["result"] = f"canceled by the owner while waiting on workers ({stopped} stopped)"
    for later in steps[index + 1:]:
        later["status"] = "skipped"
    payload["steps"] = steps
    _persist_payload(job, payload, db_path=db_path)
    _finish_plan(payload, vault=vault or vault_root(), status="canceled", send_fn=None, config=None)
    return True
def resume_plan(plan_id: str, *, db_path: Path | None = None) -> dict[str, Any]:
    """Restart a failed plan from the step that failed.

    Completed steps keep their results (later steps still see them); the
    failed step and everything skipped after it go back to pending with a
    fresh attempt count. Refuses a plan that is still running, finished, or
    has nothing failed — resuming must never double-run live work.
    """
    from .jobs import enqueue_job

    plan = next((p for p in list_plans(db_path=db_path) if p["plan_id"] == plan_id), None)
    if plan is None:
        raise ValueError(f"no plan {plan_id}")
    if plan["active"]:
        raise ValueError(f"plan {plan_id} is still active; cancel it first if you want to restart it")
    from .jobs import get_job

    job = get_job(str(plan["job_id"]), db_path=db_path) or {}
    payload = dict(job.get("payload") or {})
    steps = [dict(s) for s in payload.get("steps") or []]
    failed = next((i for i, s in enumerate(steps) if s.get("status") == "failed"), None)
    if failed is None:
        raise ValueError(f"plan {plan_id} has no failed step to resume from")
    for step in steps[failed:]:
        step["status"] = "pending"
        step["result"] = ""
        step["attempts"] = 0
        step.pop("group_id", None)  # a fanout re-runs as a fresh group
        step.pop("child_job_ids", None)
    payload["steps"] = steps
    payload["current_step"] = failed
    payload["resumed_at"] = utc_now_iso()
    payload["resume_count"] = int(payload.get("resume_count") or 0) + 1
    payload.pop("report_path", None)
    job_id = enqueue_job("plan.run", payload, db_path=db_path)
    return {"plan_id": plan_id, "job_id": job_id, "resumed_from_step": failed + 1}


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
