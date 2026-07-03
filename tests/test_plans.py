from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools import plans
from lisan.tools.jobs import get_job, list_jobs, run_jobs_worker
from lisan.tools.plans import (
    active_plans,
    cancel_plan,
    create_plan,
    format_plans,
    list_plans,
)


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "jobs.sqlite"
        self.sent: list[tuple[str, int | None]] = []

    def tearDown(self):
        self.tmp.cleanup()


class CreatePlanTests(_Env):
    def test_validation(self):
        with self.assertRaises(ValueError):
            create_plan(goal="", steps=[{"kind": "note", "description": "x"}], db_path=self.db)
        with self.assertRaises(ValueError):
            create_plan(goal="g", steps=[], db_path=self.db)
        with self.assertRaises(ValueError):
            create_plan(goal="g", steps=[{"kind": "explode", "description": "x"}], db_path=self.db)
        with self.assertRaises(ValueError):
            create_plan(goal="g", steps=[{"kind": "note", "description": ""}], db_path=self.db)

    def test_creates_claimable_job(self):
        summary = create_plan(goal="test goal", steps=[{"kind": "note", "description": "observe"}], db_path=self.db)
        job = get_job(summary["job_id"], db_path=self.db)
        self.assertEqual(job["job_type"], "plan.run")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["payload"]["goal"], "test goal")


class PlanExecutionTests(_Env):
    def test_note_steps_chain_to_completion(self):
        create_plan(
            goal="two observations",
            steps=[{"kind": "note", "description": "first"}, {"kind": "note", "description": "second"}],
            db_path=self.db,
        )
        with patch("lisan.tools.scheduler._deliver_owner_message") as deliver:
            summary = run_jobs_worker(vault=self.vault, db_path=self.db)
        self.assertEqual(summary["failure_count"], 0)
        plan = list_plans(db_path=self.db)[0]
        self.assertEqual(plan["steps_done"], 2)
        self.assertFalse(plan["active"])
        deliver.assert_called_once()
        message = deliver.call_args.args[0]
        self.assertIn("Plan completed", message)
        # report written into the vault
        report = self.vault / "reports" / f"{plan['plan_id']}.md"
        self.assertTrue(report.exists())
        self.assertIn("two observations", report.read_text())

    def test_codex_step_failure_aborts_and_reports(self):
        create_plan(
            goal="doomed",
            steps=[
                {"kind": "codex", "description": "will fail"},
                {"kind": "note", "description": "never runs"},
            ],
            db_path=self.db,
        )
        with patch("lisan.tools.plans.load_config", return_value={}), \
                patch("lisan.tools.execution_tools.assemble_context", return_value="(ctx)"), \
                patch("lisan.providers.codex.CodexClient") as client, \
                patch("lisan.tools.scheduler._deliver_owner_message") as deliver:
            client.return_value.complete.side_effect = RuntimeError("boom")
            run_jobs_worker(vault=self.vault, db_path=self.db)
        plan = list_plans(db_path=self.db)[0]
        self.assertEqual(plan["job_status"], "succeeded")  # the step job itself succeeded at *running*
        self.assertEqual(plan["steps_done"], 0)
        message = deliver.call_args.args[0]
        self.assertIn("Plan failed", message)
        self.assertIn("never runs", message)

    def test_codex_steps_see_earlier_results(self):
        seen_prompts: list[str] = []

        def fake_codex(prompt, **kwargs):
            from lisan.providers.base import LLMResponse

            seen_prompts.append(prompt)
            return LLMResponse(text="folder has 3 files", provider="stub", model="s")

        create_plan(
            goal="inventory then summarize",
            steps=[
                {"kind": "codex", "description": "list the folder"},
                {"kind": "codex", "description": "summarize what you found"},
            ],
            db_path=self.db,
        )
        with patch("lisan.tools.plans.load_config", return_value={}), \
                patch("lisan.tools.execution_tools.assemble_context", return_value="(ctx)"), \
                patch("lisan.providers.codex.CodexClient") as client, \
                patch("lisan.tools.scheduler._deliver_owner_message"):
            client.return_value.complete.side_effect = fake_codex
            run_jobs_worker(vault=self.vault, db_path=self.db)
        self.assertEqual(len(seen_prompts), 2)
        self.assertIn("Overall goal: inventory then summarize", seen_prompts[1])
        self.assertIn("folder has 3 files", seen_prompts[1])


class PlanVisibilityTests(_Env):
    def test_active_plans_and_cancel(self):
        summary = create_plan(goal="visible", steps=[{"kind": "note", "description": "x"}], db_path=self.db)
        active = active_plans(db_path=self.db)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["goal"], "visible")
        rendered = format_plans(list_plans(db_path=self.db))
        self.assertIn("visible", rendered)
        self.assertTrue(cancel_plan(summary["plan_id"], db_path=self.db))
        self.assertEqual(active_plans(db_path=self.db), [])
        self.assertFalse(cancel_plan("plan.nonexistent", db_path=self.db))

    def test_self_state_lists_active_plans(self):
        from lisan.tools.self_model import render_self_state, snapshot_self_state

        create_plan(goal="show me in state", steps=[{"kind": "note", "description": "x"}], db_path=self.db)
        state = snapshot_self_state(vault=self.vault, db_path=self.db)
        self.assertEqual(state["active_plans"][0]["goal"], "show me in state")
        self.assertIn("Active plan", render_self_state(state))


