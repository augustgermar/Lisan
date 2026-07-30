from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.jobs import enqueue_job
from lisan.tools.self_model import (
    build_capability_manifest,
    capability_index,
    cli_reference,
    ensure_capabilities_primer,
    render_capability_primer,
    render_self_state,
    snapshot_self_state,
)


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = build_capability_manifest(config={})

    def test_cli_is_introspected_from_the_real_parser(self):
        commands = {c["command"] for c in self.manifest["cli"]}
        for expected in ("lisan ingest", "lisan task", "lisan scheduler", "lisan self", "lisan chat"):
            self.assertIn(expected, commands)
        ingest = next(c for c in self.manifest["cli"] if c["command"] == "lisan ingest")
        self.assertTrue(any("--reference" in o["arg"] for o in ingest["options"]))
        self.assertTrue(any(s["command"] == "lisan ingest scan" for s in ingest.get("subcommands", [])))

    def test_tools_and_jobs_present(self):
        tool_names = {t["name"] for t in self.manifest["tools"]}
        self.assertLessEqual({"search_memory", "read_file", "run_codex", "schedule_task", "self_state"}, tool_names)
        self.assertIn("task.reminder", self.manifest["job_types"])

    def test_not_built_declared(self):
        names = " ".join(i["name"] for i in self.manifest["not_built"]).lower()
        self.assertIn("obsidian", names)

    def test_index_is_compact_and_complete(self):
        index = capability_index(self.manifest)
        self.assertLess(len(index), 2000, "index must stay cheap enough for every turn")
        for needle in ("self_state", "schedule_task", "ingest", "Not built"):
            self.assertIn(needle, index)

    def test_cli_reference_lists_flags(self):
        ref = cli_reference()
        self.assertIn("lisan task add", ref)
        self.assertIn("--reference", ref)

    def test_primer_renders_all_sections(self):
        text = render_capability_primer(self.manifest)
        for heading in ("## Conversation tools", "## CLI commands", "## Not built yet"):
            self.assertIn(heading, text)
        self.assertIn("generated — do not edit", text)


class PrimerRegenerationTests(unittest.TestCase):
    def test_regenerates_only_when_stamp_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            (vault / "primer").mkdir(parents=True)
            first = ensure_capabilities_primer(vault)
            self.assertIsNotNone(first)
            self.assertTrue(first.exists())
            self.assertIsNone(ensure_capabilities_primer(vault), "same stamp must be a no-op")
            self.assertIsNotNone(ensure_capabilities_primer(vault, force=True))


class SelfStateTests(unittest.TestCase):
    def test_snapshot_reads_live_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            vault = vault_root(root)
            db = root / "jobs.sqlite"
            enqueue_job("task.reminder", {"message": "x"}, scheduled_for="2099-01-01T00:00:00Z", db_path=db)
            state = snapshot_self_state(vault=vault, db_path=db)
            self.assertEqual(state["jobs"]["queued"]["task.reminder"], 1)
            self.assertEqual(state["next_scheduled_task"]["job_type"], "task.reminder")
            rendered = render_self_state(state)
            self.assertIn("task.reminder", rendered)
            self.assertIn("Next scheduled", rendered)

    def test_future_scheduled_jobs_are_not_rendered_as_stuck(self):
        """A reminder waiting for next week is 'queued' in the table; the
        render must say it is waiting for its time — 'two jobs stuck in the
        queue' (2026-07-06) came straight from this ambiguity."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            vault = vault_root(root)
            db = root / "jobs.sqlite"
            enqueue_job("task.reminder", {"message": "x"}, scheduled_for="2099-01-01T00:00:00Z", db_path=db)
            state = snapshot_self_state(vault=vault, db_path=db)
            self.assertEqual(state["queued_due_now"], 0)
            rendered = render_self_state(state)
            self.assertIn("0 due now", rendered)
            self.assertIn("not stuck", rendered)

    def test_snapshot_reports_machine_sleep_when_available(self):
        """The agent must be able to tell 'the machine was asleep' from 'my
        services failed' — the 2026-07-06 incident was a sleeping Mac
        misdiagnosed as a stalled processor, by the agent and then by the
        troubleshooting human it misinformed."""
        from unittest.mock import patch

        from lisan.tools import self_model

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            with patch.object(
                self_model,
                "_machine_sleep_status",
                return_value={"last_sleep": "2026-07-06 09:32 PDT", "last_wake": "2026-07-06 12:56 PDT"},
            ):
                state = snapshot_self_state(vault=vault_root(root), db_path=root / "jobs.sqlite")
                rendered = render_self_state(state)
        self.assertIn("last slept 2026-07-06 09:32 PDT", rendered)
        self.assertIn("not a service failure", rendered)

    def test_epoch_zero_sleeptime_is_not_reported_as_1969(self):
        """`sec = 0` means the kernel has no record, not midnight 1970.

        On Darwin 24.6 both kern.sleeptime and kern.waketime read
        `{ sec = 0, usec = 0 }`, and the instrument dutifully rendered "last
        slept 1969-12-31 16:00 PST" beside its own sentence telling the reader
        to treat that interval as sleep rather than a service failure. The
        instrument built to stop a confabulation was producing one; a missing
        measurement must read as missing."""
        import subprocess
        from unittest.mock import patch

        from lisan.tools import self_model

        completed = subprocess.CompletedProcess(
            args=["sysctl"], returncode=0, stdout="{ sec = 0, usec = 0 } Wed Dec 31 16:00:00 1969\n"
        )
        with patch("platform.system", return_value="Darwin"), patch.object(
            self_model.subprocess, "run", return_value=completed
        ):
            machine = self_model._machine_sleep_status()

        self.assertEqual(machine, {}, "epoch-zero sentinel must not become a timestamp")

        # ... and with no data, the snapshot makes no sleep claim at all.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            with patch.object(self_model, "_machine_sleep_status", return_value={}):
                state = snapshot_self_state(vault=vault_root(root), db_path=root / "jobs.sqlite")
                rendered = render_self_state(state)
        self.assertNotIn("1969", rendered)
        self.assertNotIn("Machine: last slept", rendered)
        self.assertNotIn("not a service failure", rendered)

    def test_log_tail_keeps_only_whole_timestamped_lines(self):
        """The tail of a multi-line traceback is a context-free shard the
        model narrates into a story; only stamped log lines may surface."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            vault = vault_root(root)
            log_dir = vault / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "errors.log").write_text(
                "2026-07-06 09:38:15 [ERROR] lisan: telegram poll error; retry in 1s\n"
                "Traceback (most recent call last):\n"
                '  File "x.py", line 1, in <module>\n'
                "TimeoutError: The read operation timed out\n",
                encoding="utf-8",
            )
            state = snapshot_self_state(vault=vault, db_path=root / "jobs.sqlite")
        for line in state["recent_log_tail"]:
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} ")

    def test_snapshot_survives_missing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            state = snapshot_self_state(vault=vault, db_path=Path(tmp) / "missing" / "db.sqlite")
            rendered = render_self_state(state)
            self.assertIn("Lisan v", rendered)


