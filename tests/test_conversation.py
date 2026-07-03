from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.providers.base import LLMResponse, LisanLLM
from lisan.tools.conversation import run_conversation_turn
from lisan.tools.jobs import list_jobs
from lisan.tools.transcripts import append_transcript


class ConversationTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "jobs.sqlite"
        self.prompts: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_complete(self, reply: str):
        def complete(_self, prompt, *, agent="x", **kwargs):
            self.prompts.append(prompt)
            return LLMResponse(text=json.dumps({"response": reply}), provider="stub", model="s")

        return complete

    def _turn(self, text: str, conversation_id="conv-1", reply="Sounds good."):
        with patch.object(LisanLLM, "complete", self._fake_complete(reply)):
            return run_conversation_turn(
                vault=self.vault, text=text, conversation_id=conversation_id, db_path=self.db,
            )

    def test_agent_sees_the_rolling_conversation(self):
        append_transcript(vault=self.vault, conversation_id="conv-1", speaker="USER",
                          text="lets ingest my obsidian notes")
        append_transcript(vault=self.vault, conversation_id="conv-1", speaker="LISAN",
                          text="Which file should we start with?")
        self._turn("you pick")
        prompt = self.prompts[-1]
        self.assertIn("lets ingest my obsidian notes", prompt)
        self.assertIn("Which file should we start with?", prompt)
        self.assertIn("CONVERSATION", prompt)
        self.assertIn("you pick", prompt)

    def test_both_sides_are_transcribed(self):
        self._turn("hello there friend", reply="Hey. Good to see you.")
        transcript = next((self.vault / "transcripts").glob("*.md")).read_text()
        self.assertIn("hello there friend", transcript)
        self.assertIn("Hey. Good to see you.", transcript)

    def test_capture_observes_in_background(self):
        result = self._turn("I met Dana for coffee today")
        observe = [j for j in list_jobs(db_path=self.db) if j["job_type"] == "capture.observe"]
        self.assertEqual(len(observe), 1)
        payload = observe[0]["payload"]
        self.assertEqual(payload["text"], "I met Dana for coffee today")
        self.assertEqual(payload["response"], "Sounds good.")
        self.assertEqual(result["route"], "conversation")

    def test_empty_reply_is_replaced_with_honest_note(self):
        result = self._turn("say nothing", reply="")
        self.assertIn("failed to produce a reply", result["response"])

    def test_capabilities_present_every_turn(self):
        self._turn("what can you do?")
        self.assertIn("CAPABILITIES", self.prompts[-1])


class TelegramApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        from lisan.tools.telegram_bot import TelegramBot

        self.bot = TelegramBot(token="TEST", allowed_user_ids={1}, vault=vault_root(self.root), config={})
        self.sent: list[str] = []
        self.replies: list[dict] = []

        def call_api(method, params, *, timeout=0):
            if method == "sendMessage":
                self.sent.append(params["text"])
                return {"ok": True, "result": []}
            if method == "getUpdates":
                batch, self.replies = self.replies, []
                return {"ok": True, "result": batch}
            return {"ok": True, "result": []}

        self.bot._call_api = call_api

    def tearDown(self):
        self.tmp.cleanup()

    def _reply(self, text, user_id=1, chat_id=7, update_id=1):
        return {"update_id": update_id,
                "message": {"text": text, "chat": {"id": chat_id}, "from": {"id": user_id}}}

    def test_yes_approves(self):
        self.replies = [self._reply("yes")]
        approve = self.bot._approval_fn_for(7)
        self.assertTrue(approve("run_codex", {"task": "ingest the folder"}))
        self.assertIn("approval", self.sent[0].lower())
        self.assertTrue(any("Approved" in s for s in self.sent))

    def test_anything_else_declines(self):
        self.replies = [self._reply("hmm not now")]
        approve = self.bot._approval_fn_for(7)
        self.assertFalse(approve("run_codex", {"task": "ingest the folder"}))

    def test_unrelated_chat_message_is_buffered_not_consumed(self):
        other = self._reply("what's the weather", chat_id=99, update_id=5)
        self.replies = [other, self._reply("yes", update_id=6)]
        approve = self.bot._approval_fn_for(7)
        self.assertTrue(approve("run_codex", {"task": "x"}))
        self.assertIn(other, self.bot._pending_updates)


    def test_button_tap_approves(self):
        callback = {"update_id": 9, "callback_query": {"id": "cb1", "from": {"id": 1}, "data": "approve:PLACEHOLDER"}}
        # nonce is generated inside approve(); capture it from the sent keyboard
        sent_markup = {}

        def call_api(method, params, *, timeout=0):
            if method == "sendMessage" and "reply_markup" in params:
                data = params["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
                callback["callback_query"]["data"] = data
                sent_markup["keyboard"] = params["reply_markup"]
                return {"ok": True, "result": []}
            if method == "sendMessage":
                self.sent.append(params["text"])
                return {"ok": True, "result": []}
            if method == "getUpdates":
                return {"ok": True, "result": [callback]}
            return {"ok": True, "result": []}

        self.bot._call_api = call_api
        approve = self.bot._approval_fn_for(7)
        self.assertTrue(approve("run_codex", {"task": "x"}))
        self.assertIn("inline_keyboard", sent_markup["keyboard"])

    def test_foreign_button_tap_is_ignored(self):
        stale = {"update_id": 9, "callback_query": {"id": "cb1", "from": {"id": 999}, "data": "approve:whatever"}}
        yes = self._reply("yes", update_id=10)
        self.replies = [stale, yes]
        approve = self.bot._approval_fn_for(7)
        self.assertTrue(approve("run_codex", {"task": "x"}))

    def test_ordinary_message_declines_but_survives(self):
        msg = self._reply("actually tell me about the weather", update_id=11)
        self.replies = [msg]
        approve = self.bot._approval_fn_for(7)
        self.assertFalse(approve("run_codex", {"task": "x"}))
        self.assertIn(msg, self.bot._pending_updates)


if __name__ == "__main__":
    unittest.main()
