"""The Adjutant gate: (task, intent) -> verdict, audited. Pure code.

Wraps the intent delegation resolver with the deterministic
task_kind -> required-capabilities mapping, the misfiled-task check, and
the audit log. No LLM is consulted, ever: authority questions are
answered by the owner's intent.md and nothing else.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .adjutant_common import TASK_KINDS
from .intent import DENY, Intent, Verdict, resolve_capabilities

# What each task kind needs permission for. Deterministic and fixed —
# the task record chooses a kind; the kind decides the capabilities;
# intent.md decides whether the arena may have them.
TASK_KIND_CAPABILITIES: dict[str, list[str]] = {
    "run_script": ["run_local_scripts", "read_files", "write_files"],
    "research": ["web_research", "write_files"],
    "collect": ["read_files", "write_files"],
    "draft": ["read_files", "write_files"],
    "notify": ["send_outbound_message"],
}


def required_capabilities(task_kinds: list[str]) -> list[str]:
    caps: list[str] = []
    for kind in task_kinds:
        for cap in TASK_KIND_CAPABILITIES.get(kind, []):
            if cap not in caps:
                caps.append(cap)
    return caps


def gate(task: dict[str, Any], intent: Intent) -> Verdict:
    """Decide one task. ``task`` is a poller item: needs ``arena``,
    ``task_kinds`` (one kind for loops/schedules, possibly several for a
    decision's pending steps), and ``blocked_contexts``."""
    arena = str(task.get("arena") or "")
    kinds = [k for k in task.get("task_kinds", []) if k]
    unknown = [k for k in kinds if k not in TASK_KINDS]
    if unknown:
        return Verdict(DENY, "unknown_task_kind", [f"unknown task_kind(s): {', '.join(unknown)}"])
    if not kinds:
        return Verdict(DENY, "no_task_kind", ["task carries no task_kind"])

    # Misfiled-task check: a task whose own compartment rules would block
    # retrieval of its arena's context can never be executed coherently.
    blocked = task.get("blocked_contexts") or []
    if arena and arena in blocked:
        return Verdict(
            DENY,
            "misfiled_task",
            [f"arena {arena!r} is in the task's own blocked_contexts; flagged for review"],
        )

    return resolve_capabilities(intent.delegations, arena, required_capabilities(kinds))


def log_verdict(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    arena: str,
    capabilities: list[str],
    verdict: Verdict,
    intent_version: int,
    note: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO adjutant_log (ts, task_id, arena, capabilities, verdict, matched_rule, intent_version, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            task_id,
            arena,
            json.dumps(capabilities),
            verdict.decision,
            verdict.rule,
            intent_version,
            note or ("; ".join(verdict.reasons) if verdict.reasons else None),
        ),
    )


def log_cycle_event(conn: sqlite3.Connection, event: str, note: str) -> None:
    """Cycle-level entries (halt, cycle summary) share the audit log so
    `adjutant status` reads one place. task_id 'cycle' is reserved."""
    conn.execute(
        "INSERT INTO adjutant_log (ts, task_id, verdict, note) VALUES (?, 'cycle', ?, ?)",
        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), event, note),
    )
