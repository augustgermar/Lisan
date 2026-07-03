from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools import scheduler
from lisan.tools.jobs import claim_next_job, enqueue_job, get_job, list_jobs, run_jobs_worker
from lisan.tools.scheduler import (
    format_task_list,
    list_tasks,
    next_occurrence,
    normalize_recurrence,
    parse_when,
    run_scheduler_loop,
    run_task_job,
    schedule_task,
    seconds_until_next_due,
)


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


class ParseWhenTests(unittest.TestCase):
    def test_iso_naive_is_local_time(self):
        parsed = parse_when("2030-06-01 15:30")
        local = parsed.astimezone(datetime.now().astimezone().tzinfo)
        self.assertEqual((local.year, local.month, local.day, local.hour, local.minute), (2030, 6, 1, 15, 30))

    def test_iso_with_timezone_is_preserved(self):
        parsed = parse_when("2030-06-01T15:30:00+00:00")
        self.assertEqual(parsed, datetime(2030, 6, 1, 15, 30, tzinfo=timezone.utc))

    def test_bare_time_rolls_to_tomorrow_when_past(self):
        now = datetime.now(timezone.utc)
        one_hour_ago = (now - timedelta(hours=1)).astimezone(datetime.now().astimezone().tzinfo)
        parsed = parse_when(one_hour_ago.strftime("%H:%M"), now=now)
        self.assertGreater(parsed, now)
        self.assertLess(parsed - now, timedelta(days=1))

    def test_garbage_raises_with_current_time(self):
        with self.assertRaises(ValueError) as ctx:
            parse_when("next thursday")
        self.assertIn("Current local time", str(ctx.exception))

    def test_relative_offset(self):
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_when("+2h", now=now), now + timedelta(hours=2))
        self.assertEqual(parse_when("+30m", now=now), now + timedelta(minutes=30))

    def test_tomorrow_form(self):
        now = datetime.now(timezone.utc)
        parsed = parse_when("tomorrow 09:00", now=now)
        self.assertGreater(parsed, now)
        self.assertLess(parsed - now, timedelta(days=2))
        local = parsed.astimezone(datetime.now().astimezone().tzinfo)
        self.assertEqual((local.hour, local.minute), (9, 0))


class RecurrenceTests(unittest.TestCase):
    def test_every_forms_normalize(self):
        self.assertEqual(normalize_recurrence("every:30m"), "every:30m")
        self.assertEqual(normalize_recurrence("EVERY:2H"), "every:2h")
        self.assertIsNone(normalize_recurrence(None))
        self.assertIsNone(normalize_recurrence(""))

    def test_daily_normalizes_hour_padding(self):
        self.assertEqual(normalize_recurrence("daily@9:05"), "daily@09:05")

    def test_bad_rules_raise(self):
        for bad in ("weekly:1", "every:xm", "daily@25:00", "sometimes"):
            with self.assertRaises(ValueError):
                normalize_recurrence(bad)

    def test_every_next_occurrence_adds_interval(self):
        after = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(next_occurrence("every:90m", after=after), after + timedelta(minutes=90))

    def test_daily_next_occurrence_is_strictly_future(self):
        nxt = next_occurrence("daily@09:00")
        self.assertGreater(nxt, datetime.now(timezone.utc))
        self.assertLess(nxt - datetime.now(timezone.utc), timedelta(days=1, minutes=1))


class ScheduleTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_shot_reminder_enqueues_future_job(self):
        summary = schedule_task(kind="reminder", text="call the dentist", when="2030-06-01 15:00", db_path=self.db)
        job = get_job(summary["job_id"], db_path=self.db)
        self.assertEqual(job["job_type"], "task.reminder")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["payload"]["message"], "call the dentist")
        self.assertIsNone(job["recurrence"])
        # not due for years: the claim must not return it
        self.assertIsNone(claim_next_job("w1", db_path=self.db))

    def test_past_when_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            schedule_task(kind="reminder", text="too late", when="2020-01-01 09:00", db_path=self.db)
        self.assertIn("past", str(ctx.exception))

    def test_recurrence_without_when_uses_next_occurrence(self):
        summary = schedule_task(kind="reminder", text="stand up", recurrence="every:1h", db_path=self.db)
        job = get_job(summary["job_id"], db_path=self.db)
        self.assertEqual(job["recurrence"], "every:1h")
        self.assertTrue(job["scheduled_for"])

    def test_two_reminders_never_coalesce(self):
        a = schedule_task(kind="reminder", text="one", when="2030-06-01 15:00", db_path=self.db)
        b = schedule_task(kind="reminder", text="two", when="2030-06-01 15:00", db_path=self.db)
        self.assertNotEqual(a["job_id"], b["job_id"])

    def test_unknown_kind_and_empty_text_raise(self):
        with self.assertRaises(ValueError):
            schedule_task(kind="explode", text="x", when="2030-06-01 15:00", db_path=self.db)
        with self.assertRaises(ValueError):
            schedule_task(kind="reminder", text="  ", when="2030-06-01 15:00", db_path=self.db)


class TaskExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "jobs.sqlite"
        self.sent: list[tuple[str, int | None]] = []

    def tearDown(self):
        self.tmp.cleanup()

    def _send(self, text: str, chat_id: int | None) -> None:
        self.sent.append((text, chat_id))

    def _due_reminder(self, message: str = "water the cats", **payload_extra) -> str:
        payload = {"message": message, "due": "2020-01-01T00:00:00Z", **payload_extra}
        return enqueue_job("task.reminder", payload, scheduled_for="2020-01-01T00:00:00Z", db_path=self.db)

    def test_reminder_delivers_via_send_fn(self):
        job_id = self._due_reminder(chat_id=42)
        job = get_job(job_id, db_path=self.db)
        result = run_task_job(job, vault=self.vault, db_path=self.db, send_fn=self._send)
        self.assertTrue(result["delivered"])
        self.assertEqual(self.sent[0][1], 42)
        self.assertIn("water the cats", self.sent[0][0])

    def test_late_delivery_says_so(self):
        job_id = self._due_reminder()
        job = get_job(job_id, db_path=self.db)
        run_task_job(job, vault=self.vault, db_path=self.db, send_fn=self._send)
        self.assertIn("delivering late", self.sent[0][0])

    def test_worker_runs_due_reminder_and_marks_succeeded(self):
        job_id = self._due_reminder()
        with patch.object(scheduler, "_deliver_owner_message") as deliver:
            summary = run_jobs_worker(vault=self.vault, db_path=self.db)
        self.assertEqual(summary["success_count"], 1)
        deliver.assert_called_once()
        self.assertEqual(get_job(job_id, db_path=self.db)["status"], "succeeded")

    def test_reminder_without_telegram_config_fails_honestly(self):
        job_id = self._due_reminder()
        with patch.object(scheduler, "load_config", return_value={}), \
                patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("LISAN_TELEGRAM_TOKEN", None)
            os.environ.pop("LISAN_TELEGRAM_ALLOWED", None)
            summary = run_jobs_worker(vault=self.vault, db_path=self.db)
        # the worker exhausts max_attempts within one drain (retry_wait
        # promotes immediately when scheduled_for is now)
        self.assertEqual(summary["failure_count"], 3)
        job = get_job(job_id, db_path=self.db)
        self.assertEqual(job["status"], "failed")
        self.assertIn("telegram is not configured", str(job["error"]))

    def test_recurring_reminder_requeues_next_occurrence(self):
        payload = {"message": "stretch", "due": "2020-01-01T00:00:00Z"}
        enqueue_job(
            "task.reminder", payload,
            scheduled_for="2020-01-01T00:00:00Z", recurrence="every:1h", db_path=self.db,
        )
        with patch.object(scheduler, "_deliver_owner_message"):
            run_jobs_worker(vault=self.vault, db_path=self.db)
        queued = [job for job in list_jobs(status="queued", db_path=self.db) if job["job_type"] == "task.reminder"]
        self.assertEqual(len(queued), 1)
        follow_up = queued[0]
        self.assertEqual(follow_up["recurrence"], "every:1h")
        self.assertGreater(
            datetime.fromisoformat(follow_up["scheduled_for"].replace("Z", "+00:00")),
            datetime.now(timezone.utc),
        )
        self.assertEqual(follow_up["payload"]["message"], "stretch")

    def test_one_shot_does_not_requeue(self):
        self._due_reminder()
        with patch.object(scheduler, "_deliver_owner_message"):
            run_jobs_worker(vault=self.vault, db_path=self.db)
        self.assertEqual(list_jobs(status="queued", db_path=self.db), [])


