from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lisan.frontmatter import load_markdown, write_markdown
from lisan.tools.adjutant_confirmations import approve_confirmation
from lisan.tools.jobs import list_jobs
from lisan.tools.self_repair import (
    SelfRepairRefused,
    _bake_metadata,
    _latest_dimension_score,
    apply_approved_proposal,
    propose,
    publish_proposal_upstream,
    rollback_applied_proposal,
    run_bake_check,
)


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


def _loop(vault: Path, *, deviation_class: str | None = None) -> Path:
    path = vault / "open_loops" / "loop.md"
    fm = {
        "id": "loop.self-repair-1", "type": "open_loop", "created": "2026-08-15",
        "updated": "2026-08-15", "status": "active", "origin": "self",
        "summary": "ordinary behavior needs repair", "significance": "medium",
    }
    if deviation_class:
        fm["deviation_class"] = deviation_class
    write_markdown(path, fm, "# Loop\n")
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
    confirmation = load_markdown(vault / "confirmations" / f"{proposal.confirmation_id.removeprefix('confirmation.')}.md")
    assert confirmation.frontmatter["status"] == "resolved"
    assert load_markdown(loop).frontmatter["resolved_by"] == "self_repair"
    episodes = list((vault / "self" / "episodes").glob("*self-repair-apply*.md"))
    assert len(episodes) == 1
    queued = [job for job in list_jobs(db_path=db) if job["id"] == applied.restart_job_id]
    assert queued and queued[0]["job_type"] == "self_repair.restart"


def test_phase_b_applies_without_owner_approval_under_tier_4(tmp_path: Path):
    """Owner decision 2026-11-04: once action_tier=4, a verified proposal
    applies on its own status ("active") — no `approve <id>` step required."""
    repo = _repo(tmp_path)
    vault = tmp_path / "vault"
    loop = _loop(vault)
    db = tmp_path / "index.sqlite"
    proposal = propose(
        vault=vault, repo=repo, loop_id="loop.self-repair-1", loop_path=loop,
        author=_author, db_path=db, test_command=["python3", "-c", "print('suite')"],
        targeted_command=["python3", "-c", "print('target')"], worktree_root=tmp_path / "worktrees",
    )
    assert load_markdown(proposal.report_path).frontmatter["status"] == "active"

    with pytest.raises(SelfRepairRefused, match="policy clamp"):
        apply_approved_proposal(
            vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
            config={"drive": {"action_tier": 3}},
        )

    applied = apply_approved_proposal(
        vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
        config={"drive": {"action_tier": 4}},
    )
    assert applied.commit
    assert (repo / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert load_markdown(proposal.report_path).frontmatter["status"] == "applied"
    # The Phase A confirmation was never approved, and apply does not need it.
    confirmation = load_markdown(vault / "confirmations" / f"{proposal.confirmation_id.removeprefix('confirmation.')}.md")
    assert confirmation.frontmatter["status"] == "pending"


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


def _diff(path: str, *, new: bool = False) -> str:
    mode = "new file mode 100644\n--- /dev/null\n" if new else f"--- a/{path}\n"
    return f"diff --git a/{path} b/{path}\n{mode}+++ b/{path}\n@@ -0,0 +1 @@\n+x\n"


@pytest.mark.parametrize("path", [
    "lisan/tools/authority.py",
    "lisan/tools/intent.py",
    "lisan/tools/firewall.py",
    "lisan/tools/judge.py",
    "lisan/tools/purge.py",
    "lisan/tools/self_repair_helpers.py",
    "lisan/tools/public_rationale.py",
    "tests/test_mail.py",
    ".github/workflows/ci.yml",
])
def test_the_mechanisms_that_judge_or_bound_a_repair_are_protected(path):
    from lisan.tools.self_repair import validate_patch_paths

    with pytest.raises(SelfRepairRefused, match="protected"):
        validate_patch_paths(_diff(path))


def test_a_repair_may_add_a_new_regression_test():
    from lisan.tools.self_repair import validate_patch_paths

    assert validate_patch_paths(_diff("tests/test_new_regression.py", new=True)) == ["tests/test_new_regression.py"]
    assert validate_patch_paths(_diff("lisan/tools/ordinary.py")) == ["lisan/tools/ordinary.py"]


def _proposal_report(vault: Path, name: str, **fm):
    from lisan.frontmatter import write_markdown

    root = vault / "reports" / "self-repair-proposals"
    root.mkdir(parents=True, exist_ok=True)
    write_markdown(root / f"{name}.md", {"id": f"report.{name}", **fm}, "x")


def test_one_open_proposal_at_a_time(tmp_path: Path):
    from datetime import date

    from lisan.tools.self_repair import assert_appetite_for_proposal

    vault = tmp_path / "vault"
    _proposal_report(vault, "sr-old", status="active", created="2026-01-01")
    assert_appetite_for_proposal(vault, today=date(2026, 10, 29))  # stale: ignored
    _proposal_report(vault, "sr-new", status="active", created="2026-10-28")
    with pytest.raises(SelfRepairRefused, match="one open proposal"):
        assert_appetite_for_proposal(vault, today=date(2026, 10, 29))


def test_one_applied_patch_per_week(tmp_path: Path):
    from datetime import date

    from lisan.tools.self_repair import assert_appetite_for_apply

    vault = tmp_path / "vault"
    _proposal_report(vault, "sr-a", status="applied", applied_at="2026-10-25")
    with pytest.raises(SelfRepairRefused, match="one applied patch"):
        assert_appetite_for_apply(vault, today=date(2026, 10, 29))
    assert_appetite_for_apply(vault, today=date(2026, 11, 2))


# ── Test runner: interpreter and availability ───────────────────────────────


def test_default_test_command_uses_running_interpreter(monkeypatch):
    # A bare "python3" resolves through PATH (on macOS: the system 3.9 without
    # pytest), which would fail a good patch's suite and trigger a rollback.
    import sys

    from lisan.tools import self_repair

    monkeypatch.setattr(sys, "executable", "/opt/lisan/venv/bin/python")
    assert self_repair.default_test_command() == ["/opt/lisan/venv/bin/python", "-m", "pytest", "-q"]


def _no_pytest(monkeypatch):
    import importlib.util

    from lisan.tools import self_repair

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        self_repair.importlib.util, "find_spec",
        lambda name, *a, **k: None if name == "pytest" else real_find_spec(name, *a, **k),
    )


