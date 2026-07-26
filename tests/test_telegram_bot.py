from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools import telegram_bot
from lisan.tools.telegram_bot import (
    TelegramBot,
    _chunk,
    _render_launchd_plist,
    _render_systemd_unit,
    _resolve_settings,
    _valid_token_format,
    detect_owner_id,
    get_me,
    save_telegram_settings,
)


def _update(text: str, *, user_id: int = 1, chat_id: int = 99, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {"text": text, "chat": {"id": chat_id}, "from": {"id": user_id}},
    }


class ChunkTests(unittest.TestCase):
    def test_empty_returns_no_chunks(self):
        self.assertEqual(_chunk("   "), [])

    def test_short_text_unchanged(self):
        self.assertEqual(_chunk("hello"), ["hello"])

    def test_splits_on_newline_boundary(self):
        text = ("a" * 4000) + "\n" + ("b" * 4000)
        chunks = _chunk(text)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(c) <= telegram_bot._MSG_LIMIT for c in chunks))
        self.assertTrue(chunks[0].endswith("a"))
        self.assertTrue(chunks[1].startswith("b"))

    def test_hard_split_when_no_newline(self):
        chunks = _chunk("x" * 9000)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= telegram_bot._MSG_LIMIT for c in chunks))


class SplitMessageTests(unittest.TestCase):
    """A long paste arrives as back-to-back fragments, each capped at 4096
    chars. They must rejoin into one turn — an instruction near the end of
    the paste ("save all of this") is meaningless without its beginning."""

    def test_limit_sized_fragments_rejoin(self):
        frags = [
            _update("a" * 4000, update_id=1),
            _update("b" * 4000, update_id=2),
            _update("the end", update_id=3),
        ]
        out = telegram_bot._coalesce_split_messages(frags)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["message"]["text"], "a" * 4000 + "\n" + "b" * 4000 + "\nthe end")

    def test_short_rapid_messages_stay_separate_turns(self):
        out = telegram_bot._coalesce_split_messages([
            _update("first thought", update_id=1),
            _update("second thought", update_id=2),
        ])
        self.assertEqual(len(out), 2)

    def test_tail_after_a_completed_merge_is_not_absorbed(self):
        # A(4000)+B(short) is one message; C(short) right after is a new one.
        out = telegram_bot._coalesce_split_messages([
            _update("a" * 4000, update_id=1),
            _update("tail", update_id=2),
            _update("new message", update_id=3),
        ])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["message"]["text"], "new message")

    def test_fragments_from_different_senders_never_merge(self):
        out = telegram_bot._coalesce_split_messages([
            _update("a" * 4000, user_id=1, update_id=1),
            _update("b" * 100, user_id=2, update_id=2),
        ])
        self.assertEqual(len(out), 2)

    def test_non_text_updates_pass_through_untouched(self):
        callback = {"update_id": 2, "callback_query": {"data": "approve:x"}}
        out = telegram_bot._coalesce_split_messages([
            _update("a" * 4000, update_id=1),
            callback,
            _update("b" * 100, update_id=3),
        ])
        self.assertEqual(len(out), 3)


