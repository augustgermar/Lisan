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
from .scope import normalize_scope

# What each task kind needs permission for. Deterministic and fixed —
# the task record chooses a kind; the kind decides the capabilities;
# intent.md decides whether the scope may have them.
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
    """Decide one task. ``task`` is a poller item: needs ``scope``,
    ``task_kinds`` (one kind for loops/schedules, possibly several for a
    decision's pending steps), and ``blocked_contexts``.

    ``scope`` is the delegation axis (:mod:`lisan.tools.scope`). It is
    routinely empty — the capture pipeline never assigns one — and an empty
    scope resolves to REPORT_ONLY with the rule ``no_scope`` so the audit
    log distinguishes "the owner delegated nothing here" from "the owner
    delegated something that does not permit this".
    """
    scope = normalize_scope(task.get("scope"))
    kinds = [k for k in task.get("task_kinds", []) if k]
    unknown = [k for k in kinds if k not in TASK_KINDS]
    if unknown:
        return Verdict(DENY, "unknown_task_kind", [f"unknown task_kind(s): {', '.join(unknown)}"])
    if not kinds:
        return Verdict(DENY, "no_task_kind", ["task carries no task_kind"])

    # Misfiled-task check: a task whose own compartment rules would block
    # retrieval of its scope's context can never be executed coherently.
    blocked = {normalize_scope(item) for item in (task.get("blocked_contexts") or [])}
    if scope and scope in blocked:
        return Verdict(
            DENY,
            "misfiled_task",
            [f"scope {scope!r} is in the task's own blocked_contexts; flagged for review"],
        )

    return resolve_capabilities(intent.delegations, scope, required_capabilities(kinds))


def log_verdict(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    scope: str,
    capabilities: list[str],
    verdict: Verdict,
    intent_version: int,
    note: str | None = None,
) -> None:
    """Audit one verdict. Writes the delegation axis to ``scope``; the older
    ``arena`` column is left NULL on new rows and keeps its historical values
    on rows written before 2026-07-29 (readers COALESCE the two)."""
    conn.execute(
        """
        INSERT INTO adjutant_log (ts, task_id, scope, capabilities, verdict, matched_rule, intent_version, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            task_id,
            scope,
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
