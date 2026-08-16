"""Phase A and B of the self-repair loop.

Phase A creates and verifies an isolated proposal. Phase B applies only an
exactly approved proposal after revalidating the clean base and patch hash.
The live policy clamp still keeps Phase B unreachable until the owner enables
it explicitly.
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
from datetime import date
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
    for path in candidates:
        try:
            fm = dict(load_markdown(path).frontmatter)
        except Exception:
            continue
        if str(fm.get("id") or "") == loop_id:
            if str(fm.get("origin") or "") != "self" or str(fm.get("status") or "") not in {"active", "open", "pending"}:
                raise SelfRepairRefused("self-repair requires an active origin:self loop")
            return path, fm
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
    fm.update({
        "status": "applied", "updated": today, "applied_at": today,
        "applied_by": "self_repair", "applied_commit": commit,
        "approval_id": str(approval["id"]), "restart_job_id": restart_job,
    })
    write_markdown(report, fm, doc.body + f"\n## Applied\n\nLocal commit: `{commit}`\nRestart job: `{restart_job}`\n")
    return AppliedProposal(proposal_id, commit, restart_job, report)