class SplitMessagePollTests(unittest.TestCase):
    """poll_once must deliver a split message as a single pipeline turn,
    including when the fragments straddle a getUpdates batch boundary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.bot = TelegramBot(token="TEST", allowed_user_ids={1}, vault=self.vault, config={})

    def tearDown(self):
        self.tmp.cleanup()

    def _wire_getupdates(self, batches: list[list[dict]]):
        """Feed successive getUpdates calls from a queue; other methods no-op."""
        def call_api(method, params, *, timeout=0):
            if method == "getUpdates" and batches:
                return {"ok": True, "result": batches.pop(0)}
            return {"ok": True, "result": []}
        self.bot._call_api = call_api

    def test_same_batch_fragments_become_one_turn(self):
        self._wire_getupdates([[
            _update("a" * 4000, update_id=10),
            _update("save all of this", update_id=11),
        ]])
        with patch.object(
            telegram_bot, "_process_chat_turn",
            return_value={"response": "ok", "route": "memory", "content_text": "x"},
        ) as proc:
            self.bot.poll_once()
        proc.assert_called_once()
        self.assertEqual(proc.call_args.kwargs["text"], "a" * 4000 + "\nsave all of this")

    def test_fragments_straddling_batches_become_one_turn(self):
        self._wire_getupdates([
            [_update("a" * 4000, update_id=10)],
            [_update("save all of this", update_id=11)],
        ])
        with patch.object(
            telegram_bot, "_process_chat_turn",
            return_value={"response": "ok", "route": "memory", "content_text": "x"},
        ) as proc:
            self.bot.poll_once()
        proc.assert_called_once()
        self.assertEqual(proc.call_args.kwargs["text"], "a" * 4000 + "\nsave all of this")
        self.assertEqual(self.bot._offset, 12)  # both fragments acknowledged


class ApprovalWaitTests(unittest.TestCase):
    """While an approval prompt is pending, only an explicit yes/no decides.
    Conversation landing mid-prompt buffers and the wait continues — before
    2026-07-26 any non-yes text counted as a decline, so a split-message
    fragment arriving mid-prompt silently vetoed the action."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.bot = TelegramBot(token="TEST", allowed_user_ids={1}, vault=self.vault, config={})

    def tearDown(self):
        self.tmp.cleanup()

    def _wire(self, batches: list[list[dict]]):
        def call_api(method, params, *, timeout=0):
            if method == "getUpdates" and batches:
                return {"ok": True, "result": batches.pop(0)}
            return {"ok": True, "result": []}
        self.bot._call_api = call_api

    def test_conversation_mid_prompt_buffers_and_wait_continues(self):
        approve_tap = {"update_id": 3, "callback_query": {"id": "cb", "from": {"id": 1}, "data": "approve:abc"}}
        self._wire([
            [_update("by the way, one more thought", update_id=2)],
            [approve_tap],
        ])
        verdict = self.bot._await_approval(99, "abc", timeout=5.0)
        self.assertTrue(verdict)
        self.assertEqual(len(self.bot._pending_updates), 1)  # the thought survives as conversation

    def test_explicit_no_denies(self):
        self._wire([[_update("no", update_id=2)]])
        self.assertFalse(self.bot._await_approval(99, "abc", timeout=5.0))

    def test_timeout_returns_none(self):
        self._wire([])
        self.assertIsNone(self.bot._await_approval(99, "abc", timeout=0.05))


