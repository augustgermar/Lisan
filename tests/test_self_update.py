"""Auto-update: periodic check for a newer remote commit, with two owner
postures (automatic / notify). Exercises real tiny git repos (a bare
"remote" plus clones) rather than mocking git, since the whole point is
fast-forward-only semantics, dirty-tree and diverged-branch refusals, and
rollback on a failing verification — behavior that is easy to get subtly
wrong with a mocked subprocess."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.tools.self_update import (
    apply_update,
    check_remote,
    ensure_self_update_job,
    read_update_state,
    render_update_status,
    run_periodic_check,
    set_update_mode,
)
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.jobs import enqueue_job


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_remote_and_clone(tmp: Path) -> tuple[Path, Path]:
    remote = tmp / "remote.git"
    local = tmp / "local"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", "--quiet", str(remote), str(local)], check=True)
    _git(local, ["config", "user.email", "test@example.com"])
    _git(local, ["config", "user.name", "Test"])
    (local / "README.md").write_text("v1\n")
    _git(local, ["add", "README.md"])
    _git(local, ["commit", "--quiet", "-m", "v1"])
    _git(local, ["branch", "-M", "main"])
    _git(local, ["push", "--quiet", "-u", "origin", "main"])
    return remote, local


def _push_new_commit(remote: Path, tmp: Path, name: str, filename: str = "README.md") -> str:
    """A second clone commits and pushes, simulating someone else's work
    landing on the remote while ``local`` sits still."""
    other = tmp / name
    subprocess.run(["git", "clone", "--quiet", str(remote), str(other)], check=True)
    _git(other, ["checkout", "main"])  # bare remote's symbolic HEAD never followed the push
    _git(other, ["config", "user.email", "test@example.com"])
    _git(other, ["config", "user.name", "Test"])
    (other / filename).write_text(f"{name}\n")
    _git(other, ["add", filename])
    _git(other, ["commit", "--quiet", "-m", name])
    _git(other, ["push", "--quiet", "origin", "main"])
    return _git(other, ["rev-parse", "HEAD"]).stdout.strip()


class CheckRemoteTests(unittest.TestCase):
    def test_up_to_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, local = _init_remote_and_clone(Path(tmp))
            status = check_remote(local, remote="origin", branch="main")
        self.assertTrue(status["ok"])
        self.assertFalse(status["update_available"])
        self.assertEqual(status["behind"], 0)

    def test_reports_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote, local = _init_remote_and_clone(tmp_path)
            new_head = _push_new_commit(remote, tmp_path, "other")
            status = check_remote(local, remote="origin", branch="main")
        self.assertTrue(status["ok"])
        self.assertTrue(status["update_available"])
        self.assertFalse(status["diverged"])
        self.assertEqual(status["behind"], 1)
        self.assertEqual(status["remote"], new_head)

    def test_reports_diverged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote, local = _init_remote_and_clone(tmp_path)
            _push_new_commit(remote, tmp_path, "other")
            (local / "local-only.txt").write_text("local work\n")
            _git(local, ["add", "local-only.txt"])
            _git(local, ["commit", "--quiet", "-m", "unpushed local work"])
            status = check_remote(local, remote="origin", branch="main")
        self.assertTrue(status["ok"])
        self.assertTrue(status["update_available"])
        self.assertTrue(status["diverged"])

    def test_fetch_failure_is_reported_not_raised(self):
        status = check_remote(Path("/nonexistent/repo/path"), remote="origin", branch="main")
        self.assertFalse(status["ok"])
        self.assertIn("error", status)


class ApplyUpdateTests(unittest.TestCase):
    def test_fast_forwards_and_runs_injected_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote, local = _init_remote_and_clone(tmp_path)
            new_head = _push_new_commit(remote, tmp_path, "other")
            calls = []
            result = apply_update(
                local, remote="origin", branch="main", restart=False,
                test_runner=lambda repo, python: (calls.append(("tests", repo)), {"ok": True})[1],
                dependency_installer=lambda repo, python: (calls.append(("deps", repo)), {"ok": True})[1],
            )
            self.assertEqual(_git(local, ["rev-parse", "HEAD"]).stdout.strip(), new_head)
        self.assertTrue(result["applied"])
        self.assertEqual(result["to"], new_head)
        self.assertEqual([c[0] for c in calls], ["tests"])  # README.md change doesn't touch deps

    def test_refuses_dirty_working_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote, local = _init_remote_and_clone(tmp_path)
            _push_new_commit(remote, tmp_path, "other")
            (local / "README.md").write_text("uncommitted edit\n")
            result = apply_update(local, remote="origin", branch="main", restart=False)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "dirty_working_tree")

    def test_refuses_diverged_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote, local = _init_remote_and_clone(tmp_path)
            _push_new_commit(remote, tmp_path, "other")
            (local / "local-only.txt").write_text("local work\n")
            _git(local, ["add", "local-only.txt"])
            _git(local, ["commit", "--quiet", "-m", "unpushed"])
            before = _git(local, ["rev-parse", "HEAD"]).stdout.strip()
            result = apply_update(local, remote="origin", branch="main", restart=False)
            self.assertEqual(_git(local, ["rev-parse", "HEAD"]).stdout.strip(), before)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "diverged")

    def test_up_to_date_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, local = _init_remote_and_clone(Path(tmp))
            result = apply_update(local, remote="origin", branch="main", restart=False)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "up_to_date")

    def test_rolls_back_on_failing_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote, local = _init_remote_and_clone(tmp_path)
            before = _git(local, ["rev-parse", "HEAD"]).stdout.strip()
            _push_new_commit(remote, tmp_path, "other")
            result = apply_update(
                local, remote="origin", branch="main", restart=False,
                test_runner=lambda repo, python: {"ok": False, "tail": "2 failed"},
            )
            self.assertEqual(_git(local, ["rev-parse", "HEAD"]).stdout.strip(), before)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "tests_failed")
        self.assertTrue(result["rolled_back"])

    def test_restart_is_invoked_only_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote, local = _init_remote_and_clone(tmp_path)
            _push_new_commit(remote, tmp_path, "other")
            restarted = []
            result = apply_update(
                local, remote="origin", branch="main",
                test_runner=lambda repo, python: {"ok": True},
                restart_fn=lambda: restarted.append(True) or {"restarted": True},
            )
        self.assertTrue(result["applied"])
        self.assertEqual(restarted, [True])


class ConfigAndStateTests(unittest.TestCase):
    def test_set_update_mode_preserves_other_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"mail": {"sender": "me@example.com"}, "update": {"branch": "dev"}}))
            set_update_mode("automatic", path=path)
            saved = json.loads(path.read_text())
        self.assertEqual(saved["mail"]["sender"], "me@example.com")
        self.assertEqual(saved["update"]["mode"], "automatic")
        self.assertEqual(saved["update"]["branch"], "dev")

    def test_set_update_mode_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            set_update_mode("yolo")

    def test_ensure_self_update_job_requires_resolved_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            db = root / "lisan.sqlite"
            self.assertIsNone(ensure_self_update_job(db_path=db, config={"update": {"mode": None}}))
            self.assertIsNone(ensure_self_update_job(db_path=db, config={"update": {"mode": "off"}}))
            job_id = ensure_self_update_job(db_path=db, config={"update": {"mode": "notify"}})
        self.assertIsNotNone(job_id)

    def test_ensure_self_update_job_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            db = root / "lisan.sqlite"
            config = {"update": {"mode": "automatic"}}
            first = ensure_self_update_job(db_path=db, config=config)
            second = ensure_self_update_job(db_path=db, config=config)
        self.assertEqual(first, second)

    def test_render_update_status_unresolved(self):
        rendered = render_update_status({"update": {"mode": None}}, {})
        self.assertIn("unresolved", rendered)

    def test_render_update_status_with_check_history(self):
        rendered = render_update_status(
            {"update": {"mode": "notify", "branch": "main"}},
            {"checked_at": "2026-10-26T00:00:00Z", "local_commit": "abc1234", "remote_commit": "def5678", "update_available": True},
        )
        self.assertIn("notify", rendered)
        self.assertIn("update is available", rendered.lower())


class RunPeriodicCheckTests(unittest.TestCase):
    def test_notify_mode_notifies_once_per_new_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote, local = _init_remote_and_clone(tmp_path)
            _push_new_commit(remote, tmp_path, "other")
            root = tmp_path / "install"
            ensure_repo_layout(root)
            db = root / "lisan.sqlite"
            notified = []
            with patch("lisan.tools.self_update._notify", side_effect=lambda vault, text: notified.append(text) or True):
                with patch("lisan.config.load_config", return_value={"update": {"mode": "notify", "remote": "origin", "branch": "main"}}):
                    first = run_periodic_check(vault_root(root), db_path=db, repo=local)
                    second = run_periodic_check(vault_root(root), db_path=db, repo=local)
        self.assertTrue(first["update_available"])
        self.assertEqual(len(notified), 1)  # second call: same remote commit, no repeat page
        self.assertEqual(second["update_available"], True)

    def test_automatic_mode_applies_and_notifies_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote, local = _init_remote_and_clone(tmp_path)
            _push_new_commit(remote, tmp_path, "other")
            root = tmp_path / "install"
            ensure_repo_layout(root)
            db = root / "lisan.sqlite"
            notified = []
            with patch("lisan.tools.self_update._notify", side_effect=lambda vault, text: notified.append(text) or True):
                with patch("lisan.config.load_config", return_value={"update": {"mode": "automatic", "remote": "origin", "branch": "main"}}):
                    with patch("lisan.tools.self_update.apply_update", return_value={"applied": True, "from": "aaa1111", "to": "bbb2222"}):
                        result = run_periodic_check(vault_root(root), db_path=db, repo=local)
        self.assertTrue(result["applied"])
        self.assertEqual(len(notified), 1)
        self.assertIn("updated", notified[0])

    def test_skips_when_mode_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            with patch("lisan.config.load_config", return_value={"update": {"mode": None}}):
                result = run_periodic_check(vault_root(root), db_path=root / "lisan.sqlite")
        self.assertTrue(result["skipped"])


class JobWiringTests(unittest.TestCase):
    def test_self_update_check_is_a_registered_job_type(self):
        from lisan.tools.jobs import JOB_TYPES

        self.assertIn("self_update.check", JOB_TYPES)

    def test_dispatch_job_routes_self_update_check(self):
        from lisan.tools.jobs import dispatch_job

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            job = {"job_type": "self_update.check", "payload": {}}
            with patch("lisan.tools.self_update.run_periodic_check", return_value={"skipped": True}) as mocked:
                result = dispatch_job(job, vault=vault_root(root), db_path=root / "lisan.sqlite")
        mocked.assert_called_once()
        self.assertEqual(result, {"skipped": True})


if __name__ == "__main__":
    unittest.main()
