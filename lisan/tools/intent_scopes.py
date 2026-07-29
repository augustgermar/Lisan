"""`lisan intent scopes` — does the authority document cover reality?

The instrument that would have caught WO-ADJUTANT's central defect in one
command. For a month, ``primer/intent.md`` declared eight areas of
responsibility while records carried none of them, and nothing anywhere
said so: the gate always resolved to *something*, the cycle log filled
with plausible verdicts, and the calibration soak collected six days of
data through a gate that had exactly one reachable answer.

This is the project's "instruments before rules" principle applied to
authority. A rule that cannot match is worse than a missing rule, because
it reads as considered. So: print what the owner declared, print what the
records actually carry, and name the gap in both directions —

- **declared but unused**: a rule written with care that no record can
  reach. The eight-for-eight case. Usually a naming mismatch.
- **used but undeclared**: records asking for authority the document never
  granted. These fall to ``defaults`` — safe, but silently inert.

Deterministic: one SQL query and a set difference. No LLM, consistent with
every other authority path.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .db import connect as db_connect
from .intent import Intent
from .scope import declared_scopes, normalize_scope, scopes_key_in_use

# Record types the Adjutant poller can ever select. Scope coverage is only
# meaningful for these — an episode carrying a scope is harmless noise, and
# counting it would inflate the "used" side with records that can never
# become tasks.
POLLABLE_TYPES = ("open_loop", "decision", "schedule", "confirmation")


def scope_coverage(intent: Intent, db_path: Path) -> dict[str, object]:
    """Declared scopes, scopes present on pollable records, and the gaps."""
    declared = set(declared_scopes(intent.delegations))
    counts: dict[str, int] = {}
    unscoped = 0
    placeholders = ",".join("?" for _ in POLLABLE_TYPES)
    try:
        conn = db_connect(db_path)
    except sqlite3.Error:
        return {
            "declared": sorted(declared),
            "present": {},
            "unscoped_pollable": 0,
            "declared_unused": sorted(declared),
            "present_undeclared": [],
            "key": scopes_key_in_use(intent.delegations),
            "database_error": f"could not open {db_path}",
        }
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT scope, COUNT(*) AS n FROM files WHERE type IN ({placeholders}) GROUP BY scope",
            POLLABLE_TYPES,
        ).fetchall()
        for row in rows:
            name = normalize_scope(row["scope"])
            if name:
                counts[name] = counts.get(name, 0) + int(row["n"])
            else:
                unscoped += int(row["n"])
    except sqlite3.Error as exc:
        return {
            "declared": sorted(declared),
            "present": {},
            "unscoped_pollable": 0,
            "declared_unused": sorted(declared),
            "present_undeclared": [],
            "key": scopes_key_in_use(intent.delegations),
            "database_error": str(exc),
        }
    finally:
        conn.close()

    present = set(counts)
    return {
        "declared": sorted(declared),
        "present": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "unscoped_pollable": unscoped,
        "declared_unused": sorted(declared - present),
        "present_undeclared": sorted(present - declared),
        "key": scopes_key_in_use(intent.delegations),
        "database_error": None,
    }


def format_scope_coverage(intent: Intent, vault: Path, db_path: Path) -> str:
    data = scope_coverage(intent, db_path)
    key = data["key"]
    lines = [f"Delegation scopes — intent v{intent.version} (delegations.{key})", ""]

    if data["database_error"]:
        error = str(data["database_error"])
        if "no such column: scope" in error:
            # Additive migration, applied by ensure_index_schema on the next
            # write. Say which command does it rather than leaving the owner
            # to infer that a missing column means "reindex".
            lines.append("! this index predates the scope column (added 2026-07-29).")
            lines.append("  Run `lisan rebuild-index` (or `lisan sync`) and re-run this command.")
        else:
            lines.append(f"! index unavailable: {error}")
        lines.append("  Declared scopes are shown below; coverage cannot be computed yet.")
        lines.append("")

    declared = data["declared"]
    lines.append(f"Declared in intent.md ({len(declared)}):")
    lines.extend([f"  {name}" for name in declared] or ["  (none)"])
    lines.append("")

    present = data["present"]
    lines.append(f"Present on pollable records ({len(present)}):")
    lines.extend([f"  {name}  ×{count}" for name, count in present.items()] or ["  (none)"])
    lines.append("")

    unused = data["declared_unused"]
    undeclared = data["present_undeclared"]
    unscoped = data["unscoped_pollable"]

    if unused:
        lines.append(f"Declared but unused ({len(unused)}) — these rules can never fire:")
        lines.extend(f"  {name}" for name in unused)
        lines.append("")
    if undeclared:
        lines.append(f"Used but undeclared ({len(undeclared)}) — these fall to defaults:")
        lines.extend(f"  {name}" for name in undeclared)
        lines.append("")
    if unscoped:
        lines.append(
            f"{unscoped} pollable record(s) carry no scope at all — reportable, never "
            "executable. Expected for anything captured from conversation."
        )
        lines.append("")

    if declared and not present:
        lines.append(
            "VERDICT: no record carries any declared scope. Every rule in this "
            "document is unreachable and the Adjutant can only ever report. "
            "Rename scopes to match how you tag work, or tag work to match "
            "these names."
        )
    elif not declared:
        lines.append(
            "VERDICT: no scopes declared. Everything resolves through defaults, "
            "which cannot grant a capability — nothing is executable. Add scopes "
            "to Standing Delegations."
        )
    elif unused and not undeclared:
        lines.append(
            f"VERDICT: {len(present)} scope(s) reachable; {len(unused)} declared rule(s) "
            "unreachable. Unreachable rules are not protecting anything."
        )
    else:
        lines.append(f"VERDICT: {len(present)} scope(s) reachable.")
    lines.append("Dry-run any single decision: lisan intent check <scope> <capability>")
    return "\n".join(lines)
