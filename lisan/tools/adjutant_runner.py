"""The Adjutant cycle: poll -> gate -> (execute) -> report.

Step 3 of WO-ADJUTANT ships the cycle in dry-run: every verdict is
logged to adjutant_log, nothing acts. Execution arrives in step 4 and
stays behind config adjutant.enabled AND the per-arena authority in
intent.md — two switches, both owner-held.

A halt is never silent (ratified 2026-07-23): any refusal to run lands
in the audit log with its reason and shows in `lisan adjutant status`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..config import load_config
from ..paths import sqlite_path, vault_root
from .adjutant_gate import gate, log_cycle_event, log_verdict, required_capabilities
from .adjutant_poller import poll
from .db import connect as _db_connect
from .intent import IntentError, detect_out_of_band_edit, load_intent
from .rebuild_index import ensure_index_schema


def run_cycle(
    vault: Path | None = None,
    db_path: Path | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    config = config or load_config()
    enabled = bool((config.get("adjutant") or {}).get("enabled", False))

    conn = _db_connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_index_schema(conn)
        if detect_out_of_band_edit(vault):
            log_cycle_event(conn, "intent_oob_edit", "intent.md edited out of band; snapshotted and version bumped")
        try:
            intent = load_intent(vault)
        except IntentError as exc:
            # Fail closed, loudly: the halt and its reason are the cycle's
            # only product.
            log_cycle_event(conn, "halt", str(exc))
            conn.commit()
            return {"halted": True, "reason": str(exc), "verdicts": [], "dry_run": True}

        tasks = poll(conn, intent, vault)
        verdicts: list[dict[str, Any]] = []
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

        # Step 3: the cycle is verdicts-only regardless of the flag; the
        # executor lands in step 4, gated on `enabled` (unused until then).
        del enabled
        dry_run = True
        log_cycle_event(
            conn,
            "cycle",
            f"dry_run={dry_run} tasks={len(tasks)} " + " ".join(f"{v['task_id']}:{v['verdict']}" for v in verdicts),
        )
        conn.commit()
        return {
            "halted": False,
            "intent_version": intent.version,
            "dry_run": dry_run,
            "verdicts": verdicts,
        }
    finally:
        conn.close()


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
        f"{len(result['verdicts'])} task(s)."
    ]
    for v in result["verdicts"]:
        lines.append(
            f"  {v['verdict'].upper():<12} {v['task_id']} [{v['source']}] arena={v['arena'] or '-'} "
            f"kinds={','.join(v['task_kinds'])} rule={v['rule']}"
        )
        for reason in v["reasons"]:
            lines.append(f"      - {reason}")
    if not result["verdicts"]:
        lines.append("  nothing actionable.")
    return "\n".join(lines)
