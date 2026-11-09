"""The self-repair loop: propose, apply, bake, and roll back.

Phase A creates and verifies an isolated proposal. Phase B applies only an
exactly approved proposal after revalidating the clean base and patch hash.
Phase C monitors the applied patch for a bake period and rolls back on
regression — deterministically, no LLM, no agent health dependency.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from .adjutant_confirmations import create_confirmation_for_task

Author = Callable[[str], str]
Verifier = Callable[[dict[str, Any]], dict[str, Any] | bool]

# The repair loop may never modify what decides what it may change, how a
# change is verified, or how one is undone (docs/security.md). The list used
# to stop at the kernel, the tier table and self_repair.py — leaving the
# intent resolver, the outbound gates, the content fences, the write
# boundary, the judge and rubric that score its own bake, purge/backup, and
# every existing test free to patch. And the "self_repair_" prefix never
# matched anything, because only ".py" entries were suffix-checked.
PROTECTED_PATHS = (
    "primer/identity-core.md",
    ".gitignore",
    "pyproject.toml",
)
PROTECTED_BASENAMES = frozenset({
    # identity
    "kernel.py",
    "voice_extract.py",
    # policy: what may happen
    "action_policy.py",
    "authority.py",
    "outbound.py",
    "intent.py",
    "intent_scopes.py",
    "adjutant_gate.py",
    "skill_gate.py",
    "approval_receipts.py",
    # epistemic boundary: what counts as instructions
    "firewall.py",
    "content_trust.py",
    # integrity: what may be written
    "write_boundary.py",
    "validator.py",
    "validation_gate.py",
    # verification: how a change is judged
    "judge.py",
    "rubric.py",
    "self_eval.py",
    "conftest.py",
    # undo: how a change is reversed or data recovered
    "purge.py",
    "backup.py",
    # publication boundary: what the repair loop may say about itself in
    # public (docs/self_repair_upstream_workorder.md)
    "public_rationale.py",
})
PROTECTED_BASENAME_PREFIXES = ("self_repair",)
PROTECTED_DIRECTORIES = ("credentials/", "backup/", "backups/", "purge/", ".github/", ".githooks/")


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


def default_test_command() -> list[str]:
    """The suite command used when the caller does not supply one.

    It runs under the interpreter Lisan itself is running in. A bare
    ``python3`` resolves through PATH instead — on macOS that is the system
    3.9, below the 3.11 floor and without pytest — so a perfectly good patch
    would "fail" its suite and the bake monitor would roll it back.
    """
    return [sys.executable, "-m", "pytest", "-q"]


def test_runner_problem(repo: Path) -> str | None:
    """Why the default suite cannot run here, or None if it can.

    The suite is the floor for both proposing and baking a repair. A missing
    test runner is an install defect, not a regression, and must never be
    scored as one. The default command runs under this very interpreter, so
    an in-process lookup answers for it exactly.
    """
    if importlib.util.find_spec("pytest") is None:
        return (
            f"pytest is not installed for {sys.executable}, so the self-repair "
            "loop cannot run the test suite. Install it with: "
            f"{sys.executable} -m pip install -e '{repo}[test]'"
        )
    return None


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


def _new_file_paths(patch: str) -> set[str]:
    new: set[str] = set()
    current: str | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git a/"):
            fields = line.split()
            current = fields[3][2:] if len(fields) == 4 else None
        elif line.startswith("new file mode") and current:
            new.add(current)
    return new


def validate_patch_paths(patch: str) -> list[str]:
    paths = patch_paths(patch)
    new_files = _new_file_paths(patch)
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized.startswith("/") or normalized == ".." or normalized.startswith("../") or "/../" in normalized:
            raise SelfRepairRefused(f"unsafe patch path: {path}")
        basename = normalized.rsplit("/", 1)[-1]
        if (
            normalized in PROTECTED_PATHS
            or basename in PROTECTED_BASENAMES
            or basename.startswith(PROTECTED_BASENAME_PREFIXES)
        ):
            raise SelfRepairRefused(f"protected patch path: {path}")
        if (normalized.startswith("tests/") or "/tests/" in normalized) and normalized not in new_files:
            # A repair may add a regression test; it may not edit or delete
            # the existing ones that judge it.
            raise SelfRepairRefused(f"protected patch path: {path} (existing tests are verification)")
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


# Bounded appetite (docs/self_repair_workorder.md §2.3): at most one open
# proposal at a time and one applied patch per week. The work order called
# these "shipped at these values"; until 2026-10-29 nothing enforced them.
APPLY_COOLDOWN_DAYS = 7
# A proposal the owner neither approved nor denied stops counting as open
# after this long, so an ignored confirmation cannot wedge the loop.
OPEN_PROPOSAL_STALE_DAYS = 14


def _proposal_reports(vault: Path) -> list[dict[str, Any]]:
    root = vault / "reports" / "self-repair-proposals"
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.md")) if root.exists() else []:
        try:
            out.append(dict(load_markdown(path).frontmatter))
        except Exception:
            continue
    return out


def _days_since(stamp: str, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(str(stamp)[:10])).days
    except ValueError:
        return None


def assert_appetite_for_proposal(vault: Path, *, today: date | None = None) -> None:
    today = today or date.today()
    for fm in _proposal_reports(vault):
        status = str(fm.get("status") or "")
        age = _days_since(str(fm.get("created") or ""), today)
        if status == "approved" or (status == "active" and age is not None and age < OPEN_PROPOSAL_STALE_DAYS):
            raise SelfRepairRefused(
                f"proposal {fm.get('id')} is still open; one open proposal at a time"
            )


def assert_appetite_for_apply(vault: Path, *, today: date | None = None) -> None:
    today = today or date.today()
    for fm in _proposal_reports(vault):
        if not fm.get("applied_at"):
            continue
        age = _days_since(str(fm.get("applied_at")), today)
        if age is not None and age < APPLY_COOLDOWN_DAYS:
            raise SelfRepairRefused(
                f"a patch was applied {age} day(s) ago ({fm.get('id')}); one applied patch per "
                f"{APPLY_COOLDOWN_DAYS} days so each is judged in isolation"
            )


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
    assert_appetite_for_proposal(vault)
    if test_command is None and (problem := test_runner_problem(repo)):
        raise SelfRepairRefused(problem)
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

        suite = _run(test_command or default_test_command(), cwd=worktree, timeout=1800)
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
        write_markdown(
            report,
            _proposal_report_frontmatter(
                proposal_id=proposal_id,
                loop_id=loop_id,
                patch_hash=patch_hash,
                base_commit=base_commit,
                worktree=str(worktree),
            ),
            body,
        )
        task_id = f"self-repair:{proposal_id}"
        confirmation = create_confirmation_for_task(
            vault, task_id=task_id,
            task_summary=f"Self-repair proposal {proposal_id}: {loop.get('summary') or loop_id}",
            planned_action=(f"Phase A proposal only; no live files will change. Review {report} and approve the exact "
                            f"proposal hash {patch_hash} with `approve <confirmation-id>`."),
            risk="The proposal is isolated and cannot apply changes in Phase A; protected paths are refused.",
            scope="self_repair", record_links=[f"report.{proposal_id}"], db_path=db_path,
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


def _proposal_report_frontmatter(
    *,
    proposal_id: str,
    loop_id: str,
    patch_hash: str,
    base_commit: str,
    worktree: str,
    record_date: str | None = None,
) -> dict[str, Any]:
    """Build a proposal report that satisfies the shared write boundary.

    ``report_kind`` preserves the operational subtype without abusing the
    episode-only ``source`` enum. Approval status does not grant authority;
    Phase B remains separately clamped by action policy.
    """
    today = record_date or date.today().isoformat()
    return {
        "id": f"report.{proposal_id}",
        "type": "report",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": "medium",
        "domain_primary": "competence",
        "domain_secondary": [],
        "privacy": "work",
        "summary": f"Self-repair proposal {proposal_id}",
        "links": [loop_id],
        "confidence": "high",
        "confidence_basis": "Isolated patch plus deterministic verification; execution still requires owner approval and policy authorization.",
        "last_confirmed": today,
        "review_after": today,
        "report_kind": "self_repair",
        "loop_id": loop_id,
        "proposal_hash": patch_hash,
        "base_commit": base_commit,
        "worktree": worktree,
    }


def read_proposal_patch(report_path: Path) -> tuple[str, list[str]]:
    """The exact diff and changed paths recorded in a proposal report —
    for a caller (the apply-notification) that wants the patch content
    without re-deriving the report's markdown layout."""
    doc = load_markdown(report_path)
    patch = _report_patch(doc.body)
    return patch, patch_paths(patch)