class TrustWindowTests(unittest.TestCase):
    """/trust opens a bounded window in which gated actions run without the
    per-step prompt, each announced. It expires, ends on /trust off, and
    does not survive /new."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.calls: list[tuple[str, dict]] = []
        self.bot = TelegramBot(token="TEST", allowed_user_ids={1}, vault=self.vault, config={})
        self.bot._call_api = lambda method, params, *, timeout=0: (
            self.calls.append((method, params)) or {"ok": True, "result": []}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _sent(self) -> list[str]:
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def test_trust_window_auto_approves_and_announces(self):
        self.bot.handle_update(_update("/trust 30m"))
        self.assertGreater(self.bot._state_for(99).trust_until, time.time())
        approve = self.bot._approval_fn_for(99)
        self.assertTrue(approve("run_codex", {"task": "touch a file"}))
        self.assertTrue(any("Auto-approved (standing trust)" in s for s in self._sent()))
        # no approval keyboard was ever shown
        self.assertFalse(any("I need your approval" in s for s in self._sent()))

    def test_trust_off_restores_prompting(self):
        self.bot.handle_update(_update("/trust 30m"))
        self.bot.handle_update(_update("/trust off"))
        self.assertEqual(self.bot._state_for(99).trust_until, 0.0)

    def test_new_conversation_drops_trust(self):
        self.bot.handle_update(_update("/trust 30m"))
        with patch.object(telegram_bot, "_process_chat_turn"):
            self.bot.handle_update(_update("/new"))
        self.assertEqual(self.bot._state_for(99).trust_until, 0.0)

    def test_expired_trust_asks_again(self):
        state = self.bot._state_for(99)
        state.trust_until = time.time() - 1
        approve = self.bot._approval_fn_for(99)
        # No reply queued: prompt goes out, wait times out quickly via patch
        with patch.object(self.bot, "_await_approval", return_value=None):
            self.assertFalse(approve("run_codex", {"task": "touch a file"}))
        self.assertTrue(any("I need your approval" in s for s in self._sent()))

    def test_trust_cap_is_eight_hours(self):
        self.bot.handle_update(_update("/trust 99h"))
        self.assertLessEqual(self.bot._state_for(99).trust_until, time.time() + 8 * 3600 + 5)


class BotDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.calls: list[tuple[str, dict]] = []
        self.bot = TelegramBot(
            token="TEST",
            allowed_user_ids={1},
            vault=self.vault,
            config={},
        )
        # Capture every Telegram API call instead of hitting the network.
        self.bot._call_api = lambda method, params, *, timeout=0: (
            self.calls.append((method, params)) or {"ok": True, "result": []}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _sent(self) -> list[str]:
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def test_unauthorized_user_is_blocked(self):
        with patch.object(telegram_bot, "_process_chat_turn") as proc:
            self.bot.handle_update(_update("hello", user_id=999))
        proc.assert_not_called()
        self.assertIn("Not authorized.", self._sent())

    def test_authorized_message_is_processed_and_replied(self):
        with patch.object(
            telegram_bot, "_process_chat_turn",
            return_value={"response": "hi there", "route": "advice", "topic": "t", "content_text": "hello"},
        ) as proc:
            self.bot.handle_update(_update("hello"))
        proc.assert_called_once()
        self.assertEqual(proc.call_args.kwargs["text"], "hello")
        self.assertIn("hi there", self._sent())
        # typing indicator should have been sent before the reply
        self.assertIn("sendChatAction", [m for m, _ in self.calls])

    def test_typing_indicator_survives_a_long_turn_and_stops_after(self):
        """Telegram drops a chat action after ~5s; a slow turn must refresh
        it until the reply is ready — and stop refreshing afterwards."""
        import time as _time

        self.bot.typing_refresh_seconds = 0.05

        def slow_turn(**kwargs):
            _time.sleep(0.3)
            return {"response": "done", "route": "advice", "topic": "t", "content_text": "x"}

        with patch.object(telegram_bot, "_process_chat_turn", side_effect=slow_turn):
            self.bot.handle_update(_update("think hard"))
        during = len([m for m, _ in self.calls if m == "sendChatAction"])
        self.assertGreaterEqual(during, 3)  # refreshed, not fired once
        _time.sleep(0.2)  # were it still running, more actions would land
        after = len([m for m, _ in self.calls if m == "sendChatAction"])
        self.assertEqual(during, after)

    def test_help_command_does_not_invoke_pipeline(self):
        with patch.object(telegram_bot, "_process_chat_turn") as proc:
            self.bot.handle_update(_update("/help"))
        proc.assert_not_called()
        self.assertTrue(any("Commands:" in s for s in self._sent()))

    def test_new_command_rotates_conversation_id(self):
        first = self.bot._state_for(99).conversation_id
        with patch.object(telegram_bot, "_process_chat_turn") as proc:
            self.bot.handle_update(_update("/new"))
        proc.assert_not_called()
        self.assertNotEqual(self.bot._state_for(99).conversation_id, first)

    def test_long_response_is_chunked_across_messages(self):
        long = "y" * 9000
        with patch.object(
            telegram_bot, "_process_chat_turn",
            return_value={"response": long, "route": "memory", "content_text": "x"},
        ):
            self.bot.handle_update(_update("remember this"))
        sends = self._sent()
        self.assertEqual(len(sends), 3)
        self.assertTrue(all(len(s) <= telegram_bot._MSG_LIMIT for s in sends))

    def test_swallowed_pipeline_error_is_reported_not_silent(self):
        # _process_chat_turn catches generic exceptions internally, returning an
        # empty response with result["error"] set — the bot must surface it.
        with patch.object(
            telegram_bot, "_process_chat_turn",
            return_value={"response": "", "error": "codex binary not found", "route": "memory"},
        ):
            self.bot.handle_update(_update("hello"))
        sends = self._sent()
        self.assertEqual(len(sends), 1)
        self.assertIn("codex binary not found", sends[0])
        self.assertNotIn("(no response)", sends[0])

    def test_empty_response_without_error_still_says_something(self):
        with patch.object(
            telegram_bot, "_process_chat_turn",
            return_value={"response": "", "route": "memory"},
        ):
            self.bot.handle_update(_update("hello"))
        sends = self._sent()
        self.assertEqual(len(sends), 1)
        self.assertIn("logged", sends[0])

    def test_non_text_update_ignored(self):
        with patch.object(telegram_bot, "_process_chat_turn") as proc:
            self.bot.handle_update({"update_id": 5, "message": {"chat": {"id": 99}, "from": {"id": 1}}})
        proc.assert_not_called()
        self.assertEqual(self.calls, [])


class ResolveSettingsTests(unittest.TestCase):
    def test_env_takes_precedence_and_parses_allowlist(self):
        with patch.dict("os.environ", {"LISAN_TELEGRAM_TOKEN": "abc", "LISAN_TELEGRAM_ALLOWED": "1, 2 ,x,3"}, clear=False):
            token, allowed = _resolve_settings({"telegram": {"token": "ignored"}})
        self.assertEqual(token, "abc")
        self.assertEqual(allowed, {1, 2, 3})

    def test_falls_back_to_config_block(self):
        import os
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("LISAN_TELEGRAM_TOKEN", None)
            os.environ.pop("LISAN_TELEGRAM_ALLOWED", None)
            token, allowed = _resolve_settings({"telegram": {"token": "cfgtok", "allowed_user_ids": [7, 8]}})
        self.assertEqual(token, "cfgtok")
        self.assertEqual(allowed, {7, 8})

    def test_include_env_false_ignores_env(self):
        with patch.dict("os.environ", {"LISAN_TELEGRAM_TOKEN": "envtok", "LISAN_TELEGRAM_ALLOWED": "5"}, clear=False):
            token, allowed = _resolve_settings({"telegram": {}}, include_env=False)
        self.assertEqual(token, "")
        self.assertEqual(allowed, set())


class ServiceInstallGuardTests(unittest.TestCase):
    """install-service must validate against config.json alone: the detached
    service never inherits the installing shell's env, so env-only settings
    would produce a crash-looping service."""

    def test_env_only_settings_refuse_install(self):
        env = {"LISAN_TELEGRAM_TOKEN": "123:tok", "LISAN_TELEGRAM_ALLOWED": "1"}
        with patch.dict("os.environ", env, clear=False), \
                patch.object(telegram_bot, "load_config", return_value={}), \
                patch.object(telegram_bot, "_install_launchd") as launchd, \
                patch.object(telegram_bot, "_install_systemd") as systemd:
            rc = telegram_bot.install_service(vault=Path("/tmp/nowhere"))
        self.assertEqual(rc, 1)
        launchd.assert_not_called()
        systemd.assert_not_called()

    def test_unconfigured_refuses_install(self):
        import os
        with patch.dict("os.environ", {}, clear=False), \
                patch.object(telegram_bot, "load_config", return_value={}), \
                patch.object(telegram_bot, "_install_launchd") as launchd:
            os.environ.pop("LISAN_TELEGRAM_TOKEN", None)
            os.environ.pop("LISAN_TELEGRAM_ALLOWED", None)
            rc = telegram_bot.install_service(vault=Path("/tmp/nowhere"))
        self.assertEqual(rc, 1)
        launchd.assert_not_called()

    def test_config_persisted_settings_install(self):
        cfg = {"telegram": {"token": "123:tok", "allowed_user_ids": [1]}}
        with patch.object(telegram_bot, "load_config", return_value=cfg), \
                patch.object(telegram_bot.platform, "system", return_value="Darwin"), \
                patch.object(telegram_bot, "_install_launchd", return_value=0) as launchd:
            rc = telegram_bot.install_service(vault=Path("/tmp/nowhere"))
        self.assertEqual(rc, 0)
        launchd.assert_called_once()


class WizardTests(unittest.TestCase):
    def test_token_format_validation(self):
        self.assertTrue(_valid_token_format("123456789:ABCdefGHI-jklMNOpqrSTUvwxYZ012345678"))
        self.assertFalse(_valid_token_format("not-a-token"))
        self.assertFalse(_valid_token_format("123:short"))

    def test_get_me_returns_bot_on_ok(self):
        api = lambda token, method, params, *, timeout=0: {"ok": True, "result": {"username": "lisanbot", "first_name": "Lisan"}}
        self.assertEqual(get_me("tok", api=api)["username"], "lisanbot")

    def test_get_me_returns_none_on_failure(self):
        self.assertIsNone(get_me("tok", api=lambda *a, **k: {"ok": False}))
        def boom(*a, **k):
            raise OSError("network")
        self.assertIsNone(get_me("tok", api=boom))

    def test_detect_owner_id_captures_sender(self):
        payload = {"result": [{"update_id": 7, "message": {"from": {"id": 4242, "first_name": "Augie"}}}]}
        got = detect_owner_id("tok", api=lambda *a, **k: payload, max_wait=5)
        self.assertEqual(got, (4242, "Augie"))

    def test_detect_owner_id_times_out(self):
        self.assertIsNone(detect_owner_id("tok", api=lambda *a, **k: {"result": []}, max_wait=0))

    def test_save_settings_roundtrips_and_resolves(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.json"
            save_telegram_settings("123:tok", [1, 2], path=cfg_path)
            import json
            saved = json.loads(cfg_path.read_text())
            self.assertEqual(saved["telegram"]["token"], "123:tok")
            self.assertEqual(saved["telegram"]["allowed_user_ids"], [1, 2])
            # and the runtime resolver reads it back (env cleared)
            import os
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("LISAN_TELEGRAM_TOKEN", None)
                os.environ.pop("LISAN_TELEGRAM_ALLOWED", None)
                token, allowed = _resolve_settings(saved)
            self.assertEqual(token, "123:tok")
            self.assertEqual(allowed, {1, 2})


class SchedulerThreadTests(unittest.TestCase):
    """The bot process hosts the scheduler loop; due tasks must deliver
    through the bot's own session, owner-only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "jobs.sqlite"
        self.calls: list[tuple[str, dict]] = []
        self.bot = TelegramBot(token="TEST", allowed_user_ids={1}, vault=self.vault, config={})
        self.bot._call_api = lambda method, params, *, timeout=0: (
            self.calls.append((method, params)) or {"ok": True, "result": []}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _run_until_delivered(self, timeout: float = 5.0) -> list[dict]:
        thread, stop = telegram_bot._start_scheduler_thread(
            self.bot, vault=self.vault, db_path=self.db, provider=None, model=None,
            allowed={1}, config={"scheduler": {"poll_seconds": 0.05}},
        )
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if any(m == "sendMessage" for m, _ in self.calls):
                    break
                time.sleep(0.02)
        finally:
            stop.set()
            thread.join(timeout=2)
        return [p for m, p in self.calls if m == "sendMessage"]

    def test_due_reminder_delivers_through_bot_session(self):
        from lisan.tools.jobs import enqueue_job, get_job, list_jobs

        job_id = enqueue_job(
            "task.reminder",
            {"message": "thread check", "due": "2020-01-01T00:00:00Z", "chat_id": 1},
            scheduled_for="2020-01-01T00:00:00Z",
            db_path=self.db,
        )
        sends = self._run_until_delivered()
        self.assertEqual(len(sends), 1)
        self.assertIn("thread check", sends[0]["text"])
        self.assertEqual(sends[0]["chat_id"], 1)
        self.assertEqual(get_job(job_id, db_path=self.db)["status"], "succeeded")

    def test_non_allowlisted_chat_falls_back_to_owner(self):
        from lisan.tools.jobs import enqueue_job

        enqueue_job(
            "task.reminder",
            {"message": "for the owner", "due": "2020-01-01T00:00:00Z", "chat_id": 999},
            scheduled_for="2020-01-01T00:00:00Z",
            db_path=self.db,
        )
        sends = self._run_until_delivered()
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["chat_id"], 1)