class ToolWiringTests(unittest.TestCase):
    def test_self_state_tool_returns_rendered_state(self):
        from lisan.tools.execution_tools import build_tool_handlers

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            handlers = build_tool_handlers(vault=vault_root(root), db_path=root / "jobs.sqlite", config={})
            out = handlers["self_state"]()
            self.assertIn("Lisan v", out)
            self.assertIn("Jobs", out)

    def test_codex_briefing_includes_cli_reference(self):
        from unittest.mock import patch

        from lisan.tools import execution_tools
        from lisan.tools.execution_tools import _build_codex_prompt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            with patch.object(execution_tools, "assemble_context", return_value="(ctx)"):
                prompt = _build_codex_prompt(
                    task="t", working_directory=root, vault=vault_root(root), db_path=root / "x.sqlite"
                )
            self.assertIn("lisan ingest", prompt)
            self.assertIn("lisan task add", prompt)


if __name__ == "__main__":
    unittest.main()


class StaleCodeInstrumentTests(unittest.TestCase):
    """A long-running process holds the modules it imported, and nothing said so.

    On 2026-07-30 the analyst re-created five duplicate pattern records overnight
    using a record_factory that had been fixed the previous afternoon. The fix
    was committed, tested, and live in the tree — and entirely absent from the
    process doing the work, which had started at 14:48 the day before. The vault
    looked like the fix had failed. It had simply never been loaded.

    Comparing the newest source mtime against each service's start time needs no
    new state file and answers the only question that matters: is the agent
    running the code I think it is?
    """

    def test_stale_services_are_named_in_the_rendered_snapshot(self):
        from unittest.mock import patch

        from lisan.tools import self_model

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            with patch.object(self_model, "_stale_code_services", return_value=["adjutant", "telegram"]):
                state = snapshot_self_state(vault=vault_root(root), db_path=root / "jobs.sqlite")
                rendered = render_self_state(state)
        self.assertIn("STALE CODE", rendered)
        self.assertIn("adjutant", rendered)
        self.assertIn("restart", rendered.lower(), "the report must name the remedy")

    def test_nothing_is_said_when_no_service_is_stale(self):
        from unittest.mock import patch

        from lisan.tools import self_model

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            with patch.object(self_model, "_stale_code_services", return_value=[]):
                state = snapshot_self_state(vault=vault_root(root), db_path=root / "jobs.sqlite")
                rendered = render_self_state(state)
        self.assertNotIn("STALE CODE", rendered)

    def test_service_discovery_finds_services_the_code_does_not_hardcode(self):
        """The hardcoded pair missed com.lisan.adjutant and com.lisan.jobs, both
        installed on a real deployment — so the agent could not say whether its
        own execution layer was alive."""
        from unittest.mock import patch

        from lisan.tools import self_model

        listing = "15262\t1\tcom.lisan.adjutant\n25118\t-15\tcom.lisan.telegram\n-\t0\tcom.lisan.jobs\n"

        class _Result:
            stdout = listing

        with patch.object(self_model.subprocess, "run", return_value=_Result()), \
                patch("platform.system", return_value="Darwin"):
            services = self_model._service_status()

        self.assertTrue(services["telegram"])
        self.assertTrue(services["adjutant"], "a discovered running service is up")
        self.assertIn("jobs", services)
        self.assertFalse(services["jobs"], "an interval-triggered service with no pid is idle")
