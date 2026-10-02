from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools import plans
from lisan.tools.jobs import get_job, list_jobs, run_jobs_worker
from lisan.tools.plans import _report_to_memory as _real_report_to_memory
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
        # Plans report outcomes through the real capture pipeline (an LLM
        # round trip); tests exercise that path with an injected spy instead.
        self._memory_report = patch("lisan.tools.plans._report_to_memory")
        self._memory_report.start()

    def tearDown(self):
        self._memory_report.stop()
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

    def test_codex_plan_creates_without_asking(self):
        # Owner decision 2026-07-26: the approval gate is deleted — the
        # command that asked for the plan is the consent.
        handlers = self._handlers(approval_fn=lambda n, a: False)
        out = handlers["create_plan"](goal="g", steps=[{"kind": "codex", "description": "x"}])
        self.assertIn("Plan created", out)
        self.assertEqual(len(list_plans(db_path=self.db)), 1)


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


class SummaryMessageTests(_Env):
    def test_closing_prompt_result_is_the_delivered_message(self):
        create_plan(
            goal="summarize things",
            steps=[{"kind": "note", "description": "gather"}, {"kind": "prompt", "description": "summarize"}],
            db_path=self.db,
        )
        with patch("lisan.tools.chat._process_chat_turn", autospec=True,
                   return_value={"response": "Here is what I learned, conversationally."}), \
                patch("lisan.tools.scheduler._deliver_owner_message") as deliver:
            run_jobs_worker(vault=self.vault, db_path=self.db)
        message = deliver.call_args.args[0]
        self.assertIn("Here is what I learned", message)
        self.assertNotIn("2/2 steps done", message)

    def test_failed_plan_still_gets_the_checklist(self):
        create_plan(
            goal="doomed",
            steps=[{"kind": "prompt", "description": "will fail"}],
            db_path=self.db,
        )
        with patch("lisan.tools.chat._process_chat_turn", autospec=True,
                   return_value={"response": "", "error": "nope"}), \
                patch("lisan.tools.scheduler._deliver_owner_message") as deliver:
            run_jobs_worker(vault=self.vault, db_path=self.db)
        message = deliver.call_args.args[0]
        self.assertIn("Plan failed", message)


if __name__ == "__main__":
    unittest.main()


class PlanRecursionContainmentTests(_Env):
    """A plan's `prompt` step runs the full conversation agent, tools and
    all — including create_plan. Told "this is one step of a larger plan,"
    the model reaches for it, and each child plan does the same. On
    2026-07-27 that produced 234 plans and ~200 queued jobs overnight from
    a single research request, until it hit the provider usage limit.
    Plans must not be able to make plans, or schedule background work."""

    def _handlers_for(self, conversation_id):
        from lisan.tools.execution_tools import build_tool_handlers

        return build_tool_handlers(
            vault=self.vault, db_path=self.db, config={}, conversation_id=conversation_id
        )

    def test_plan_step_cannot_create_a_nested_plan(self):
        handlers = self._handlers_for("plan-plan.abc123")
        out = handlers["create_plan"](
            goal="research everything",
            steps=[{"kind": "prompt", "description": "search"},
                   {"kind": "codex", "description": "write it up"}],
        )
        self.assertIn("already executing inside a plan", out)
        self.assertEqual(list_plans(db_path=self.db), [])

    def test_plan_step_cannot_schedule_background_work(self):
        handlers = self._handlers_for("plan-plan.abc123")
        out = handlers["schedule_task"](text="do it again", when="+1h", kind="codex")
        self.assertIn("inside a plan", out)

    def test_ordinary_conversation_still_creates_plans(self):
        handlers = self._handlers_for("telegram-4242-2026-07-27")
        out = handlers["create_plan"](
            goal="research everything",
            steps=[{"kind": "codex", "description": "write it up"}],
        )
        self.assertIn("Plan created", out)
        self.assertEqual(len(list_plans(db_path=self.db)), 1)


class _CaptureSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return {"captured": True}


def _job_row(db, plan_id):
    jobs = [j for j in list_jobs(limit=500, db_path=db) if (j.get("payload") or {}).get("plan_id") == plan_id]
    return jobs