class ServiceRenderTests(unittest.TestCase):
    def test_launchd_plist_has_exec_vault_and_label(self):
        plist = _render_launchd_plist(
            label="com.lisan.telegram",
            python="/venv/bin/python",
            vault=Path("/home/me/.lisan/vault"),
            repo_dir=Path("/home/me/.lisan/repo"),
            out_log=Path("/x/out.log"),
            err_log=Path("/x/err.log"),
        )
        self.assertIn("<string>com.lisan.telegram</string>", plist)
        self.assertIn("<string>/venv/bin/python</string>", plist)
        self.assertIn("<string>telegram</string>", plist)
        self.assertIn("<string>/home/me/.lisan/vault</string>", plist)
        self.assertIn("<key>RunAtLoad</key>", plist)
        self.assertIn("<key>KeepAlive</key>", plist)

    def test_systemd_unit_has_execstart_and_restart(self):
        unit = _render_systemd_unit(python="/venv/bin/python", vault=Path("/home/me/.lisan/vault"))
        self.assertIn("ExecStart=/venv/bin/python -m lisan telegram run --vault /home/me/.lisan/vault", unit)
        self.assertIn('Environment="LISAN_VAULT=/home/me/.lisan/vault"', unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_launchd_plist_carries_path_env(self):
        # Detached services get a minimal PATH; without the installing shell's
        # PATH embedded, provider binaries like codex are unreachable.
        plist = _render_launchd_plist(
            label="com.lisan.telegram",
            python="/venv/bin/python",
            vault=Path("/v"),
            repo_dir=Path("/r"),
            out_log=Path("/x/out.log"),
            err_log=Path("/x/err.log"),
            path_env="/usr/local/bin:/usr/bin",
        )
        self.assertIn("<key>PATH</key>", plist)
        self.assertIn("<string>/usr/local/bin:/usr/bin</string>", plist)

    def test_systemd_unit_carries_path_env(self):
        unit = _render_systemd_unit(python="/venv/bin/python", vault=Path("/v"), path_env="/usr/local/bin:/usr/bin")
        self.assertIn('Environment="PATH=/usr/local/bin:/usr/bin"', unit)


if __name__ == "__main__":
    unittest.main()