def find_loop_summary(vault: Path, loop_id: str) -> str | None:
    """Best-effort lookup of an open loop's human-written summary, by id,
    regardless of its status — used after apply, when the origin loop has
    already been marked resolved."""
    for path in sorted((vault / "open_loops").glob("*.md")):
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        if str(fm.get("id") or "") == loop_id:
            summary = fm.get("summary") or fm.get("description")
            return str(summary) if summary else None
    return None


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
    """Phase B: apply one independently-verified proposal as one local commit.

    Owner decision 2026-11-04: once the action-policy clamp is raised, a
    proposal that passed Phase A's independent verification applies without
    waiting on an owner confirmation — the clamp itself is the owner's
    permission. The default policy check remains blocked below tier 4. The
    injectable check exists for deterministic tests without weakening
    runtime policy.
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
    if str(fm.get("status") or "") not in {"active", "approved"}:
        raise SelfRepairRefused(f"proposal {proposal_id} is not open for apply (status={fm.get('status')!r})")
    assert_appetite_for_apply(vault)
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
        {"vault": str(vault), "proposal_id": proposal_id, "applied_commit": commit},
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
            narration=(f"{{{{self}}}} applied the independently-verified self-repair proposal "
                       f"{proposal_id} for {{{{principal}}}} as local commit {commit}, under the "
                       f"owner's standing tier-4 action-policy clamp (no per-apply confirmation)."),
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
        "restart_job_id": restart_job,
        **bake,
        "bake_check_job_id": bake_job,
    })
    write_markdown(report, fm, doc.body + f"\n## Applied\n\nLocal commit: `{commit}`\nRestart job: `{restart_job}`\n")
    # Direct Phase-B application has no Adjutant task-run wrapper, so close the
    # approved confirmation here. Otherwise the confirmation remains
    # resolution=approved/status=pending and can be polled again forever.
    from .adjutant_confirmations import mark_executed

    mark_executed(vault, f"self-repair:{proposal_id}", db_path=db_path)
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
    now = datetime.now(UTC)
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

    scheduled = datetime.now(UTC) + timedelta(hours=delay_hours)
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
    now = datetime.now(UTC)
    bake_end_str = str(fm.get("bake_end") or "")
    bake_start_str = str(fm.get("bake_start") or "")
    dimension = fm.get("targeted_dimension")
    pre_score = fm.get("bake_pre_score")
    extensions = int(fm.get("bake_extensions") or 0)

    try:
        bake_end = datetime.fromisoformat(bake_end_str)
        if bake_end.tzinfo is None:
            bake_end = bake_end.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        bake_end = now

    expired = now >= bake_end

    if test_command is None and (problem := test_runner_problem(repo)):
        # Neither pass nor fail: the patch is unjudged. Keep monitoring so the
        # check resumes once the runner is installed, and never roll back on it.
        _enqueue_bake_check(vault, proposal_id, db_path=db_path)
        return {
            "proposal_id": proposal_id,
            "check_time": now.isoformat(),
            "suite_ok": None,
            "verdict": "suite_unavailable",
            "error": problem,
        }

    suite = _run(test_command or default_test_command(), cwd=repo, timeout=1800)
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


# ── Upstream publication (WO-REPAIR-UPSTREAM, docs/self_repair_upstream_workorder.md) ──


def _find_loop_frontmatter(vault: Path, loop_id: str) -> dict[str, Any] | None:
    """Best-effort lookup of a loop record by id, regardless of status —
    unlike ``_loop_record``, which requires ``active``/``open``/``pending``.
    Publication can run after local apply has already resolved the loop, so
    it must not re-demand the loop still be open."""
    for path in sorted((vault / "open_loops").glob("*.md")):
        try:
            fm = dict(load_markdown(path).frontmatter)
        except Exception:
            continue
        if str(fm.get("id") or "") == loop_id:
            return fm
    return None


def publish_proposal_upstream(
    *,
    vault: Path,
    repo: Path,
    proposal_id: str,
    config: dict[str, Any] | None = None,
    db_path: Path | None = None,
    policy_check: Callable[[str, dict[str, Any]], bool] | None = None,
    run_command: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Open a verified Phase A proposal as a PR against a public upstream
    repo. Orthogonal to local apply (docs/self_repair_upstream_workorder.md
    §2.1): a proposal need not be applied locally to be published, and
    publishing never applies it. Gated at its own action-policy tier,
    separate from ``self_repair_apply``/``self_repair_rollback``.
    """
    from .action_policy import action_allowed
    from .public_rationale import assert_no_vault_leakage, build_public_rationale

    if config is None:
        from ..config import load_config
        config = load_config()
    check = policy_check or (lambda kind, cfg: action_allowed(kind, cfg))
    if not check("self_repair_publish_upstream", config):
        raise SelfRepairRefused("self-repair upstream publication is disabled by the action-policy clamp")

    proposal_id = str(proposal_id).strip()
    if not proposal_id or "/" in proposal_id or "\\" in proposal_id or ".." in proposal_id:
        raise SelfRepairRefused("invalid proposal id")
    report_path, fm, body = _load_proposal_report(vault, proposal_id)
    if fm.get("upstream"):
        raise SelfRepairRefused(f"proposal {proposal_id} was already published upstream")
    if str(fm.get("status") or "") not in {"active", "approved", "applied"}:
        raise SelfRepairRefused(
            f"proposal {proposal_id} is not eligible for upstream publication (status={fm.get('status')!r})"
        )

    loop_id = str(fm.get("loop_id") or "").strip()
    patch = _report_patch(body)
    paths = validate_patch_paths(patch)
    loop_fm = _find_loop_frontmatter(vault, loop_id) or {}
    deviation_class = str(loop_fm.get("deviation_class") or "").strip()
    if not deviation_class:
        raise SelfRepairRefused("upstream publication requires a deviation_class on the origin loop")

    upstream_cfg = dict(((config or {}).get("self_repair") or {}).get("upstream") or {})
    if not upstream_cfg.get("enabled"):
        raise SelfRepairRefused("self_repair.upstream.enabled is not set")
    upstream_repo = str(upstream_cfg.get("repo") or "").strip()
    if not upstream_repo:
        raise SelfRepairRefused("self_repair.upstream.repo is not configured")
    # Per-install, not hardcoded (decided 2026-11-09, §3.2): this install's own
    # configured credential/remote publishes under whatever account its own
    # owner authenticated — another install's config names its own account.
    remote = str(upstream_cfg.get("remote") or "origin").strip() or "origin"
    install_id = str(upstream_cfg.get("install_id") or "").strip()
    if not install_id:
        raise SelfRepairRefused("self_repair.upstream.install_id is not configured")

    rationale = build_public_rationale(
        deviation_class=deviation_class, changed_paths=paths, record_count=1, install_id=install_id,
    )
    assert_no_vault_leakage(rationale.title, db_path=db_path)
    assert_no_vault_leakage(rationale.body, db_path=db_path)

    runner = run_command or (lambda args: subprocess.run(args, text=True, capture_output=True, timeout=120))
    label = f"self-repair-{deviation_class}"

    search = runner([
        "gh", "pr", "list", "--repo", upstream_repo, "--state", "open",
        "--search", f"label:self-repair label:{label}", "--json", "number,url",
    ])
    if search.returncode == 0 and (search.stdout or "").strip() not in ("", "[]"):
        import json as _json
        try:
            existing = _json.loads(search.stdout)
        except ValueError:
            existing = []
        if existing:
            pr = existing[0]
            comment_body = (
                f"Corroborating independent verification from install-{install_id}: this "
                f"install's Phase A suite plus targeted probe also passed for a `{deviation_class}` "
                "proposal touching the same files."
            )
            assert_no_vault_leakage(comment_body, db_path=db_path)
            runner(["gh", "pr", "comment", str(pr.get("number")), "--repo", upstream_repo, "--body", comment_body])
            upstream_result = {
                "status": "corroborated", "pr_url": pr.get("url"), "pr_number": pr.get("number"),
                "published_at": date.today().isoformat(),
            }
            fm = dict(fm)
            fm["upstream"] = upstream_result
            write_markdown(report_path, fm, body)
            return upstream_result

    branch = f"self-repair/{proposal_id}"
    applied_commit = str(fm.get("applied_commit") or "").strip()
    base_commit = str(fm.get("base_commit") or "").strip()
    if not base_commit:
        raise SelfRepairRefused("proposal report is missing base_commit")

    cleanup_worktree: Path | None = None
    if applied_commit:
        push_ref, push_cwd = applied_commit, repo
    else:
        tmp_root = Path(tempfile.mkdtemp(prefix="lisan-self-repair-publish-"))
        worktree = tmp_root / proposal_id
        _git(repo, "worktree", "add", "--detach", str(worktree), base_commit, timeout=120)
        cleanup_worktree = worktree
        applied = subprocess.run(
            ["git", "apply", "--index", "-"], cwd=worktree, input=patch + "\n", text=True, capture_output=True
        )
        if applied.returncode:
            raise SelfRepairRefused(applied.stderr.strip() or "verified patch no longer applies cleanly for publication")
        commit_result = _run(
            ["git", "commit", "-m", f"self-repair: {proposal_id} ({deviation_class})"], cwd=worktree, timeout=120
        )
        if commit_result.returncode:
            raise SelfRepairRefused(commit_result.stderr.strip() or "self-repair publish commit failed")
        push_ref, push_cwd = _git(worktree, "rev-parse", "HEAD"), worktree

    try:
        push = runner(["git", "-C", str(push_cwd), "push", remote, f"{push_ref}:refs/heads/{branch}"])
        if push.returncode:
            raise SelfRepairRefused((push.stderr or "").strip() or "push to upstream remote failed")

        created = runner([
            "gh", "pr", "create", "--repo", upstream_repo, "--head", branch,
            "--title", rationale.title, "--body", rationale.body,
            "--label", "self-repair", "--label", label,
        ])
        if created.returncode:
            raise SelfRepairRefused((created.stderr or "").strip() or "gh pr create failed")
        pr_url = (created.stdout or "").strip().splitlines()[-1].strip() if (created.stdout or "").strip() else ""
    finally:
        if cleanup_worktree is not None:
            try:
                _git(repo, "worktree", "remove", "--force", str(cleanup_worktree), timeout=120)
            except Exception:
                pass
            shutil.rmtree(cleanup_worktree, ignore_errors=True)

    upstream_result = {
        "status": "published", "pr_url": pr_url, "branch": branch,
        "published_at": date.today().isoformat(),
    }
    fm = dict(fm)
    fm["upstream"] = upstream_result
    write_markdown(report_path, fm, body)
    return upstream_result