class PlanToolTests(_Env):
    def _handlers(self, approval_fn=None):
        from lisan.tools.execution_tools import build_tool_handlers

        return build_tool_handlers(vault=self.vault, db_path=self.db, config={}, approval_fn=approval_fn)

    def test_note_only_plan_needs_no_approval(self):
        handlers = self._handlers(approval_fn=lambda n, a: False)
        out = handlers["create_plan"](goal="g", steps=[{"kind": "note", "description": "x"}])
        self.assertIn("Plan created", out)

    def test_codex_plan_requires_approval(self):
        handlers = self._handlers(approval_fn=lambda n, a: False)
        out = handlers["create_plan"](goal="g", steps=[{"kind": "codex", "description": "x"}])
        self.assertIn("denied", out)
        self.assertEqual(list_plans(db_path=self.db), [])


class PromptStepSignatureTests(_Env):
    def test_prompt_step_matches_chat_turn_signature(self):
        """autospec catches signature drift — the live plan run failed on
        exactly this (required kwargs added to _process_chat_turn)."""
        create_plan(goal="ask", steps=[{"kind": "prompt", "description": "say hi"}], db_path=self.db)
        with patch("lisan.tools.chat._process_chat_turn", autospec=True,
                   return_value={"response": "hi"}), \
                patch("lisan.tools.scheduler._deliver_owner_message"):
            summary = run_jobs_worker(vault=self.vault, db_path=self.db)
        self.assertEqual(summary["failure_count"], 0)
        self.assertEqual(list_plans(db_path=self.db)[0]["steps_done"], 1)

    def test_scheduled_prompt_task_matches_signature_too(self):
        from lisan.tools.jobs import enqueue_job

        enqueue_job("task.prompt", {"prompt": "say hi", "due": "2020-01-01T00:00:00Z"},
                    scheduled_for="2020-01-01T00:00:00Z", db_path=self.db)
        with patch("lisan.tools.chat._process_chat_turn", autospec=True,
                   return_value={"response": "hi"}), \
                patch("lisan.tools.scheduler._deliver_owner_message"):
            summary = run_jobs_worker(vault=self.vault, db_path=self.db)
        self.assertEqual(summary["failure_count"], 0)


class TerminalFailureTests(_Env):
    def test_infra_death_still_delivers_failure_report(self):
        create_plan(goal="fragile", steps=[{"kind": "prompt", "description": "x"}], db_path=self.db)
        with patch("lisan.tools.chat._process_chat_turn", side_effect=OSError("infra down")), \
                patch("lisan.tools.scheduler._deliver_owner_message") as deliver:
            run_jobs_worker(vault=self.vault, db_path=self.db)
        self.assertTrue(deliver.called)
        message = deliver.call_args.args[0]
        self.assertIn("Plan failed", message)
        plan = list_plans(db_path=self.db)[0]
        self.assertFalse(plan["active"])


class FolderIngestionPlanTests(_Env):
    def _folder(self, count: int) -> Path:
        folder = self.root / "notes"
        folder.mkdir()
        for i in range(count):
            (folder / f"note-{i:02d}.md").write_text(f"# Note {i}\ncontent {i}\n")
        return folder

    def test_batches_and_final_summary_step(self):
        from lisan.tools.plans import build_folder_ingestion_plan

        folder = self._folder(13)
        summary = build_folder_ingestion_plan(folder, batch_size=5, db_path=self.db)
        job = get_job(summary["job_id"], db_path=self.db)
        steps = job["payload"]["steps"]
        self.assertEqual(len(steps), 4)  # 5+5+3 files, then the summary prompt
        self.assertEqual([s["kind"] for s in steps], ["codex", "codex", "codex", "prompt"])
        self.assertIn("note-00.md", steps[0]["description"])
        self.assertIn("note-12.md", steps[2]["description"])
        self.assertIn("lisan ingest --reference", steps[0]["description"])
        self.assertIn("QUESTIONS", steps[0]["description"])

    def test_limit_takes_first_files_only(self):
        from lisan.tools.plans import build_folder_ingestion_plan

        folder = self._folder(9)
        summary = build_folder_ingestion_plan(folder, batch_size=4, limit=4, db_path=self.db)
        job = get_job(summary["job_id"], db_path=self.db)
        steps = job["payload"]["steps"]
        self.assertEqual(len(steps), 2)
        self.assertNotIn("note-04.md", steps[0]["description"])

    def test_rejects_empty_or_missing_folder(self):
        from lisan.tools.plans import build_folder_ingestion_plan

        with self.assertRaises(ValueError):
            build_folder_ingestion_plan(self.root / "nope", db_path=self.db)
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            build_folder_ingestion_plan(empty, db_path=self.db)


if __name__ == "__main__":
    unittest.main()
