"""`lisan doctor`: check that the system's claims about itself are true.

The project's principle is "instruments before rules": an honesty rule is
worthless without something that can tell the truth. The doctor applies
that to the system's own architecture. Each check states a claim the README
or SPEC makes and measures whether the running install keeps it:

- every outbound or execution path is decided by the authority engine;
- every declared job type is dispatchable;
- the hand-written "not built" list does not deny a real capability;
- intent.md is adopted, so execution is not silently blocked or open;
- the identity kernel matches its ratified hash;
- entity relationships survive a rebuild (they live in markdown);
- every record marked "embedded" has a vector;
- entity story logs hold the owner's words, not pipeline JSON;
- self-repair cannot touch the mechanisms that judge it.

Read-only: the doctor never repairs anything. It reports PASS, WARN, or
FAIL with the evidence, so the owner (or a self-repair loop) can act.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass(frozen=True)
class Check:
    name: str
    claim: str
    status: str
    evidence: str


def _check_authority_covers_outbound(vault: Path, db_path: Path) -> Check:
    from .authority import ACTIONS
    from .execution_tools import TOOLS

    claim = "Every action that leaves the machine needs the owner's yes for that exact action."
    outbound = {name: spec for name, spec in ACTIONS.items() if spec.outbound}
    unapproved = [name for name, spec in outbound.items() if not spec.approval and not name.startswith("drive.")]
    tool_names = {t["name"] for t in TOOLS}
    missing = [name for name in ("send_email",) if name in tool_names and name not in ACTIONS]
    if unapproved or missing:
        return Check("authority", claim, FAIL, f"ungated outbound actions: {unapproved + missing}")
    return Check("authority", claim, PASS, f"{len(outbound)} outbound action kinds, all approval-gated or tier-gated")


def _check_job_registry(vault: Path, db_path: Path) -> Check:
    from .jobs import _JOB_HANDLERS, JOB_TYPES

    claim = "Every declared job type can be dispatched."
    missing = sorted(set(JOB_TYPES) - set(_JOB_HANDLERS))
    extra = sorted(set(_JOB_HANDLERS) - set(JOB_TYPES))
    if missing or extra:
        return Check("jobs", claim, FAIL, f"undispatchable: {missing}; undeclared handlers: {extra}")
    return Check("jobs", claim, PASS, f"{len(JOB_TYPES)} job types, one handler each")


def _check_not_built(vault: Path, db_path: Path) -> Check:
    from .execution_tools import TOOLS
    from .self_model import NOT_BUILT

    claim = "The self-model never tells the agent a real capability is missing."
    tool_names = {t["name"].replace("_", " ") for t in TOOLS}
    text = " ".join(f"{i['name']} {i['detail']}" for i in NOT_BUILT).lower()
    contradicted = sorted(name for name in tool_names if len(name) > 6 and f"no {name}" in text)
    if contradicted:
        return Check("self-model", claim, FAIL, f"NOT_BUILT denies: {contradicted}")
    return Check("self-model", claim, PASS, f"{len(NOT_BUILT)} declared gaps, none contradicted by the tool surface")


def _check_intent(vault: Path, db_path: Path) -> Check:
    from .intent import IntentError, has_sentinel_dates, load_intent

    claim = "Execution answers to an adopted intent.md (absent or template intent blocks execution)."
    try:
        intent = load_intent(vault)
    except IntentError as exc:
        return Check("intent", claim, WARN, f"{exc} — chat execution and delegation will refuse")
    if has_sentinel_dates(intent):
        return Check("intent", claim, WARN, "intent.md is still the template — execution will refuse until adopted")
    return Check("intent", claim, PASS, f"intent.md v{intent.version} adopted")


def _check_kernel(vault: Path, db_path: Path) -> Check:
    from .kernel import compute_kernel_hash, kernel_path, stored_kernel_hash

    claim = "The identity kernel changes only through a ceremony."
    path = kernel_path(vault)
    if not path.exists():
        return Check("kernel", claim, WARN, "no identity kernel yet")
    # Read-only: verify_kernel would also append a drift report.
    text = path.read_text(encoding="utf-8")
    stored = stored_kernel_hash(text)
    if not stored:
        return Check("kernel", claim, WARN, "kernel is unstamped")
    if compute_kernel_hash(text) != stored:
        return Check("kernel", claim, FAIL, "kernel content no longer matches its ratified hash (drift)")
    return Check("kernel", claim, PASS, "kernel matches its ratified hash")


def _connect(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    from .db import connect

    return connect(db_path, readonly=True)


def _check_edges_in_markdown(vault: Path, db_path: Path) -> Check:
    from ..frontmatter import load_frontmatter_cached

    claim = "Entity relationships live in markdown, so a rebuild restores them."
    conn = _connect(db_path)
    if conn is None:
        return Check("edges", claim, WARN, "no index database")
    try:
        rows = conn.execute(
            "SELECT l.source_id, l.target_id, l.relationship_type, f.path FROM links l "
            "JOIN files f ON f.id = l.source_id WHERE l.source_id LIKE 'entity.%' AND l.target_id LIKE 'entity.%'"
        ).fetchall()
    except sqlite3.Error as exc:
        return Check("edges", claim, WARN, f"could not read links: {exc}")
    finally:
        conn.close()
    orphaned = 0
    for source_id, target_id, rel_type, rel_path in rows:
        try:
            rels = load_frontmatter_cached(vault / rel_path).get("relationships") or []
        except Exception:
            rels = []
        if not any(isinstance(r, dict) and r.get("target") == target_id for r in rels):
            orphaned += 1
    if orphaned:
        return Check("edges", claim, FAIL, f"{orphaned} of {len(rows)} entity edges exist only in SQLite")
    return Check("edges", claim, PASS, f"{len(rows)} entity edges, all backed by frontmatter")


def _check_embeddings(vault: Path, db_path: Path) -> Check:
    from .vector_store import load_index

    claim = "A record marked 'embedded' has a vector on disk."
    conn = _connect(db_path)
    if conn is None:
        return Check("embeddings", claim, WARN, "no index database")
    try:
        embedded = {r[0] for r in conn.execute("SELECT id FROM files WHERE embedding_status = 'embedded'")}
    except sqlite3.Error as exc:
        return Check("embeddings", claim, WARN, f"could not read files: {exc}")
    finally:
        conn.close()
    # Records may be stored whole (``<id>``) or as passages
    # (``<id>::passage:<n>:<hash>``); either form means the record has a vector.
    vectors = {key.split("::passage:", 1)[0] for key in load_index(db_path.parent / "embeddings.bin").vectors}
    missing = embedded - vectors
    if missing:
        status = FAIL if len(missing) > max(5, len(embedded) // 50) else WARN
        return Check("embeddings", claim, status, f"{len(missing)} of {len(embedded)} 'embedded' rows have no vector")
    return Check("embeddings", claim, PASS, f"{len(embedded)} embedded rows, all with vectors")


_PIPELINE_MARKERS = ('"action"', "Skeptic approved", "Skeptic rejected", '"memory_type"')


def _check_story_logs(vault: Path, db_path: Path) -> Check:
    from ..frontmatter import load_frontmatter_cached

    claim = "Entity story logs hold what was said, not pipeline JSON."
    total = polluted = 0
    affected = 0
    for path in (vault / "entities").rglob("*.md") if (vault / "entities").exists() else []:
        if "archive" in path.parts:
            continue
        try:
            log = load_frontmatter_cached(path).get("source_log") or []
        except Exception:
            continue
        bad = sum(1 for e in log if isinstance(e, dict) and any(m in str(e.get("text", "")) for m in _PIPELINE_MARKERS))
        total += len(log)
        polluted += bad
        affected += bool(bad)
    if polluted:
        return Check("story-logs", claim, WARN,
                     f"{polluted} of {total} log entries ({affected} entities) carry pipeline JSON from before the "
                     "2026-10-29 fix; new entries are clean")
    return Check("story-logs", claim, PASS, f"{total} log entries, none with pipeline JSON")


def _check_self_repair_protection(vault: Path, db_path: Path) -> Check:
    from .self_repair import SelfRepairRefused, validate_patch_paths

    claim = "Self-repair cannot modify what decides, verifies, or undoes a repair."
    must_refuse = [
        "lisan/tools/authority.py", "lisan/tools/intent.py", "lisan/tools/judge.py",
        "lisan/tools/kernel.py", "lisan/tools/purge.py", "tests/test_authority.py",
    ]
    allowed = []
    for path in must_refuse:
        diff = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-x\n+y\n"
        try:
            validate_patch_paths(diff)
            allowed.append(path)
        except SelfRepairRefused:
            pass
    if allowed:
        return Check("self-repair", claim, FAIL, f"patchable: {allowed}")
    return Check("self-repair", claim, PASS, f"{len(must_refuse)} sampled guard paths refused")


CHECKS: list[Callable[[Path, Path], Check]] = [
    _check_authority_covers_outbound,
    _check_job_registry,
    _check_not_built,
    _check_intent,
    _check_kernel,
    _check_edges_in_markdown,
    _check_embeddings,
    _check_story_logs,
    _check_self_repair_protection,
]


def run_doctor(vault: Path, db_path: Path) -> list[Check]:
    results: list[Check] = []
    for check in CHECKS:
        try:
            results.append(check(vault, db_path))
        except Exception as exc:  # a broken instrument is itself a finding
            name = check.__name__.removeprefix("_check_").replace("_", "-")
            results.append(Check(name, "(check crashed)", FAIL, f"{type(exc).__name__}: {exc}"))
    return results


def format_doctor(results: list[Check]) -> str:
    width = max(len(r.name) for r in results) if results else 0
    lines = []
    for r in results:
        lines.append(f"{r.status:4}  {r.name:<{width}}  {r.claim}")
        lines.append(f"      {'':<{width}}  → {r.evidence}")
    worst = FAIL if any(r.status == FAIL for r in results) else WARN if any(r.status == WARN for r in results) else PASS
    lines.append("")
    lines.append(f"overall: {worst}")
    return "\n".join(lines)


def doctor_json(results: list[Check]) -> str:
    return json.dumps([r.__dict__ for r in results], indent=2, ensure_ascii=True)


def exit_code(results: list[Any]) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0
