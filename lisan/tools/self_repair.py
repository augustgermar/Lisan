"""The self-repair loop: propose, apply, bake, and roll back.

Phase A creates and verifies an isolated proposal. Phase B applies only an
exactly approved proposal after revalidating the clean base and patch hash.
Phase C monitors the applied patch for a bake period and rolls back on
regression — deterministically, no LLM, no agent health dependency.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..frontmatter import dump_markdown, load_markdown, write_markdown
from ..utils import slugify
from .adjutant_confirmations import create_confirmation_for_task

Author = Callable[[str], str]
Verifier = Callable[[dict[str, Any]], dict[str, Any] | bool]

PROTECTED_PATHS = (
    "primer/identity-core.md",
    "kernel.py",
    "action_policy.py",
    "self_repair.py",
    "self_repair_",
    ".gitignore",
)
PROTECTED_DIRECTORIES = ("credentials/", "backup/", "backups/", "purge/")


class SelfRepairRefused(RuntimeError):
    """The proposal violates a structural Phase A boundary."""


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    loop_id: str
    base_commit: str
    patch_hash: str
    worktree: Path
    report_path: Path
    confirmation_id: str | None
    telegram_message: str


@dataclass(frozen=True)
class AppliedProposal:
    proposal_id: str
    commit: str
    restart_job_id: str | None
    report_path: Path


DEFAULT_BAKE_HOURS = 48
BAKE_CHECK_INTERVAL_HOURS = 12
MAX_BAKE_EXTENSIONS = 2
REGRESSION_DROP_THRESHOLD = 0.5
_SELF_EVAL_DIM_PREFIX = "self-eval-dim-"


def _run(command: list[str], *, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)


def _git(repo: Path, *args: str, timeout: int = 120) -> str:
    result = _run(["git", *args], cwd=repo, timeout=timeout)
    if result.returncode:
        raise SelfRepairRefused(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def ensure_clean_checkout(repo: Path) -> str:
    """Return HEAD, refusing any owner changes in the live checkout."""
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise SelfRepairRefused("live checkout is dirty; self-repair will not touch owner changes")
    return _git(repo, "rev-parse", "HEAD")


def patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("diff --git a/"):
            continue
        fields = line.split()
        if len(fields) != 4 or not fields[2].startswith("a/") or not fields[3].startswith("b/"):
            raise SelfRepairRefused("malformed diff header")
        left, right = fields[2][2:], fields[3][2:]
        if left != right:
            raise SelfRepairRefused("renames are not allowed in Phase A")
        paths.append(right)
    if not paths:
        raise SelfRepairRefused("proposal contains no git diff paths")
    return paths


def validate_patch_paths(patch: str) -> list[str]:
    paths = patch_paths(patch)
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized.startswith("/") or normalized == ".." or normalized.startswith("../") or "/../" in normalized:
            raise SelfRepairRefused(f"unsafe patch path: {path}")
        if normalized in PROTECTED_PATHS or any(normalized.endswith(item) for item in PROTECTED_PATHS if item.endswith(".py")):
            raise SelfRepairRefused(f"protected patch path: {path}")
        if any(normalized == item or normalized.startswith(item) for item in PROTECTED_DIRECTORIES):
            raise SelfRepairRefused(f"protected patch path: {path}")
        if normalized.startswith(".git/"):
            raise SelfRepairRefused(f"git metadata path: {path}")
    return paths


def _loop_record(vault: Path, loop_id: str, loop_path: Path | None) -> tuple[Path, dict[str, Any]]:
    if loop_path is None:
        candidates = sorted((vault / "open_loops").glob("*.md"))
    else:
        candidates = [loop_path]
    matched_inactive = False
    for path in candidates:
        try:
            fm = dict(load_markdown(path).frontmatter)
        except Exception:
            continue
        if str(fm.get("id") or "") == loop_id:
            if str(fm.get("origin") or "") != "self" or str(fm.get("status") or "") not in {"active", "open", "pending"}:
                matched_inactive = True
                if loop_path is not None:
                    raise SelfRepairRefused("self-repair requires an active origin:self loop")
                continue
            return path, fm
    if matched_inactive:
        raise SelfRepairRefused("self-repair requires an active origin:self loop")
    raise SelfRepairRefused(f"active origin:self loop not found: {loop_id}")


def _author_prompt(loop_id: str, loop: dict[str, Any], paths: list[str] | None = None) -> str:
    requested = ", ".join(paths or []) or "the smallest ordinary application files needed"
    return (
        "You are drafting one narrowly scoped self-repair patch. Return ONLY a unified git diff "
        "with diff --git headers; do not include markdown fences or commentary.\n"
        f"Origin loop: {loop_id}\n"
        f"Finding: {loop.get('summary') or loop.get('description') or '(see linked loop record)'}\n"
        f"Candidate paths: {requested}\n"
        "Never modify policy, identity/kernel, credentials, privacy, backup, purge, or self-repair files. "
        "Add or update tests when appropriate."
    )


def _default_verifier(result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": bool(result.get("suite_ok") and result.get("targeted_ok")), "method": "deterministic test commands"}


def propose(
    *,
    vault: Path,
    repo: Path,
    loop_id: str,
    author: Author,
    verifier: Verifier | None = None,
    db_path: Path | None = None,
    loop_path: Path | None = None,
    candidate_paths: list[str] | None = None,
    test_command: list[str] | None = None,
    targeted_command: list[str] | None = None,
    worktree_root: Path | None = None,
    author_id: str = "repair-author",
    verifier_id: str = "deterministic-verifier",
) -> Proposal:
    """Create a Phase A proposal.  No operation here mutates ``repo``."""
    if author_id == verifier_id:
        raise SelfRepairRefused("examiner and examinee must be independent")
    loop_file, loop = _loop_record(vault, loop_id, loop_path)
    base_commit = ensure_clean_checkout(repo)
    prompt = _author_prompt(loop_id, loop, candidate_paths)
    patch = str(author(prompt) or "").strip()
    paths = validate_patch_paths(patch)
    patch_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    proposal_id = f"sr-{date.today().isoformat()}-{uuid.uuid4().hex[:10]}"

    root = (worktree_root or Path(tempfile.gettempdir()) / "lisan-self-repair").resolve()
    root.mkdir(parents=True, exist_ok=True)
    worktree = root / proposal_id
    _git(repo, "worktree", "add", "--detach", str(worktree), base_commit, timeout=120)
    try:
        patch_file = worktree / ".self-repair.patch"
        patch_file.write_text(patch + "\n", encoding="utf-8")
        checked = _run(["git", "apply", "--check", str(patch_file)], cwd=worktree)
        if checked.returncode:
            raise SelfRepairRefused(checked.stderr.strip() or "git apply --check rejected the proposal")
        applied = _run(["git", "apply", str(patch_file)], cwd=worktree)
        if applied.returncode:
            raise SelfRepairRefused(applied.stderr.strip() or "git apply rejected the proposal")
        patch_file.unlink(missing_ok=True)

        suite = _run(test_command or ["python3", "-m", "pytest", "-q"], cwd=worktree, timeout=1800)
        targeted = _run(targeted_command, cwd=worktree, timeout=900) if targeted_command else suite
        result = {
            "proposal_id": proposal_id,
            "loop_id": loop_id,
            "base_commit": base_commit,
            "patch_hash": patch_hash,
            "paths": paths,
            "suite_ok": suite.returncode == 0,
            "suite_output": (suite.stdout + suite.stderr)[-12000:],
            "targeted_ok": targeted.returncode == 0,
            "targeted_output": (targeted.stdout + targeted.stderr)[-12000:],
            "worktree": str(worktree),
        }
        verification = dict(verifier(result) if verifier else _default_verifier(result))
        if not verification.get("ok"):
            raise SelfRepairRefused("independent verification did not approve the proposal")

        reports = vault / "reports" / "self-repair-proposals"
        reports.mkdir(parents=True, exist_ok=True)
        report = reports / f"{proposal_id}.md"
        body = (
            f"# Self-repair proposal {proposal_id}\n\n"
            f"Origin loop: `{loop_id}`\n\n"
            f"Base commit: `{base_commit}`\n\n"
            f"Patch hash: `{patch_hash}`\n\n"
            f"Paths: {', '.join(f'`{p}`' for p in paths)}\n\n"
            f"Author: `{author_id}`; verifier: `{verifier_id}`\n\n"
            f"## Verification\n\n```json\n{json.dumps(verification, indent=2, sort_keys=True)}\n```\n\n"
            f"## Patch\n\n```diff\n{patch}\n```\n"
        )
        write_markdown(report, {
            "id": f"report.{proposal_id}", "type": "report", "created": date.today().isoformat(),
            "updated": date.today().isoformat(), "status": "active", "summary": f"Self-repair proposal {proposal_id}",
            "source": "self_repair", "loop_id": loop_id, "proposal_hash": patch_hash,
            "base_commit": base_commit, "worktree": str(worktree),
        }, body)
        task_id = f"self-repair:{proposal_id}"
        confirmation = create_confirmation_for_task(
            vault, task_id=task_id,
            task_summary=f"Self-repair proposal {proposal_id}: {loop.get('summary') or loop_id}",
            planned_action=(f"Phase A proposal only; no live files will change. Review {report} and approve the exact "
                            f"proposal hash {patch_hash} with `approve <confirmation-id>`."),
            risk="The proposal is isolated and cannot apply changes in Phase A; protected paths are refused.",
            scope="self_repair", db_path=db_path,
        )
        confirmation_id = confirmation
        telegram = (
            f"Self-repair proposal {proposal_id}\n"
            f"Loop: {loop_id}\n"
            f"Files: {', '.join(paths)}\n"
            f"Verification: passed ({verifier_id})\n"
            f"Hash: {patch_hash}\n"
            f"Full report: {report}\n"
            f"Phase A only — no live files changed. Confirmation: {confirmation_id or 'already pending'}"
        )
        return Proposal(proposal_id, loop_id, base_commit, patch_hash, worktree, report, confirmation_id, telegram)
    except Exception:
        try:
            _git(repo, "worktree", "remove", "--force", str(worktree), timeout=120)
        except Exception:
            pass
        shutil.rmtree(worktree, ignore_errors=True)
        raise


def _report_patch(body: str) -> str:
    marker = "```diff\n"
    start = body.find(marker)
    if start < 0:
        raise SelfRepairRefused("proposal report has no unified diff")
    start += len(marker)
    end = body.find("\n```", start)
    if end < 0:
        raise SelfRepairRefused("proposal report has an unterminated unified diff")
    patch = body[start:end].strip()
    if not patch:
        raise SelfRepairRefused("proposal report contains an empty unified diff")
    return patch


def _approved_confirmation(vault: Path, proposal_id: str, db_path: Path | None) -> dict[str, Any]:
    """Return the exact owner-approved confirmation for a proposal."""
    from .db import connect
    from .rebuild_index import ensure_index_schema

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_index_schema(conn)
        row = conn.execute(
            "SELECT * FROM confirmations WHERE task_id = ? AND resolution = 'approved' "
            "AND status = 'pending' ORDER BY resolved_at DESC LIMIT 1",
            (f"self-repair:{proposal_id}",),
        ).fetchone()
        if row is None:
            raise SelfRepairRefused("proposal has no exact owner approval")
        return dict(row)
    finally:
        conn.close()


def _resolve_origin_loop(vault: Path, loop_id: str, *, proposal_id: str, commit: str) -> None:
    for path in sorted((vault / "open_loops").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("id") or "") != loop_id:
            continue
        fm = dict(doc.frontmatter)
        today = date.today().isoformat()
        fm.update({
            "status": "resolved",
            "updated": today,
            "resolved_at": today,
            "resolved_by": "self_repair",
            "resolution": f"applied proposal {proposal_id} as {commit}",
        })
        write_markdown(path, fm, doc.body)
        return
    raise SelfRepairRefused(f"origin loop not found after apply: {loop_id}")


def apply_approved_proposal(
    *,
    vault: Path,
    repo: Path,
    proposal_id: str,
    db_path: Path | None = None,
    worktree_root: Path | None = None,
    config: dict[str, Any] | None = None,
    policy_check: Callable[[str, dict[str, Any]], bool] | None = None,
) -> AppliedProposal:
    """Phase B: apply one exact, owner-approved proposal as one local commit.

    The default policy check remains blocked by the live tier-3 clamp. The
    injectable check exists for deterministic tests without weakening runtime
    policy.
    """
    from .action_policy import action_allowed

    if config is None:
        from ..config import load_config

        config = load_config()
    check = policy_check or (lambda kind, cfg: action_allowed(kind, cfg))
    if not check("self_repair_apply", config):
        raise SelfRepairRefused("self-repair apply is disabled by the action-policy clamp")
    proposal_id = str(proposal_id).strip()
    if not proposal_id or "/" in proposal_id or "\\" in proposal_id or ".." in proposal_id:
        raise SelfRepairRefused("invalid proposal id")
    report = vault / "reports" / "self-repair-proposals" / f"{proposal_id}.md"
    if not report.exists():
        raise SelfRepairRefused(f"proposal report not found: {proposal_id}")
    doc = load_markdown(report)
    fm = dict(doc.frontmatter)
    if str(fm.get("status") or "") != "approved":
        raise SelfRepairRefused("proposal is not owner-approved")
    approval = _approved_confirmation(vault, proposal_id, db_path)
    base_commit = str(fm.get("base_commit") or "").strip()
    loop_id = str(fm.get("loop_id") or "").strip()
    if not base_commit:
        match = re.search(r"^Base commit: `([^`]+)`", doc.body, re.MULTILINE)
        base_commit = match.group(1).strip() if match else ""
    if not loop_id:
        match = re.search(r"^Origin loop: `([^`]+)`", doc.body, re.MULTILINE)
        loop_id = match.group(1).strip() if match else ""
    expected_hash = str(fm.get("proposal_hash") or "").strip()
    if not base_commit or not loop_id or not expected_hash:
        raise SelfRepairRefused("proposal report is missing apply metadata")
    patch = _report_patch(doc.body)
    actual_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    if actual_hash != expected_hash:
        raise SelfRepairRefused("proposal hash does not match the approved report")
    validate_patch_paths(patch)
    _loop_record(vault, loop_id, None)
    current_base = ensure_clean_checkout(repo)
    if current_base != base_commit:
        raise SelfRepairRefused(f"live checkout moved from approved base {base_commit} to {current_base}")

    worktree_value = str(fm.get("worktree") or "").strip()
    if worktree_value:
        worktree = Path(worktree_value)
    else:
        root = (worktree_root or Path(tempfile.gettempdir()) / "lisan-self-repair").resolve()
        worktree = root / proposal_id
    if not worktree.is_dir():
        raise SelfRepairRefused(f"verified proposal worktree is missing: {worktree}")
    if _git(worktree, "rev-parse", "HEAD") != base_commit:
        raise SelfRepairRefused("proposal worktree no longer points at the approved base")
    checked = subprocess.run(
        ["git", "apply", "--check", "-"], cwd=repo, input=patch + "\n", text=True, capture_output=True
    )
    if checked.returncode:
        raise SelfRepairRefused(checked.stderr.strip() or "approved patch no longer applies cleanly")
    applied = subprocess.run(
        ["git", "apply", "--index", "-"], cwd=repo, input=patch + "\n", text=True, capture_output=True
    )
    if applied.returncode:
        raise SelfRepairRefused(applied.stderr.strip() or "approved patch could not be applied")
    commit_result = _run(
        ["git", "commit", "-m", f"self-repair: apply {proposal_id} (loop {loop_id})"], cwd=repo, timeout=120
    )
    if commit_result.returncode:
        raise SelfRepairRefused(commit_result.stderr.strip() or "self-repair commit failed")
    commit = _git(repo, "rev-parse", "HEAD")

    from .jobs import enqueue_job

    restart_job = enqueue_job(
        "self_repair.restart",
        {"vault": str(vault), "proposal_id": proposal_id, "applied_commit": commit, "approval_id": approval["id"]},
        db_path=db_path,
    )
    _resolve_origin_loop(vault, loop_id, proposal_id=proposal_id, commit=commit)
    today = date.today().isoformat()
    from .self_episodes import SelfEvent, write_self_episode

    write_self_episode(
        vault,
        SelfEvent(
            event_id=f"self-repair-apply-{proposal_id}",
            event_kind="self_repair",
            date=today,
            title=f"Applied self-repair proposal {proposal_id}",
            narration=(f"{{{{self}}}} applied the owner-approved self-repair proposal {proposal_id} "
                       f"for {{{{principal}}}} as local commit {commit}."),
            outcome="succeeded",
            source_refs=[f"reports/self-repair-proposals/{proposal_id}.md", commit],
            significance="high",
        ),
        db_path=db_path,
    )
    bake = _bake_metadata(vault, loop_id, commit, base_commit)
    bake_job = _enqueue_bake_check(vault, proposal_id, db_path=db_path)
    fm.update({
        "status": "applied", "updated": today, "applied_at": today,
        "applied_by": "self_repair", "applied_commit": commit,
        "approval_id": str(approval["id"]), "restart_job_id": restart_job,
        **bake,
        "bake_check_job_id": bake_job,
    })
    write_markdown(report, fm, doc.body + f"\n## Applied\n\nLocal commit: `{commit}`\nRestart job: `{restart_job}`\n")
    return AppliedProposal(proposal_id, commit, restart_job, report)


# ── Phase C: bake monitoring and dumb rollback ───────────────────────────────


def _targeted_dimension(vault: Path, loop_id: str) -> str | None:
    """Extract the self-eval dimension from the origin loop's fingerprint."""
    for path in sorted((vault / "open_loops").glob("*.md")):
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        if str(fm.get("id") or "") != loop_id:
            continue
        fp = str(fm.get("deviation_fingerprint") or "")
        if fp.startswith(_SELF_EVAL_DIM_PREFIX):
            return fp[len(_SELF_EVAL_DIM_PREFIX):]
        return None
    return None