class ScheduleTaskToolTests(unittest.TestCase):
    """The interlocutor-facing schedule_task tool."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "jobs.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def _handlers(self, conversation_id: str | None = None, approval_fn=None):
        from lisan.tools.execution_tools import build_tool_handlers

        return build_tool_handlers(
            vault=self.vault,
            db_path=self.db,
            config={},
            conversation_id=conversation_id,
            approval_fn=approval_fn,
        )

    def test_reminder_schedules_without_approval(self):
        handlers = self._handlers(approval_fn=lambda name, args: False)
        out = handlers["schedule_task"](text="call mom", when="+2h")
        self.assertIn("Scheduled reminder", out)
        self.assertEqual(len(list_tasks(db_path=self.db)), 1)

    def test_codex_kind_requires_approval(self):
        handlers = self._handlers(approval_fn=lambda name, args: False)
        out = handlers["schedule_task"](text="rotate backups", when="+1d", kind="codex")
        self.assertIn("denied", out)
        self.assertEqual(list_tasks(db_path=self.db), [])

    def test_telegram_conversation_id_binds_chat(self):
        handlers = self._handlers(conversation_id="telegram-4242-2026-07-02")
        handlers["schedule_task"](text="hydrate", when="+1h")
        job = list_tasks(db_path=self.db)[0]
        self.assertEqual(job["payload"]["chat_id"], 4242)

    def test_bad_when_returns_error_with_current_time(self):
        handlers = self._handlers()
        out = handlers["schedule_task"](text="x", when="whenever")
        self.assertTrue(out.startswith("Error:"))
        self.assertIn("Current local time", out)
        self.assertEqual(list_tasks(db_path=self.db), [])


class SchedulerLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "jobs.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_seconds_until_next_due_clamps(self):
        self.assertEqual(seconds_until_next_due(db_path=self.db, ceiling=30.0), 30.0)
        enqueue_job("task.reminder", {"message": "x"}, scheduled_for="2099-01-01T00:00:00Z", db_path=self.db)
        self.assertEqual(seconds_until_next_due(db_path=self.db, ceiling=30.0), 30.0)
        enqueue_job("task.reminder", {"message": "y"}, scheduled_for="2020-01-01T00:00:00Z", db_path=self.db)
        self.assertEqual(seconds_until_next_due(db_path=self.db, ceiling=30.0), 0.0)

    def test_loop_delivers_due_task_through_send_fn(self):
        sent: list[str] = []
        enqueue_job(
            "task.reminder",
            {"message": "loop check", "due": "2020-01-01T00:00:00Z"},
            scheduled_for="2020-01-01T00:00:00Z",
            db_path=self.db,
        )
        ticks = run_scheduler_loop(
            vault=self.vault,
            db_path=self.db,
            poll_seconds=0.01,
            max_ticks=1,
            send_fn=lambda text, chat_id: sent.append(text),
        )
        self.assertEqual(ticks, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("loop check", sent[0])

    def test_list_and_format_tasks(self):
        schedule_task(kind="reminder", text="visible task", when="2030-06-01 15:00", db_path=self.db)
        tasks = list_tasks(db_path=self.db)
        self.assertEqual(len(tasks), 1)
        rendered = format_task_list(tasks)
        self.assertIn("visible task", rendered)
        self.assertIn("queued", rendered)
        self.assertEqual(format_task_list([]), "No scheduled tasks.")


if __name__ == "__main__":
    unittest.main()