class PlanExecutionHardeningTests(_Env):
    """Phase 2: shared run ledger, intent gate, retry, resume, memory report."""

    def _run(self, complete, *, config=None):
        with patch("lisan.tools.plans.load_config", return_value=config or {}), \
                patch("lisan.tools.execution_tools.assemble_context", return_value="(ctx)"), \
                patch("lisan.providers.codex.CodexClient") as client, \
                patch("lisan.tools.scheduler._deliver_owner_message") as deliver:
            client.return_value.complete.side_effect = complete
            run_jobs_worker(vault=self.vault, db_path=self.db)
        return deliver

    def _runs(self):
        import sqlite3

        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT task_id, attempt, exit_status, origin FROM task_runs ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_each_step_leaves_a_ledger_row(self):
        from lisan.providers.base import LLMResponse

        summary = create_plan(
            goal="ledger", steps=[{"kind": "codex", "description": "a"}, {"kind": "note", "description": "b"}],
            db_path=self.db,
        )
        self._run(lambda prompt, **kw: LLMResponse(text="ok", provider="stub", model="s"))
        pid = summary["plan_id"]
        self.assertEqual(self._runs(), [(f"{pid}#step1", 1, "ok", "plan"), (f"{pid}#step2", 1, "ok", "plan")])

    def test_failed_step_is_recorded_failed(self):
        summary = create_plan(goal="doomed", steps=[{"kind": "codex", "description": "x"}], db_path=self.db)
        self._run(RuntimeError("boom"))
        self.assertEqual(self._runs(), [(f"{summary['plan_id']}#step1", 1, "failed", "plan")])

    def test_intent_never_rule_refuses_a_codex_step(self):
        from types import SimpleNamespace

        create_plan(goal="forbidden", steps=[{"kind": "codex", "description": "wipe it"}], db_path=self.db)
        verdict = SimpleNamespace(decision="deny", rule="never: destructive", reasons=["no deletes"])
        calls = []
        with patch("lisan.tools.execution_tools._chat_intent_verdict", return_value=(verdict, 3)):
            deliver = self._run(lambda prompt, **kw: calls.append(prompt))
        self.assertEqual(calls, [])  # codex was never invoked
        message = deliver.call_args.args[0]
        self.assertIn("Plan failed", message)
        self.assertIn("forbids this", message)

    def test_opted_in_retry_reruns_the_same_step_then_succeeds(self):
        from lisan.providers.base import LLMResponse

        attempts = []

        def flaky(prompt, **kw):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("transient")
            return LLMResponse(text="fine", provider="stub", model="s")

        summary = create_plan(
            goal="flaky", steps=[{"kind": "codex", "description": "try", "retries": 1}], db_path=self.db
        )
        deliver = self._run(flaky)
        self.assertEqual(len(attempts), 2)
        self.assertIn("Plan completed", deliver.call_args.args[0])
        pid = summary["plan_id"]
        self.assertEqual(
            self._runs(), [(f"{pid}#step1", 1, "failed", "plan"), (f"{pid}#step1", 2, "ok", "plan")]
        )

    def test_retries_are_bounded_and_default_off(self):
        calls = []

        def always(prompt, **kw):
            calls.append(1)
            raise RuntimeError("nope")

        create_plan(goal="no retry", steps=[{"kind": "codex", "description": "x"}], db_path=self.db)
        self._run(always)
        self.assertEqual(len(calls), 1)  # retries default to 0
        with self.assertRaises(ValueError):
            create_plan(goal="g", steps=[{"kind": "codex", "description": "x", "retries": 5}], db_path=self.db)

    def test_a_timed_out_step_is_never_retried(self):
        calls = []

        def hang(prompt, **kw):
            calls.append(1)
            raise RuntimeError("coding agent timed out after 1800s and was killed")

        create_plan(goal="slow", steps=[{"kind": "codex", "description": "x", "retries": 2}], db_path=self.db)
        deliver = self._run(hang)
        self.assertEqual(len(calls), 1)  # ambiguous state: do not run it twice
        self.assertIn("Plan failed", deliver.call_args.args[0])

    def test_resume_restarts_from_the_failed_step_and_keeps_earlier_results(self):
        from lisan.providers.base import LLMResponse
        from lisan.tools.plans import resume_plan

        summary = create_plan(
            goal="resumable",
            steps=[
                {"kind": "note", "description": "first"},
                {"kind": "codex", "description": "second"},
                {"kind": "note", "description": "third"},
            ],
            db_path=self.db,
        )
        self._run(RuntimeError("boom"))
        plan = list_plans(db_path=self.db)[0]
        self.assertEqual(plan["steps_done"], 1)
        resumed = resume_plan(summary["plan_id"], db_path=self.db)
        self.assertEqual(resumed["resumed_from_step"], 2)
        prompts = []

        def ok(prompt, **kw):
            prompts.append(prompt)
            return LLMResponse(text="recovered", provider="stub", model="s")

        deliver = self._run(ok)
        self.assertEqual(len(prompts), 1)  # only the failed step re-ran
        self.assertIn("first", prompts[0])  # and it still sees the earlier result
        self.assertIn("Plan completed", deliver.call_args.args[0])
        self.assertEqual(list_plans(db_path=self.db)[0]["steps_done"], 3)

    def test_resume_refuses_active_finished_and_unknown_plans(self):
        from lisan.tools.plans import resume_plan

        summary = create_plan(goal="live", steps=[{"kind": "note", "description": "x"}], db_path=self.db)
        with self.assertRaisesRegex(ValueError, "still active"):
            resume_plan(summary["plan_id"], db_path=self.db)
        self._run(RuntimeError("unused"))  # runs the note step to completion
        with self.assertRaisesRegex(ValueError, "no failed step"):
            resume_plan(summary["plan_id"], db_path=self.db)
        with self.assertRaisesRegex(ValueError, "no plan"):
            resume_plan("plan.nope", db_path=self.db)

    def test_outcome_is_reported_through_capture(self):
        real_report = _real_report_to_memory
        spy = _CaptureSpy()
        payload = {
            "plan_id": "plan.abc", "goal": "audit the box", "report_path": "/v/reports/plan.abc.md",
            "steps": [
                {"kind": "codex", "description": "collect", "status": "done"},
                {"kind": "codex", "description": "analyze", "status": "failed", "result": "ssh refused"},
            ],
        }
        self._memory_report.stop()
        try:
            real_report(payload, vault=self.vault, status="failed", capture=spy)
        finally:
            self._memory_report.start()
        self.assertEqual(len(spy.calls), 1)
        call = spy.calls[0]
        self.assertEqual(call["conversation_id"], "adjutant")
        self.assertIn("plan.abc (plan): FAILURE", call["text"])
        self.assertIn("ssh refused", call["text"])
        self.assertIn("audit the box", call["text"])

    def test_a_failing_capture_never_breaks_the_plan(self):
        real_report = _real_report_to_memory

        def boom(**kw):
            raise RuntimeError("pipeline down")

        payload = {"plan_id": "plan.x", "goal": "g", "steps": [{"kind": "note", "description": "d", "status": "done"}]}
        self._memory_report.stop()
        try:
            real_report(payload, vault=self.vault, status="completed", capture=boom)  # must not raise
        finally:
            self._memory_report.start()


class PlanProgressOrderingTests(unittest.TestCase):
    def test_resumed_chain_outranks_failed_chain_even_with_identical_timestamps(self):
        stamp = "2026-10-02T12:00:00"
        failed = {"created_at": stamp, "payload": {"current_step": 1, "steps": [{}, {"attempts": 1}]}}
        resumed = {"created_at": stamp, "payload": {"current_step": 1, "resume_count": 1, "steps": [{}, {"attempts": 0}]}}
        self.assertGreater(plans._plan_progress_key(resumed), plans._plan_progress_key(failed))

    def test_retry_of_the_current_step_outranks_its_first_attempt(self):
        stamp = "2026-10-02T12:00:00"
        first = {"created_at": stamp, "payload": {"current_step": 0, "steps": [{"attempts": 1}]}}
        retry = {"created_at": stamp, "payload": {"current_step": 0, "steps": [{"attempts": 2}]}}
        self.assertGreater(plans._plan_progress_key(retry), plans._plan_progress_key(first))