def _latest_dimension_score(vault: Path, dimension: str, *, after: str | None = None) -> float | None:
    """Most recent self-eval score for a dimension, optionally after a date."""
    history = vault / "reports" / "self-eval-history.jsonl"
    if not history.exists():
        return None
    score = None
    try:
        for line in history.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if after and str(entry.get("date") or "") <= after:
                continue
            dims = entry.get("dimensions") or {}
            dim_stats = dims.get(dimension)
            if isinstance(dim_stats, dict) and dim_stats.get("n", 0) > 0:
                score = float(dim_stats["mean"])
    except Exception:
        pass
    return score


def _error_log_line_count(vault: Path) -> int:
    log = vault / "logs" / "errors.log"
    if not log.exists():
        return 0
    try:
        return len(log.read_text(encoding="utf-8", errors="ignore").splitlines())
    except Exception:
        return 0


def _bake_metadata(
    vault: Path,
    loop_id: str,
    applied_commit: str,
    base_commit: str,
    *,
    bake_hours: int = DEFAULT_BAKE_HOURS,
) -> dict[str, Any]:
    """Compute the rollback metadata recorded at apply time."""
    now = datetime.now(timezone.utc)
    dimension = _targeted_dimension(vault, loop_id)
    pre_score = _latest_dimension_score(vault, dimension) if dimension else None
    return {
        "bake_status": "monitoring",
        "bake_start": now.isoformat(),
        "bake_end": (now + timedelta(hours=bake_hours)).isoformat(),
        "bake_base_commit": base_commit,
        "bake_applied_commit": applied_commit,
        "targeted_dimension": dimension,
        "bake_pre_score": pre_score,
        "bake_error_log_lines": _error_log_line_count(vault),
        "bake_extensions": 0,
    }


