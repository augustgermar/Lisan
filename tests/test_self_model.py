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