def test_propose_refuses_before_authoring_when_pytest_missing(tmp_path: Path, monkeypatch):
    _no_pytest(monkeypatch)
    repo = _repo(tmp_path)
    vault = tmp_path / "vault"
    loop = _loop(vault)
    authored: list[str] = []

    with pytest.raises(SelfRepairRefused, match="pytest is not installed"):
        propose(
            vault=vault, repo=repo, loop_id="loop.self-repair-1", loop_path=loop,
            author=lambda prompt: authored.append(prompt) or _author(prompt),
            db_path=tmp_path / "index.sqlite", worktree_root=tmp_path / "worktrees",
        )
    assert authored == []  # no LLM call spent on a proposal that cannot be verified
    assert not (tmp_path / "worktrees").exists()


def test_bake_check_missing_pytest_is_not_a_regression(tmp_path: Path, monkeypatch):
    repo, vault, db, proposal, applied = _applied_setup(tmp_path)
    report_path = vault / "reports" / "self-repair-proposals" / f"{proposal.proposal_id}.md"
    _no_pytest(monkeypatch)

    result = run_bake_check(
        vault, proposal.proposal_id, repo=repo, db_path=db,
        config={"drive": {"action_tier": 4}},  # rollback would be allowed
    )
    assert result["verdict"] == "suite_unavailable"
    assert result["suite_ok"] is None
    assert "rolled_back" not in result
    assert (repo / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert load_markdown(report_path).frontmatter["bake_status"] == "monitoring"
    bake_jobs = [j for j in list_jobs(db_path=db) if j["job_type"] == "self_repair.bake_check"]
    assert bake_jobs and bake_jobs[0]["status"] == "queued"


# ── Upstream publication (WO-REPAIR-UPSTREAM) ────────────────────────────────


def _publish_setup(tmp_path: Path, *, deviation_class: str = "thin"):
    repo = _repo(tmp_path)
    vault = tmp_path / "vault"
    loop = _loop(vault, deviation_class=deviation_class)
    db = tmp_path / "index.sqlite"
    proposal = propose(
        vault=vault, repo=repo, loop_id="loop.self-repair-1", loop_path=loop,
        author=_author, db_path=db, test_command=["python3", "-c", "print('suite')"],
        targeted_command=["python3", "-c", "print('target')"], worktree_root=tmp_path / "worktrees",
    )
    return repo, vault, db, proposal


def _upstream_config(**overrides) -> dict:
    upstream = {"enabled": True, "repo": "owner/repo", "remote": "origin", "install_id": "anon-1"}
    upstream.update(overrides)
    return {"self_repair": {"upstream": upstream}}


def test_publish_upstream_requires_policy_tier(tmp_path: Path):
    repo, vault, db, proposal = _publish_setup(tmp_path)
    with pytest.raises(SelfRepairRefused, match="action-policy clamp"):
        publish_proposal_upstream(
            vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
            config={**_upstream_config(), "drive": {"action_tier": 0}},
        )


def test_publish_upstream_requires_upstream_config(tmp_path: Path):
    repo, vault, db, proposal = _publish_setup(tmp_path)
    with pytest.raises(SelfRepairRefused, match="enabled"):
        publish_proposal_upstream(
            vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
            policy_check=lambda *_: True, config={"self_repair": {}},
        )


def test_publish_upstream_requires_deviation_class_on_origin_loop(tmp_path: Path):
    repo, vault, db, proposal = _publish_setup(tmp_path, deviation_class="")
    with pytest.raises(SelfRepairRefused, match="deviation_class"):
        publish_proposal_upstream(
            vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
            policy_check=lambda *_: True, config=_upstream_config(),
        )


def test_publish_upstream_corroborates_existing_pr_instead_of_duplicating(tmp_path: Path):
    repo, vault, db, proposal = _publish_setup(tmp_path)
    calls: list[list[str]] = []

    def fake_run(args: list[str]) -> subprocess.CompletedProcess:
        calls.append(args)
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(
                args, 0, stdout='[{"number": 42, "url": "https://github.com/owner/repo/pull/42"}]', stderr="",
            )
        if args[:3] == ["gh", "pr", "comment"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected call in corroboration path: {args}")

    result = publish_proposal_upstream(
        vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
        policy_check=lambda *_: True, config=_upstream_config(), run_command=fake_run,
    )

    assert result["status"] == "corroborated"
    assert result["pr_number"] == 42
    assert any(c[:3] == ["gh", "pr", "comment"] for c in calls)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)
    fm = load_markdown(proposal.report_path).frontmatter
    assert fm["upstream"]["status"] == "corroborated"


def test_publish_upstream_pushes_branch_and_opens_pr(tmp_path: Path):
    repo, vault, db, proposal = _publish_setup(tmp_path)
    calls: list[list[str]] = []

    def fake_run(args: list[str]) -> subprocess.CompletedProcess:
        calls.append(args)
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")
        if args[0] == "git" and "push" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/owner/repo/pull/99\n", stderr="")
        raise AssertionError(f"unexpected call in publish path: {args}")

    result = publish_proposal_upstream(
        vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
        policy_check=lambda *_: True, config=_upstream_config(), run_command=fake_run,
    )

    assert result["status"] == "published"
    assert result["pr_url"] == "https://github.com/owner/repo/pull/99"
    assert result["branch"] == f"self-repair/{proposal.proposal_id}"
    push_calls = [c for c in calls if c[0] == "git" and "push" in c]
    assert len(push_calls) == 1
    fm = load_markdown(proposal.report_path).frontmatter
    assert fm["upstream"]["pr_url"] == "https://github.com/owner/repo/pull/99"
    # The live checkout is never touched by publication; apply remains a
    # separate, orthogonal action (docs/self_repair_upstream_workorder.md §2.1).
    assert (repo / "ordinary.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_publish_upstream_refuses_once_already_published(tmp_path: Path):
    repo, vault, db, proposal = _publish_setup(tmp_path)

    def fake_run(args: list[str]) -> subprocess.CompletedProcess:
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")
        if args[0] == "git" and "push" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/owner/repo/pull/99\n", stderr="")
        raise AssertionError(f"unexpected call: {args}")

    publish_proposal_upstream(
        vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
        policy_check=lambda *_: True, config=_upstream_config(), run_command=fake_run,
    )
    with pytest.raises(SelfRepairRefused, match="already published"):
        publish_proposal_upstream(
            vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
            policy_check=lambda *_: True, config=_upstream_config(), run_command=fake_run,
        )


def test_publish_upstream_refuses_when_rationale_matches_vault_index(tmp_path: Path):
    import sqlite3

    from lisan.tools.rebuild_index import ensure_index_schema

    repo, vault, db, proposal = _publish_setup(tmp_path, deviation_class="thin")
    conn = sqlite3.connect(db)
    ensure_index_schema(conn)
    # Plant an alias that collides with the deviation class name itself, so
    # the independent denylist scan refuses even though build_public_rationale
    # never touched any entity data — it does not trust the first pass.
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias, context) VALUES (?, ?, ?)",
        ("entity.agent.thin", "thin", None),
    )
    conn.commit()
    conn.close()

    def fake_run(args: list[str]) -> subprocess.CompletedProcess:
        raise AssertionError(f"no gh/git call should happen before the leakage check: {args}")

    with pytest.raises(SelfRepairRefused, match="vault-index term"):
        publish_proposal_upstream(
            vault=vault, repo=repo, proposal_id=proposal.proposal_id, db_path=db,
            policy_check=lambda *_: True, config=_upstream_config(), run_command=fake_run,
        )