def _enqueue_bake_check(
    vault: Path,
    proposal_id: str,
    *,
    delay_hours: int = BAKE_CHECK_INTERVAL_HOURS,
    db_path: Path | None = None,
) -> str:
    """Schedule the next bake check."""
    from .jobs import enqueue_job

    scheduled = datetime.now(timezone.utc) + timedelta(hours=delay_hours)
    return enqueue_job(
        "self_repair.bake_check",
        {"vault": str(vault), "proposal_id": proposal_id},
        db_path=db_path,
        scheduled_for=scheduled.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _load_proposal_report(vault: Path, proposal_id: str) -> tuple[Path, dict[str, Any], str]:
    """Load and validate a proposal report. Returns (path, frontmatter, body)."""
    proposal_id = str(proposal_id).strip()
    if not proposal_id or "/" in proposal_id or "\\" in proposal_id or ".." in proposal_id:
        raise SelfRepairRefused("invalid proposal id")
    report = vault / "reports" / "self-repair-proposals" / f"{proposal_id}.md"
    if not report.exists():
        raise SelfRepairRefused(f"proposal report not found: {proposal_id}")
    doc = load_markdown(report)
    return report, dict(doc.frontmatter), doc.body


def run_bake_check(
    vault: Path,
    proposal_id: str,
    *,
    repo: Path | None = None,
    db_path: Path | None = None,
    test_command: list[str] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One deterministic bake-period check. No LLM calls.

    Returns a report dict; handles its own rescheduling, rollback, or
    graduation. The caller (job dispatcher) needs only to return the result.
    """
    from .action_policy import action_allowed

    if config is None:
        from ..config import load_config
        config = load_config()

    report_path, fm, body = _load_proposal_report(vault, proposal_id)

    if str(fm.get("bake_status") or "") != "monitoring":
        return {"proposal_id": proposal_id, "verdict": "not_monitoring",
                "bake_status": fm.get("bake_status")}

    repo = repo or Path(__file__).resolve().parents[2]
    now = datetime.now(timezone.utc)
    bake_end_str = str(fm.get("bake_end") or "")
    bake_start_str = str(fm.get("bake_start") or "")
    applied_commit = str(fm.get("bake_applied_commit") or fm.get("applied_commit") or "")
    dimension = fm.get("targeted_dimension")
    pre_score = fm.get("bake_pre_score")
    extensions = int(fm.get("bake_extensions") or 0)

    try:
        bake_end = datetime.fromisoformat(bake_end_str)
        if bake_end.tzinfo is None:
            bake_end = bake_end.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        bake_end = now

    expired = now >= bake_end

    suite = _run(test_command or ["python3", "-m", "pytest", "-q"], cwd=repo, timeout=1800)
    suite_ok = suite.returncode == 0

    error_delta = _error_log_line_count(vault) - int(fm.get("bake_error_log_lines") or 0)

    post_score = None
    score_drop = None
    if dimension and pre_score is not None:
        post_score = _latest_dimension_score(vault, dimension, after=bake_start_str[:10])
        if post_score is not None:
            score_drop = round(float(pre_score) - post_score, 2)

    if not suite_ok:
        verdict = "regression"
    elif dimension and pre_score is not None and score_drop is not None and score_drop >= REGRESSION_DROP_THRESHOLD:
        verdict = "regression"
    elif expired and dimension and post_score is None and extensions < MAX_BAKE_EXTENSIONS:
        verdict = "inconclusive"
    elif expired:
        verdict = "passed"
    else:
        verdict = "ok"

    result: dict[str, Any] = {
        "proposal_id": proposal_id,
        "check_time": now.isoformat(),
        "suite_ok": suite_ok,
        "error_delta": error_delta,
        "targeted_dimension": dimension,
        "pre_score": pre_score,
        "post_score": post_score,
        "score_drop": score_drop,
        "verdict": verdict,
    }

    today = date.today().isoformat()

    if verdict == "regression":
        if action_allowed("self_repair_rollback", config):
            try:
                rb = rollback_applied_proposal(
                    vault, proposal_id, repo=repo, db_path=db_path,
                    policy_check=lambda _k, _c: True,
                )
                result["rolled_back"] = True
                result["rollback_commit"] = rb.get("rollback_commit")
            except SelfRepairRefused as exc:
                result["rolled_back"] = False
                result["rollback_refused"] = str(exc)
                fm["bake_status"] = "regression_unresolved"
                fm["updated"] = today
                write_markdown(report_path, fm, body)
        else:
            result["rolled_back"] = False
            result["rollback_refused"] = "self_repair_rollback action not enabled"
            fm["bake_status"] = "regression_unresolved"
            fm["updated"] = today
            write_markdown(report_path, fm, body)

    elif verdict == "inconclusive":
        new_end = bake_end + timedelta(hours=DEFAULT_BAKE_HOURS)
        fm["bake_end"] = new_end.isoformat()
        fm["bake_extensions"] = extensions + 1
        fm["updated"] = today
        write_markdown(report_path, fm, body)
        _enqueue_bake_check(vault, proposal_id, db_path=db_path)

    elif verdict == "passed":
        fm["bake_status"] = "passed"
        fm["updated"] = today
        write_markdown(report_path, fm, body)
        from .self_episodes import SelfEvent, write_self_episode
        write_self_episode(
            vault,
            SelfEvent(
                event_id=f"self-repair-bake-passed-{proposal_id}",
                event_kind="self_repair",
                date=today,
                title=f"Bake period passed for {proposal_id}",
                narration=(
                    f"{{{{self}}}} monitored its own self-repair patch {proposal_id} "
                    f"through the bake period with no regression detected."
                ),
                outcome="succeeded",
                source_refs=[f"reports/self-repair-proposals/{proposal_id}.md"],
                significance="medium",
            ),
            db_path=db_path,
        )

    elif verdict == "ok":
        _enqueue_bake_check(vault, proposal_id, db_path=db_path)

    return result


def _owner_commits_after(repo: Path, applied_commit: str) -> list[str]:
    """Commits between applied_commit and HEAD (exclusive of applied_commit)."""
    try:
        out = _git(repo, "log", "--format=%H", f"{applied_commit}..HEAD")
        return [h for h in out.splitlines() if h.strip()]
    except SelfRepairRefused:
        return []


def _reopen_origin_loop(vault: Path, loop_id: str, *, reason: str) -> None:
    """Set an origin loop back to active after a rollback."""
    for path in sorted((vault / "open_loops").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("id") or "") != loop_id:
            continue
        fm = dict(doc.frontmatter)
        fm.update({
            "status": "active",
            "updated": date.today().isoformat(),
            "reopened_reason": reason,
        })
        for key in ("resolved_at", "resolved_by", "resolution"):
            fm.pop(key, None)
        write_markdown(path, fm, doc.body)
        return


def rollback_applied_proposal(
    vault: Path,
    proposal_id: str,
    *,
    repo: Path | None = None,
    db_path: Path | None = None,
    policy_check: Callable[[str, dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Phase C rollback: revert the exact applied commit. Dumb — no LLM."""
    from .action_policy import action_allowed

    check = policy_check or (lambda kind, cfg: action_allowed(kind, cfg))
    config: dict[str, Any] = {}
    if policy_check is None:
        from ..config import load_config
        config = load_config()
    if not check("self_repair_rollback", config):
        raise SelfRepairRefused("self-repair rollback is disabled by the action-policy clamp")

    report_path, fm, body = _load_proposal_report(vault, proposal_id)
    applied_commit = str(fm.get("bake_applied_commit") or fm.get("applied_commit") or "").strip()
    base_commit = str(fm.get("bake_base_commit") or fm.get("base_commit") or "").strip()
    loop_id = str(fm.get("loop_id") or "").strip()
    if not applied_commit or not base_commit:
        raise SelfRepairRefused("proposal report is missing rollback metadata")

    repo = repo or Path(__file__).resolve().parents[2]

    after = _owner_commits_after(repo, applied_commit)
    if after:
        raise SelfRepairRefused(
            f"{len(after)} owner commit(s) exist after the applied commit; manual rollback required"
        )

    current_head = _git(repo, "rev-parse", "HEAD")
    if current_head != applied_commit:
        raise SelfRepairRefused(
            f"HEAD ({current_head[:12]}) is not the applied commit ({applied_commit[:12]}); "
            "cannot auto-revert safely"
        )

    diff_paths = _git(repo, "diff", "--name-only", f"{applied_commit}^", applied_commit)
    for p in diff_paths.splitlines():
        p = p.strip()
        if not p:
            continue
        normalized = p.replace("\\", "/")
        if normalized in PROTECTED_PATHS or any(normalized.endswith(item) for item in PROTECTED_PATHS if item.endswith(".py")):
            raise SelfRepairRefused(f"rollback touches protected path: {p}")
        if any(normalized == d or normalized.startswith(d) for d in PROTECTED_DIRECTORIES):
            raise SelfRepairRefused(f"rollback touches protected directory: {p}")

    _git(repo, "revert", "--no-edit", applied_commit)
    rollback_commit = _git(repo, "rev-parse", "HEAD")

    from .jobs import enqueue_job
    restart_job = enqueue_job(
        "self_repair.restart",
        {"vault": str(vault), "proposal_id": proposal_id, "rollback_commit": rollback_commit},
        db_path=db_path,
    )

    today = date.today().isoformat()
    from .self_episodes import SelfEvent, write_self_episode
    dim = fm.get("targeted_dimension")
    pre = fm.get("bake_pre_score")
    narration = (
        f"{{{{self}}}} detected a regression after applying self-repair patch {proposal_id} "
        f"and rolled it back automatically to the pre-patch state."
    )
    if dim and pre is not None:
        post = _latest_dimension_score(vault, dim, after=str(fm.get("bake_start") or "")[:10])
        if post is not None:
            narration = (
                f"{{{{self}}}} detected a regression after applying self-repair patch {proposal_id}: "
                f"the targeted dimension '{dim}' dropped from {pre} to {post}. "
                f"Rolled back automatically to the pre-patch state."
            )
    write_self_episode(
        vault,
        SelfEvent(
            event_id=f"self-repair-rollback-{proposal_id}",
            event_kind="self_repair",
            date=today,
            title=f"Rolled back self-repair proposal {proposal_id}",
            narration=narration,
            outcome="rolled_back",
            source_refs=[f"reports/self-repair-proposals/{proposal_id}.md", rollback_commit],
            significance="high",
        ),
        db_path=db_path,
    )

    if loop_id:
        _reopen_origin_loop(vault, loop_id, reason=f"rolled back {proposal_id}")

    fm.update({
        "bake_status": "rolled_back",
        "updated": today,
        "rolled_back_at": today,
        "rollback_commit": rollback_commit,
        "rollback_restart_job_id": restart_job,
    })
    write_markdown(report_path, fm, body)
    return {"rolled_back": True, "rollback_commit": rollback_commit, "restart_job_id": restart_job}
