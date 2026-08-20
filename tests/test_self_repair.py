from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lisan.frontmatter import load_markdown, write_markdown
from lisan.tools.self_repair import (
    SelfRepairRefused,
    apply_approved_proposal,
    propose,
    rollback_applied_proposal,
    run_bake_check,
    _bake_metadata,
    _latest_dimension_score,
    _targeted_dimension,
)
from lisan.tools.adjutant_confirmations import approve_confirmation
from lisan.tools.jobs import list_jobs


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "ordinary.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _loop(vault: Path) -> Path:
    path = vault / "open_loops" / "loop.md"
    write_markdown(path, {
        "id": "loop.self-repair-1", "type": "open_loop", "created": "2026-08-15",
        "updated": "2026-08-15", "status": "active", "origin": "self",
        "summary": "ordinary behavior needs repair", "significance": "medium",
    }, "# Loop\n")
    return path


def _author(_prompt: str) -> str:
    return """diff --git a/ordinary.py b/ordinary.py
index d2c3f3a..f4e5f6a 100644
--- a/ordinary.py
+++ b/ordinary.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""


def test_phase_a_creates_isolated_verified_proposal_and_confirmation(tmp_path: Path):
    repo = _repo(tmp_path)
    vault = tmp_path / "vault"
    loop = _loop(vault)
    db = tmp_path / "index.sqlite"

    proposal = propose(
        vault=vault, repo=repo, loop_id="loop.self-repair-1", loop_path=loop,
        author=_author, db_path=db, test_command=["python3", "-c", "print('suite')"],
        targeted_command=["python3", "-c", "print('target')"], worktree_root=tmp_path / "worktrees",
    )

    assert proposal.patch_hash
    assert proposal.worktree.exists()
    assert proposal.report_path.exists()
    assert proposal.confirmation_id and proposal.confirmation_id.startswith("confirmation.")
    assert (repo / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (proposal.worktree / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert "Phase A only" in proposal.telegram_message

    approve_confirmation(proposal.report_path.parents[2], proposal.confirmation_id, db_path=db, capture=lambda **_: None)
    assert load_markdown(proposal.report_path).frontmatter["status"] == "approved"


def test_phase_a_refuses_dirty_checkout(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "ordinary.py").write_text("VALUE = 99\n", encoding="utf-8")
    vault = tmp_path / "vault"
    loop = _loop(vault)

    with pytest.raises(SelfRepairRefused, match="dirty"):
        propose(vault=vault, repo=repo, loop_id="loop.self-repair-1", loop_path=loop, author=_author)


def test_phase_a_refuses_protected_paths(tmp_path: Path):
    repo = _repo(tmp_path)
    vault = tmp_path / "vault"
    loop = _loop(vault)

    def author(_prompt: str) -> str:
        return _author(_prompt).replace("ordinary.py", "action_policy.py")

    with pytest.raises(SelfRepairRefused, match="protected"):
        propose(vault=vault, repo=repo, loop_id="loop.self-repair-1", loop_path=loop, author=author)


def test_phase_a_requires_independent_verifier(tmp_path: Path):
    repo = _repo(tmp_path)
    vault = tmp_path / "vault"
    loop = _loop(vault)

    with pytest.raises(SelfRepairRefused, match="independent"):
        propose(
            vault=vault, repo=repo, loop_id="loop.self-repair-1", loop_path=loop,
            author=_author, author_id="same", verifier_id="same",
        )


def test_phase_a_refuses_unverified_patch(tmp_path: Path):
    repo = _repo(tmp_path)
    vault = tmp_path / "vault"
    loop = _loop(vault)

    with pytest.raises(SelfRepairRefused, match="verification"):
        propose(
            vault=vault, repo=repo, loop_id="loop.self-repair-1", loop_path=loop,
            author=_author, verifier=lambda result: {"ok": False},
            test_command=["python3", "-c", "print('suite')"],
        )


def test_loop_lookup_skips_older_resolved_duplicate_id(tmp_path: Path):
    vault = tmp_path / "vault"
    old = vault / "open_loops" / "old.md"
    write_markdown(old, {
        "id": "loop.self-repair-duplicate", "type": "open_loop", "created": "2026-08-01",
        "updated": "2026-08-01", "status": "resolved", "origin": "self",
    }, "old")
    current = vault / "open_loops" / "current.md"
    write_markdown(current, {
        "id": "loop.self-repair-duplicate", "type": "open_loop", "created": "2026-08-02",
        "updated": "2026-08-02", "status": "active", "origin": "self",
    }, "current")
    from lisan.tools.self_repair import _loop_record

    path, _ = _loop_record(vault, "loop.self-repair-duplicate", None)
    assert path == current


def test_phase_b_applies_exact_approved_proposal_and_queues_restart(tmp_path: Path):
    repo = _repo(tmp_path)
    vault = tmp_path / "vault"
    loop = _loop(vault)
    db = tmp_path / "index.sqlite"
    proposal = propose(
        vault=vault, repo=repo, loop_id="loop.self-repair-1", loop_path=loop,
        author=_author, db_path=db, test_command=["python3", "-c", "print('suite')"],
        targeted_command=["python3", "-c", "print('target')"], worktree_root=tmp_path / "worktrees",
    )
    approve_confirmation(vault, proposal.confirmation_id, db_path=db, capture=lambda **_: None)

    with pytest.raises(SelfRepairRefused, match="policy clamp"):
        apply_approved_proposal(
            vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
            config={"drive": {"action_tier": 3}},
        )

    # Reports created before Phase B did not duplicate base/worktree metadata
    # in frontmatter; the body and conventional worktree path remain enough.
    approved_doc = load_markdown(proposal.report_path)
    legacy_fm = dict(approved_doc.frontmatter)
    legacy_fm.pop("base_commit", None)
    legacy_fm.pop("worktree", None)
    write_markdown(proposal.report_path, legacy_fm, approved_doc.body)
    applied = apply_approved_proposal(
        vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
        worktree_root=tmp_path / "worktrees",
        policy_check=lambda _kind, _config: True,
    )
    assert applied.commit
    assert (repo / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert load_markdown(proposal.report_path).frontmatter["status"] == "applied"
    assert load_markdown(loop).frontmatter["resolved_by"] == "self_repair"
    episodes = list((vault / "self" / "episodes").glob("*self-repair-apply*.md"))
    assert len(episodes) == 1
    queued = [job for job in list_jobs(db_path=db) if job["id"] == applied.restart_job_id]
    assert queued and queued[0]["job_type"] == "self_repair.restart"


def test_phase_b_refuses_changed_approved_report(tmp_path: Path):
    repo = _repo(tmp_path)
    vault = tmp_path / "vault"
    loop = _loop(vault)
    db = tmp_path / "index.sqlite"
    proposal = propose(
        vault=vault, repo=repo, loop_id="loop.self-repair-1", loop_path=loop,
        author=_author, db_path=db, test_command=["python3", "-c", "print('suite')"],
        targeted_command=["python3", "-c", "print('target')"], worktree_root=tmp_path / "worktrees",
    )
    approve_confirmation(vault, proposal.confirmation_id, db_path=db, capture=lambda **_: None)
    doc = load_markdown(proposal.report_path)
    write_markdown(proposal.report_path, doc.frontmatter, doc.body.replace("VALUE = 2", "VALUE = 99"))
    with pytest.raises(SelfRepairRefused, match="hash"):
        apply_approved_proposal(
            vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
            policy_check=lambda _kind, _config: True,
        )
    assert (repo / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 1\n"


# ── Phase C helpers ──────────────────────────────────────────────────────────


def _self_eval_loop(vault: Path) -> Path:
    """An origin:self loop from a self-eval dimension finding."""
    path = vault / "open_loops" / "dim-loop.md"
    write_markdown(path, {
        "id": "loop.dim-warmth", "type": "open_loop", "created": "2026-08-15",
        "updated": "2026-08-15", "status": "active", "origin": "self",
        "deviation_fingerprint": "self-eval-dim-warmth",
        "deviation_class": "self_eval",
        "summary": "warmth quality is slipping", "significance": "medium",
    }, "# Loop\n")
    return path


def _applied_setup(tmp_path: Path, *, use_dim_loop: bool = False):
    """Create a repo + vault with a fully applied proposal. Returns
    (repo, vault, db, proposal, applied)."""
    repo = _repo(tmp_path)
    vault = tmp_path / "vault"
    if use_dim_loop:
        loop = _self_eval_loop(vault)
        loop_id = "loop.dim-warmth"
    else:
        loop = _loop(vault)
        loop_id = "loop.self-repair-1"
    db = tmp_path / "index.sqlite"
    proposal = propose(
        vault=vault, repo=repo, loop_id=loop_id, loop_path=loop,
        author=_author, db_path=db,
        test_command=["python3", "-c", "print('suite')"],
        targeted_command=["python3", "-c", "print('target')"],
        worktree_root=tmp_path / "worktrees",
    )
    approve_confirmation(vault, proposal.confirmation_id, db_path=db, capture=lambda **_: None)
    applied = apply_approved_proposal(
        vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
        worktree_root=tmp_path / "worktrees",
        policy_check=lambda _kind, _config: True,
    )
    return repo, vault, db, proposal, applied


def _write_history(vault: Path, entries: list[dict]) -> None:
    import json
    path = vault / "reports" / "self-eval-history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


# ── Phase C: bake metadata ──────────────────────────────────────────────────


def test_bake_metadata_extracts_targeted_dimension(tmp_path: Path):
    vault = tmp_path / "vault"
    _self_eval_loop(vault)
    _write_history(vault, [
        {"date": "2026-08-10", "dimensions": {"warmth": {"mean": 4.2, "n": 5}}},
    ])
    meta = _bake_metadata(vault, "loop.dim-warmth", "abc123", "def456")
    assert meta["targeted_dimension"] == "warmth"
    assert meta["bake_pre_score"] == 4.2
    assert meta["bake_status"] == "monitoring"
    assert meta["bake_base_commit"] == "def456"
    assert meta["bake_applied_commit"] == "abc123"


def test_bake_metadata_handles_non_dimension_loop(tmp_path: Path):
    vault = tmp_path / "vault"
    _loop(vault)
    meta = _bake_metadata(vault, "loop.self-repair-1", "abc", "def")
    assert meta["targeted_dimension"] is None
    assert meta["bake_pre_score"] is None


def test_latest_dimension_score_filters_by_date(tmp_path: Path):
    vault = tmp_path / "vault"
    _write_history(vault, [
        {"date": "2026-08-01", "dimensions": {"warmth": {"mean": 4.0, "n": 5}}},
        {"date": "2026-08-10", "dimensions": {"warmth": {"mean": 3.5, "n": 5}}},
        {"date": "2026-08-20", "dimensions": {"warmth": {"mean": 3.0, "n": 5}}},
    ])
    assert _latest_dimension_score(vault, "warmth") == 3.0
    assert _latest_dimension_score(vault, "warmth", after="2026-08-10") == 3.0
    assert _latest_dimension_score(vault, "warmth", after="2026-08-20") is None
    assert _latest_dimension_score(vault, "nonexistent") is None


# ── Phase C: bake check ─────────────────────────────────────────────────────


def test_bake_check_passes_after_bake_period(tmp_path: Path):
    repo, vault, db, proposal, applied = _applied_setup(tmp_path)
    report_path = vault / "reports" / "self-repair-proposals" / f"{proposal.proposal_id}.md"
    doc = load_markdown(report_path)
    fm = dict(doc.frontmatter)
    fm["bake_end"] = "2020-01-01T00:00:00+00:00"
    write_markdown(report_path, fm, doc.body)

    result = run_bake_check(
        vault, proposal.proposal_id, repo=repo, db_path=db,
        test_command=["python3", "-c", "print('ok')"],
    )
    assert result["verdict"] == "passed"
    assert load_markdown(report_path).frontmatter["bake_status"] == "passed"
    episodes = list((vault / "self" / "episodes").glob("*bake-passed*"))
    assert len(episodes) == 1


def test_bake_check_detects_suite_regression(tmp_path: Path):
    repo, vault, db, proposal, applied = _applied_setup(tmp_path)

    result = run_bake_check(
        vault, proposal.proposal_id, repo=repo, db_path=db,
        test_command=["python3", "-c", "import sys; sys.exit(1)"],
        config={"drive": {"action_tier": 4}},
    )
    assert result["verdict"] == "regression"
    assert result["suite_ok"] is False
    assert result["rolled_back"] is True
    assert (repo / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_bake_check_detects_score_regression(tmp_path: Path):
    repo, vault, db, proposal, applied = _applied_setup(tmp_path, use_dim_loop=True)
    report_path = vault / "reports" / "self-repair-proposals" / f"{proposal.proposal_id}.md"

    doc = load_markdown(report_path)
    fm = dict(doc.frontmatter)
    fm["bake_pre_score"] = 4.2
    write_markdown(report_path, fm, doc.body)

    _write_history(vault, [
        {"date": "2099-01-01", "dimensions": {"warmth": {"mean": 3.2, "n": 5}}},
    ])

    result = run_bake_check(
        vault, proposal.proposal_id, repo=repo, db_path=db,
        test_command=["python3", "-c", "print('ok')"],
        config={"drive": {"action_tier": 4}},
    )
    assert result["verdict"] == "regression"
    assert result["score_drop"] >= 0.5
    assert result["rolled_back"] is True


def test_bake_check_inconclusive_extends_period(tmp_path: Path):
    repo, vault, db, proposal, applied = _applied_setup(tmp_path, use_dim_loop=True)
    report_path = vault / "reports" / "self-repair-proposals" / f"{proposal.proposal_id}.md"
    doc = load_markdown(report_path)
    fm = dict(doc.frontmatter)
    fm["bake_end"] = "2020-01-01T00:00:00+00:00"
    write_markdown(report_path, fm, doc.body)

    result = run_bake_check(
        vault, proposal.proposal_id, repo=repo, db_path=db,
        test_command=["python3", "-c", "print('ok')"],
    )
    assert result["verdict"] == "inconclusive"
    updated = load_markdown(report_path).frontmatter
    assert updated["bake_extensions"] == 1
    assert updated["bake_status"] == "monitoring"


def test_bake_check_ok_reschedules(tmp_path: Path):
    repo, vault, db, proposal, applied = _applied_setup(tmp_path)

    result = run_bake_check(
        vault, proposal.proposal_id, repo=repo, db_path=db,
        test_command=["python3", "-c", "print('ok')"],
    )
    assert result["verdict"] == "ok"
    bake_jobs = [j for j in list_jobs(db_path=db) if j["job_type"] == "self_repair.bake_check"]
    assert len(bake_jobs) >= 1
    assert bake_jobs[0]["status"] == "queued"


# ── Phase C: rollback ───────────────────────────────────────────────────────


def test_rollback_reverts_exact_commit(tmp_path: Path):
    repo, vault, db, proposal, applied = _applied_setup(tmp_path)
    assert (repo / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    result = rollback_applied_proposal(
        vault, proposal.proposal_id, repo=repo, db_path=db,
        policy_check=lambda _k, _c: True,
    )
    assert result["rolled_back"] is True
    assert (repo / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    fm = load_markdown(proposal.report_path).frontmatter
    assert fm["bake_status"] == "rolled_back"
    assert fm["rollback_commit"] == result["rollback_commit"]


def test_rollback_refuses_when_owner_commits_on_top(tmp_path: Path):
    repo, vault, db, proposal, applied = _applied_setup(tmp_path)
    (repo / "owner_file.txt").write_text("owner work\n", encoding="utf-8")
    _git(repo, "add", "owner_file.txt")
    _git(repo, "commit", "-m", "owner commit")

    with pytest.raises(SelfRepairRefused, match="owner commit"):
        rollback_applied_proposal(
            vault, proposal.proposal_id, repo=repo, db_path=db,
            policy_check=lambda _k, _c: True,
        )
    assert (repo / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_rollback_reopens_origin_loop(tmp_path: Path):
    repo, vault, db, proposal, applied = _applied_setup(tmp_path)
    loop_path = vault / "open_loops" / "loop.md"
    assert load_markdown(loop_path).frontmatter["status"] == "resolved"

    rollback_applied_proposal(
        vault, proposal.proposal_id, repo=repo, db_path=db,
        policy_check=lambda _k, _c: True,
    )
    fm = load_markdown(loop_path).frontmatter
    assert fm["status"] == "active"
    assert "resolved_by" not in fm


def test_rollback_emits_self_episode(tmp_path: Path):
    repo, vault, db, proposal, applied = _applied_setup(tmp_path)
    rollback_applied_proposal(
        vault, proposal.proposal_id, repo=repo, db_path=db,
        policy_check=lambda _k, _c: True,
    )
    episodes = list((vault / "self" / "episodes").glob("*rollback*"))
    assert len(episodes) == 1


def test_rollback_requires_policy(tmp_path: Path):
    repo, vault, db, proposal, applied = _applied_setup(tmp_path)
    with pytest.raises(SelfRepairRefused, match="action-policy"):
        rollback_applied_proposal(
            vault, proposal.proposal_id, repo=repo, db_path=db,
            policy_check=lambda _k, _c: False,
        )


# ── Phase C: full lifecycle proof ────────────────────────────────────────────


def test_full_bake_regression_lifecycle(tmp_path: Path):
    """The proof required by the work order: a deliberately bad patch is
    applied, the bake check catches the regression, and rollback restores
    the known-good state automatically."""
    repo, vault, db, proposal, applied = _applied_setup(tmp_path)
    assert (repo / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    result = run_bake_check(
        vault, proposal.proposal_id, repo=repo, db_path=db,
        test_command=["python3", "-c", "import sys; sys.exit(1)"],
        config={"drive": {"action_tier": 4}},
    )

    assert result["verdict"] == "regression"
    assert result["rolled_back"] is True
    assert (repo / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    fm = load_markdown(proposal.report_path).frontmatter
    assert fm["bake_status"] == "rolled_back"
    assert fm.get("rollback_commit")

    loop_fm = load_markdown(vault / "open_loops" / "loop.md").frontmatter
    assert loop_fm["status"] == "active"

    rollback_episodes = list((vault / "self" / "episodes").glob("*rollback*"))
    assert len(rollback_episodes) == 1
