"""The Adjutant cycle: poll -> gate -> execute -> report.

Dry-run (config adjutant.enabled=false, the default) logs verdicts and
touches nothing. Enabled, the cycle executes only what the gate says
EXECUTE (or what the owner explicitly approved), through the local
executor, and reports every outcome — success or failure — through the
capture front door. The executor never writes memory; the reporter only
speaks through capture.

A halt is never silent (ratified 2026-07-23): any refusal to run lands
in the audit log with its reason and shows in `lisan adjutant status`.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..config import load_config
from ..frontmatter import load_markdown, write_markdown
from ..paths import sqlite_path, vault_root
from .adjutant_confirmations import (
    create_confirmation_for_task,
    expire_stale_confirmations,
    mark_executed,
)
from .adjutant_executor import (
    ExecutionResult,
    execute_task,
    set_task_status,
    task_payload_from_record,
)
from .adjutant_gate import gate, log_cycle_event, log_verdict, required_capabilities
from .adjutant_poller import PolledTask, poll
from .adjutant_reporter import report_result
from .db import connect as _db_connect
from .intent import (
    CONFIRM,
    DENY,
    EXECUTE,
    Intent,
    IntentError,
    detect_out_of_band_edit,
    has_sentinel_dates,
    load_intent,
)
from .rebuild_index import ensure_index_schema, reindex_record

MAX_ATTEMPTS = 2  # a task that fails twice moves to blocked — no infinite retries


def run_cycle(
    vault: Path | None = None,
    db_path: Path | None = None,
    *,
    config: dict[str, Any] | None = None,
    complete: Callable[[str], str] | None = None,
    capture: Callable[..., Any] | None = None,
    deliver: Callable[[str], None] | None = None,
    scratch_root: Path | None = None,
) -> dict[str, Any]:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    config = config or load_config()
    enabled = bool((config.get("adjutant") or {}).get("enabled", False))
    dry_run = not enabled

    conn = _db_connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_index_schema(conn)
        if detect_out_of_band_edit(vault):
            log_cycle_event(conn, "intent_oob_edit", "intent.md edited out of band; snapshotted and version bumped")
        if deliver is None:
            deliver = _default_deliver(config)
        try:
            intent = load_intent(vault)
        except IntentError as exc:
            # Fail closed, loudly: the halt and its reason are the cycle's
            # only product.
            _log_halt(conn, str(exc), deliver)
            conn.commit()
            return {"halted": True, "reason": str(exc), "verdicts": [], "executed": [], "dry_run": dry_run}
        conn.commit()

        if enabled and has_sentinel_dates(intent):
            # Uncustomized authority is no authority: the template's
            # sentinel dates mean nobody has adopted this document yet.
            # Dry-run may proceed (it acts on nothing); execution may not.
            reason = (
                "intent.md still carries the template's sentinel dates (1970-01-01) in "
                "created/updated/review_after; customize and set real dates before enabling"
            )
            _log_halt(conn, reason, deliver)
            conn.commit()
            return {"halted": True, "reason": reason, "verdicts": [], "executed": [], "dry_run": dry_run}

        expired = expire_stale_confirmations(vault, db_path)
        if expired:
            log_cycle_event(conn, "confirmations_expired", ", ".join(e["id"] for e in expired))
            _escalate_repeat_expiries(conn, expired, deliver)

        tasks = poll(conn, intent, vault)
        wall_seconds = int((intent.delegations.get("global", {}) or {}).get("max_task_wall_seconds", 600) or 600)
        verdicts: list[dict[str, Any]] = []
        executed: list[dict[str, Any]] = []
        for task in tasks:
            verdict = gate(task.as_gate_task(), intent)
            capabilities = required_capabilities(task.task_kinds)
            log_verdict(
                conn,
                task_id=task.task_id,
                arena=task.arena,
                capabilities=capabilities,
                verdict=verdict,
                intent_version=intent.version,
                note=f"source={task.source}" + (f"; {'; '.join(verdict.reasons)}" if verdict.reasons else ""),
            )
            verdicts.append(
                {
                    "task_id": task.task_id,
                    "source": task.source,
                    "arena": task.arena,
                    "task_kinds": task.task_kinds,
                    "capabilities": capabilities,
                    "verdict": verdict.decision,
                    "rule": verdict.rule,
                    "reasons": verdict.reasons,
                }
            )
            # Release the write lock before any helper opens its own
            # connection (confirmations, reindex) — an uncommitted INSERT
            # here would stall them into quiet lock timeouts.
            conn.commit()
            if dry_run:
                continue

            verdict_path = f"{verdict.rule} (intent v{intent.version})"
            if task.source == "confirmation":
                # Owner approval satisfies CONFIRM — but never-rules still
                # outrank a stale approval.
                if verdict.decision == DENY:
                    log_cycle_event(
                        conn, "approval_overridden", f"{task.task_id}: approved but now denied by {verdict.rule}"
                    )
                    continue
                outcome = _execute_and_report(
                    conn, vault, task, intent, config,
                    verdict_path=f"owner approval + {verdict_path}",
                    wall_seconds=wall_seconds, db_path=db_path,
                    complete=complete, capture=capture, deliver=deliver, scratch_root=scratch_root,
                )
                if outcome["ok"]:
                    mark_executed(vault, task.task_id, db_path)
                executed.append(outcome)
            elif verdict.decision == EXECUTE:
                executed.append(
                    _execute_and_report(
                        conn, vault, task, intent, config,
                        verdict_path=verdict_path, wall_seconds=wall_seconds, db_path=db_path,
                        complete=complete, capture=capture, deliver=deliver, scratch_root=scratch_root,
                    )
                )
            elif verdict.decision == CONFIRM:
                created = create_confirmation_for_task(
                    vault,
                    task_id=task.task_id,
                    task_summary=task.summary or task.task_id,
                    planned_action=_planned_action(vault, task),
                    risk="; ".join(verdict.reasons) or f"requires confirmation per {verdict.rule}",
                    arena=task.arena,
                    db_path=db_path,
                )
                if created:
                    log_cycle_event(conn, "confirmation_created", f"{created} for {task.task_id}")
                    _ping_owner(
                        deliver,
                        f"Adjutant: confirmation pending — {created}\n"
                        f"Task: {task.summary or task.task_id}\n"
                        f"Will do: {_planned_action(vault, task)}\n"
                        f"Reply: approve {created}  /  deny {created}",
                    )

        log_cycle_event(
            conn,
            "cycle",
            f"dry_run={dry_run} tasks={len(tasks)} executed={len(executed)} "
            + " ".join(f"{v['task_id']}:{v['verdict']}" for v in verdicts),
        )
        conn.commit()
        return {
            "halted": False,
            "intent_version": intent.version,
            "dry_run": dry_run,
            "verdicts": verdicts,
            "executed": executed,
        }
    finally:
        conn.close()


def _planned_action(vault: Path, task: PolledTask) -> str:
    payload = task_payload_from_record(vault, task.path) if task.path else {}
    if "notify" in task.task_kinds:
        # Spec §5 / Never #1: the human approves the actual message.
        message = str(payload.get("message", "")).strip() or "(payload carries no message — the task will fail)"
        return f"Send this exact message to the owner via telegram:\n\n{message}"
    return f"kind={'+'.join(task.task_kinds)} payload={payload!r} (task: {task.summary})"


def _log_halt(conn: sqlite3.Connection, reason: str, deliver: Callable[[str], None] | None) -> None:
    """A halt is never silent: log it always, and ping the owner on the
    EDGE — the first halt after a non-halt — so a daemon halting every
    interval sends one message, not one per cycle."""
    previous = conn.execute(
        "SELECT verdict, note FROM adjutant_log WHERE task_id='cycle' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    fresh = previous is None or str(previous["verdict"]) != "halt" or str(previous["note"]) != reason
    log_cycle_event(conn, "halt", reason)
    if fresh:
        _ping_owner(
            deliver,
            f"Adjutant halted: {reason}\n"
            f"(since {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}; "
            "see `lisan adjutant status`. This message will not repeat for the same reason.)",
        )


def _ping_owner(deliver: Callable[[str], None] | None, text: str) -> None:
    """Owner pings are best-effort: an unconfigured or failing channel must
    never break the cycle; the log and batch review remain the floor."""
    if deliver is None:
        return
    try:
        deliver(text)
    except Exception:
        pass


def _escalate_repeat_expiries(conn: sqlite3.Connection, expired: list[dict[str, Any]], deliver) -> None:
    """Ratified 2026-07-23: the same task expiring twice is a decision the
    owner keeps not making. Ping exactly at the second expiry."""
    for item in expired:
        row = conn.execute(
            "SELECT COUNT(*) FROM confirmations WHERE task_id = ? AND resolution = 'expired'",
            (str(item["task_id"]),),
        ).fetchone()
        if int(row[0]) == 2:
            _ping_owner(
                deliver,
                f"Adjutant: task {item['task_id']} has now had 2 confirmations expire unanswered — "
                "this decision keeps not getting made. It is parked in batch review.",
            )


def _default_deliver(config: dict[str, Any]):
    """Owner delivery via the existing Telegram path (settled fork: no
    parallel adapter). None when unconfigured — callers degrade cleanly."""
    try:
        from .telegram_bot import _resolve_settings

        token, allowed = _resolve_settings(config)
        if not token or not allowed:
            return None
        from .scheduler import _deliver_owner_message

        def _send(text: str) -> None:
            _deliver_owner_message(text, config=config)

        return _send
    except Exception:
        return None


def _next_attempt(conn: sqlite3.Connection, task_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()
    return int(row[0]) + 1


def _execute_and_report(
    conn: sqlite3.Connection,
    vault: Path,
    task: PolledTask,
    intent: Intent,
    config: dict[str, Any],
    *,
    verdict_path: str,
    wall_seconds: int,
    db_path: Path,
    complete: Callable[[str], str] | None,
    capture: Callable[..., Any] | None,
    deliver: Callable[[str], None] | None,
    scratch_root: Path | None,
) -> dict[str, Any]:
    attempt = _next_attempt(conn, task.task_id)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cursor = conn.execute(
        "INSERT INTO task_runs (task_id, attempt, started) VALUES (?, ?, ?)",
        (task.task_id, attempt, started),
    )
    run_row = cursor.lastrowid
    conn.commit()

    if task.source in {"open_loop", "confirmation"}:
        _safe_set_task_status(vault, task.path, "running", db_path)

    if complete is None:
        complete = _default_complete(config, db_path)

    if task.source == "decision":
        results = _execute_decision_steps(
            vault, task, config, wall_seconds=wall_seconds, complete=complete, deliver=deliver,
            scratch_root=scratch_root, db_path=db_path
        )
    else:
        payload = task_payload_from_record(vault, task.path) if task.path else {}
        kind = task.task_kinds[0] if task.task_kinds else ""
        results = [
            execute_task(
                task.task_id, kind, payload,
                vault=vault, config=config, timeout_seconds=wall_seconds,
                complete=complete, deliver=deliver, scratch_root=scratch_root,
            )
        ]

    ok = all(r.ok for r in results) and bool(results)
    error_text = "; ".join(err for r in results for err in r.errors)[:500] or None
    conn.execute(
        "UPDATE task_runs SET finished = ?, exit_status = ?, error = ? WHERE id = ?",
        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok" if ok else "failed", error_text, run_row),
    )
    conn.commit()

    for result in results:
        try:
            report_result(vault, result, verdict_path=verdict_path, db_path=db_path, capture=capture)
        except Exception as exc:
            log_cycle_event(conn, "report_failed", f"{task.task_id}: {exc}")

    if task.source in {"open_loop", "confirmation"}:
        if ok:
            _safe_set_task_status(vault, task.path, "resolved", db_path)
        elif attempt >= MAX_ATTEMPTS:
            _safe_set_task_status(vault, task.path, "blocked", db_path)
            log_cycle_event(conn, "task_blocked", f"{task.task_id} failed {attempt} time(s); moved to blocked")
        else:
            _safe_set_task_status(vault, task.path, "pending", db_path)
    elif task.source == "schedule" and ok:
        _advance_schedule(conn, vault, task, db_path)

    return {
        "task_id": task.task_id,
        "attempt": attempt,
        "ok": ok,
        "kinds": task.task_kinds,
        "errors": [err for r in results for err in r.errors],
        "artifacts": [a for r in results for a in r.artifacts],
    }


def _execute_decision_steps(
    vault: Path,
    task: PolledTask,
    config: dict[str, Any],
    *,
    wall_seconds: int,
    complete: Callable[[str], str] | None,
    deliver: Callable[[str], None] | None,
    scratch_root: Path | None,
    db_path: Path,
) -> list[ExecutionResult]:
    """Execute a decision's pending steps in order; each successful step is
    marked resolved in the record (payloads come from the record)."""
    record = vault / task.path
    doc = load_markdown(record)
    fm = dict(doc.frontmatter)
    steps = fm.get("execution_steps") or []
    results: list[ExecutionResult] = []
    changed = False
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("status") != "pending":
            continue
        payload = step.get("task_payload") or {}
        result = execute_task(
            f"{task.task_id}#step{index}", str(step.get("task_kind", "")), payload if isinstance(payload, dict) else {},
            vault=vault, config=config, timeout_seconds=wall_seconds,
            complete=complete, deliver=deliver, scratch_root=scratch_root,
        )
        results.append(result)
        if result.ok:
            step["status"] = "resolved"
            changed = True
        else:
            break  # a failed step halts the sequence; retry picks it up
    if changed:
        fm["updated"] = date.today().isoformat()
        write_markdown(record, fm, doc.body)
        reindex_record(record, vault, db_path, quiet=True)
    return results


def _advance_schedule(conn: sqlite3.Connection, vault: Path, task: PolledTask, db_path: Path) -> None:
    record = vault / task.path
    doc = load_markdown(record)
    fm = dict(doc.frontmatter)
    cron = str(fm.get("cron", ""))
    try:
        from .adjutant_common import next_cron_stamp

        fm["next_run"] = next_cron_stamp(cron)
    except (ValueError, KeyError):
        # An unparseable cadence parks rather than refiring every cycle —
        # and says so. The validator should have caught it upstream.
        fm["next_run"] = ""
        log_cycle_event(conn, "schedule_parked", f"{task.task_id}: cadence {cron!r} is not parseable")
    fm["updated"] = date.today().isoformat()
    write_markdown(record, fm, doc.body)
    reindex_record(record, vault, db_path, quiet=True)


def _safe_set_task_status(vault: Path, path: str, status: str, db_path: Path) -> None:
    try:
        set_task_status(vault, path, status, db_path)
    except Exception:
        pass


def _default_complete(config: dict[str, Any], db_path: Path) -> Callable[[str], str] | None:
    """A thin provider callable for draft tasks; None when unavailable so
    the executor fails cleanly instead of hanging on a dead endpoint."""
    try:
        from ..providers.base import LisanLLM

        llm = LisanLLM(config, db_path)

        def _call(prompt: str) -> str:
            response = llm.complete(prompt, agent="adjutant", significance="medium")
            return str(getattr(response, "text", response))

        return _call
    except Exception:
        return None


def adjutant_status(vault: Path | None = None, db_path: Path | None = None) -> dict[str, Any]:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    from .intent import validate_intent_file

    intent_issues = validate_intent_file(vault)
    conn = _db_connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_index_schema(conn)
        last_cycle = conn.execute(
            "SELECT ts, note FROM adjutant_log WHERE task_id='cycle' AND verdict='cycle' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_halt = conn.execute(
            "SELECT ts, note FROM adjutant_log WHERE task_id='cycle' AND verdict='halt' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        halted_since = None
        if last_halt is not None and (last_cycle is None or str(last_halt["ts"]) > str(last_cycle["ts"])):
            halted_since = {"ts": str(last_halt["ts"]), "reason": str(last_halt["note"])}
        pending = conn.execute(
            "SELECT COUNT(*) FROM confirmations WHERE status='pending' AND resolution IS NULL"
        ).fetchone()[0]
        blocked = conn.execute(
            "SELECT COUNT(*) FROM files WHERE task_status='blocked'"
        ).fetchone()[0]
        return {
            "intent_valid": not intent_issues,
            "intent_issues": intent_issues,
            "last_cycle": dict(last_cycle) if last_cycle else None,
            "halted": halted_since,
            "pending_confirmations": int(pending),
            "blocked_tasks": int(blocked),
        }
    finally:
        conn.close()


def format_status(status: dict[str, Any]) -> str:
    lines = []
    if status["halted"]:
        lines.append(f"HALTED since {status['halted']['ts']}: {status['halted']['reason']}")
    if not status["intent_valid"]:
        lines.append(f"intent.md INVALID ({len(status['intent_issues'])} issue(s)); the Adjutant will not run:")
        lines.extend(f"  - {issue}" for issue in status["intent_issues"])
    else:
        lines.append("intent.md valid")
    if status["last_cycle"]:
        lines.append(f"last cycle {status['last_cycle']['ts']}: {status['last_cycle']['note']}")
    else:
        lines.append("no cycles recorded")
    lines.append(f"pending confirmations: {status['pending_confirmations']}")
    lines.append(f"blocked tasks: {status['blocked_tasks']}")
    return "\n".join(lines)


def tail_log(db_path: Path | None = None, limit: int = 20) -> list[dict[str, Any]]:
    conn = _db_connect(db_path or sqlite_path())
    conn.row_factory = sqlite3.Row
    try:
        ensure_index_schema(conn)
        rows = conn.execute(
            "SELECT ts, task_id, arena, capabilities, verdict, matched_rule, intent_version, note "
            "FROM adjutant_log ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]
    finally:
        conn.close()


def format_log(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "adjutant_log is empty."
    lines = []
    for row in rows:
        if row["task_id"] == "cycle":
            lines.append(f"{row['ts']}  [{row['verdict']}] {row['note']}")
        else:
            lines.append(
                f"{row['ts']}  {row['verdict'].upper():<12} {row['task_id']}"
                f"  arena={row['arena'] or '-'}  rule={row['matched_rule'] or '-'}"
                f"  intent_v{row['intent_version']}"
                + (f"  ({row['note']})" if row["note"] else "")
            )
    return "\n".join(lines)


def format_cycle_result(result: dict[str, Any]) -> str:
    if result["halted"]:
        return f"HALTED: {result['reason']}"
    lines = [
        f"Cycle complete (dry-run={str(result['dry_run']).lower()}, intent v{result['intent_version']}): "
        f"{len(result['verdicts'])} task(s), {len(result.get('executed', []))} executed."
    ]
    for v in result["verdicts"]:
        lines.append(
            f"  {v['verdict'].upper():<12} {v['task_id']} [{v['source']}] arena={v['arena'] or '-'} "
            f"kinds={','.join(v['task_kinds'])} rule={v['rule']}"
        )
        for reason in v["reasons"]:
            lines.append(f"      - {reason}")
    for outcome in result.get("executed", []):
        status = "ok" if outcome["ok"] else "FAILED"
        lines.append(f"  ran {outcome['task_id']} attempt {outcome['attempt']}: {status}")
        for error in outcome["errors"]:
            lines.append(f"      ! {error}")
    if not result["verdicts"]:
        lines.append("  nothing actionable.")
    return "\n".join(lines)
