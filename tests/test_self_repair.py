from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lisan.frontmatter import write_markdown
from lisan.tools.self_repair import SelfRepairRefused, propose


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
