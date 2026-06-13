from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools import telegram_bot
from lisan.tools.telegram_bot import (
    TelegramBot,
    _chunk,
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
            cfg_path = Path(d) / "config.yaml"
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


if __name__ == "__main__":
    unittest.main()
