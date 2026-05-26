from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from lisan.frontmatter import load_markdown
from lisan.config import load_config
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.capture import capture_text
from lisan.tools.chat import _process_chat_turn, run_chat, startup_check
from lisan.tools.memory_pipeline import _create_decisions
from lisan.tools.transcripts import append_transcript
from lisan.tools.tracing import list_recent_turn_traces, load_turn_trace
from lisan.providers.base import LLMResponse, ProviderError


class ChatPerformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"
        self.client = MagicMock()
        self.client.complete.side_effect = self._complete_ok
        self.sqlite_patch = patch("lisan.providers.base.sqlite_path", side_effect=lambda base=None: self.db_path)
        self.client_patch = patch("lisan.providers.base._client_for", return_value=self.client)
        self.sqlite_patch.start()
        self.client_patch.start()

    def tearDown(self) -> None:
        self.client_patch.stop()
        self.sqlite_patch.stop()
        self.tmp.cleanup()

    def _complete_ok(self, prompt: str, *, schema=None, temperature=0.2, agent="writer", significance="medium", model=None):
        if agent == "listener":
            text = json.dumps(
                {
                    "worth_remembering": True,
                    "mode": "writer",
                    "reason": ["memory-worthy"],
                    "memory_events": [],
                    "action": "full",
                    "score": 9,
                    "seed_score": 8,
                    "narrative_score": 1,
                }
            )
        elif agent == "writer":
            text = json.dumps(
                {
                    "record_type": "episode",
                    "summary": "Memory draft",
                    "significance": "medium",
                    "frontmatter": {
                        "summary": "Memory draft",
                        "significance": "medium",
                        "confidence": "low",
                        "confidence_basis": "test",
                        "review_after": "",
                        "links": [],
                    },
                    "sections": {"event_timeline": prompt[:80]},
                    "questions": ["What detail matters most here?"],
                    "significance_rationale": "test",
                    "entities_to_create": [{"name": "Person A", "subtype": "person", "summary": "Person A mentioned in conversation."}],
                    "evidence_to_create": [{"title": "Conversation evidence", "summary": "Conversation evidence", "source_type": "manual_note", "arena": "cross_arena", "reliability": "medium", "sensitivity": "low"}],
                    "claims_to_create": [{"claim_text": "Person A spoke about a memory.", "status": "active", "confidence": 0.6, "summary": "Person A spoke about a memory."}],
                    "state_updates": [{"category": "relational", "summary": "Person A mentioned a personal memory.", "confidence": "low"}],
                    "open_loops_to_create": [],
                    "decisions_to_create": [],
                }
            )
        elif agent == "skeptic":
            text = json.dumps(
                {
                    "approved": True,
                    "approved_for_dreamer": True,
                    "issues": [],
                    "risk": "low",
                    "recommended_action": "approve",
                    "priority_questions": [],
                    "observed_facts": [],
                    "interpretations": [],
                    "alternative_hypotheses": [],
                    "evidence_needed": [],
                    "claim_updates": [],
                    "confidence_adjustments": [],
                    "reasoning_errors": [],
                    "reviewed_record_id": "",
                    "reviewed_record_type": "draft",
                    "pattern_status": "approved",
                    "counterexample_search": {"performed": True},
                    "summary": "Approved",
                }
            )
        elif agent == "interlocutor":
            text = json.dumps(
                {
                    "response": "Got it.",
                    "questions": [],
                    "recommended_action": "auto_commit",
                    "updated_narrative_state": {"next_step": "Continue", "mode_status": "developing"},
                }
            )
        elif agent == "elicitor":
            text = json.dumps(
                {
                    "response": "Tell me more.",
                    "updated_narrative_state": {"mode_status": "developing", "next_step": "Continue"},
                    "questions": [],
                }
            )
        elif agent == "advice":
            text = "Sure."
        else:
            text = "OK"
        return LLMResponse(text=text, provider="local", model="fake")

    def test_hi_uses_fast_path_and_no_llm_calls(self) -> None:
        result = _process_chat_turn(
            vault=self.vault,
            conversation_id="demo",
            text="hi",
            provider=None,
            model=None,
            advice_history=[],
            advice_context_active=False,
            advice_topic=None,
            domain_override=None,
            db_path=self.db_path,
        )

        self.assertIn("Lisan", result["response"])
        self.assertTrue(result["fast_path_used"])
        self.assertEqual(len(result["trace"]["llm_calls"]), 0)
        self.assertFalse(result["trace"]["retrieval_used"])
        self.assertEqual(result["trace"]["jobs_queued"], 0)
        self.assertEqual(self.client.complete.call_count, 0)

    def test_identity_questions_answer_lisan_without_llm(self) -> None:
        for text in ["what is your name?", "what are you?"]:
            with self.subTest(text=text):
                result = _process_chat_turn(
                    vault=self.vault,
                    conversation_id="demo",
                    text=text,
                    provider=None,
                    model=None,
                    advice_history=[],
                    advice_context_active=False,
                    advice_topic=None,
                    domain_override=None,
                    db_path=self.db_path,
                )
                self.assertIn("Lisan", result["response"])
                self.assertEqual(len(result["trace"]["llm_calls"]), 0)
                self.assertFalse(result["trace"]["retrieval_used"])

    def test_startup_check_shows_local_provider_error(self) -> None:
        self.client.complete.side_effect = ProviderError("Connection refused to http://localhost:11434/v1/chat/completions")
        config = load_config()
        buffer = io.StringIO()

        with redirect_stdout(buffer):
            ready = startup_check(self.vault, config)

        output = buffer.getvalue()
        self.assertFalse(ready)
        self.assertIn("Provider: local not reachable", output)
        self.assertIn("Connection refused to http://localhost:11434/v1/chat/completions", output)

    def test_thanks_schedules_no_background_jobs(self) -> None:
        result = _process_chat_turn(
            vault=self.vault,
            conversation_id="demo",
            text="thanks",
            provider=None,
            model=None,
            advice_history=[],
            advice_context_active=False,
            advice_topic=None,
            domain_override=None,
            db_path=self.db_path,
        )

        self.assertEqual(result["queued_jobs"], [])
        self.assertEqual(result["trace"]["jobs_queued"], 0)
        self.assertEqual(self.client.complete.call_count, 0)

    def test_trivial_turn_does_not_run_retrieval_or_background_analysis(self) -> None:
        result = _process_chat_turn(
            vault=self.vault,
            conversation_id="demo",
            text="cool. what are you up to?",
            provider=None,
            model=None,
            advice_history=[],
            advice_context_active=False,
            advice_topic=None,
            domain_override=None,
            db_path=self.db_path,
        )

        self.assertTrue(result["trace"]["fast_path_used"])
        self.assertFalse(result["trace"]["retrieval_used"])
        self.assertEqual(result["trace"]["jobs_queued"], 0)
        self.assertEqual(result["queued_jobs"], [])

    def test_retrieved_alice_entity_cannot_override_identity(self) -> None:
        child = self.vault / "entities" / "people" / "child-one.md"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text(
            "---\n"
            "id: entity.child_one\n"
            "type: entity\n"
            "created: 2026-05-25\n"
            "updated: 2026-05-25\n"
            "status: active\n"
            "significance: low\n"
            "domain_primary: relational\n"
            "domain_secondary: []\n"
            "privacy: personal\n"
            "compartments: []\n"
            "allowed_contexts: [all]\n"
            "blocked_contexts: []\n"
            "summary: Child One is a person mentioned in the vault.\n"
            "links: []\n"
            "confidence: low\n"
            "confidence_basis: seed\n"
            "last_confirmed: 2026-05-25\n"
            "review_after: 2026-05-25\n"
            "subtype: person\n"
            "canonical_name: Child One\n"
            "aliases: []\n"
            "disambiguation: test entity\n"
            "epoch: 1\n"
            "epoch_started: 2026-05-25\n"
            "previous_epochs: []\n"
            "---\n"
            "# Child One\n\nChild One is just data.\n",
            encoding="utf-8",
        )

        result = _process_chat_turn(
            vault=self.vault,
            conversation_id="demo",
            text="what is your name?",
            provider=None,
            model=None,
            advice_history=[],
            advice_context_active=False,
            advice_topic=None,
            domain_override=None,
            db_path=self.db_path,
        )

        self.assertEqual(result["response"], "My name is Lisan. I am your local personal assistant and memory system.")
        self.assertFalse(result["trace"]["retrieval_used"])
        self.assertEqual(len(result["trace"]["llm_calls"]), 0)

    def test_trace_records_all_llm_calls_and_elapsed_time(self) -> None:
        memory_text = "/remember my name is Person A and I have two pets named Pet One and Pet Two."
        result = _process_chat_turn(
            vault=self.vault,
            conversation_id="demo",
            text=memory_text,
            provider=None,
            model=None,
            advice_history=[],
            advice_context_active=False,
            advice_topic=None,
            domain_override=None,
            db_path=self.db_path,
        )

        trace = result["trace"]
        self.assertFalse(trace["fast_path_used"])
        self.assertGreaterEqual(len(trace["llm_calls"]), 3)
        self.assertTrue(all("elapsed_ms" in call for call in trace["llm_calls"]))
        self.assertTrue(all("error_type" in call for call in trace["llm_calls"]))
        self.assertTrue(any(step.startswith("memory_pipeline.") for step in trace["inline_steps"]))

        recent = list_recent_turn_traces(limit=5, db_path=self.db_path)
        self.assertTrue(recent)
        self.assertEqual(recent[0]["turn_id"], trace["turn_id"])
        loaded = load_turn_trace(trace["turn_id"], db_path=self.db_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded["llm_calls"]), len(trace["llm_calls"]))

    def test_memory_turn_can_queue_background_jobs(self) -> None:
        memory_text = "/remember my name is Person A and I have two pets named Pet One and Pet Two."
        result = _process_chat_turn(
            vault=self.vault,
            conversation_id="demo",
            text=memory_text,
            provider=None,
            model=None,
            advice_history=[],
            advice_context_active=False,
            advice_topic=None,
            domain_override=None,
            db_path=self.db_path,
        )

        self.assertFalse(result["trace"]["fast_path_used"])
        self.assertGreater(len(result["queued_jobs"]), 0)
        self.assertEqual(result["trace"]["jobs_queued"], len(result["queued_jobs"]))

    def test_failed_provider_call_returns_explicit_error_and_no_memory(self) -> None:
        self.client.complete.side_effect = ProviderError("permission denied creating session files")
        result = _process_chat_turn(
            vault=self.vault,
            conversation_id="demo",
            text="my name is Person A",
            provider=None,
            model=None,
            advice_history=[],
            advice_context_active=False,
            advice_topic=None,
            domain_override=None,
            db_path=self.db_path,
        )

        self.assertTrue(result.get("provider_failure"))
        self.assertIn("The local model provider failed before I could answer.", result["response"])
        self.assertIn("permission denied", result["response"].lower())
        self.assertEqual(result["queued_jobs"], [])
        self.assertEqual(result["trace"]["jobs_queued"], 0)
        self.assertEqual(result["trace"]["retrieval_used"], False)
        self.assertEqual(result["trace"]["llm_calls"][0]["success"], False)
        self.assertEqual(result["trace"]["llm_calls"][0]["error_type"], "ProviderError")
        durable_paths = [
            path for path in self.vault.rglob("*.md")
            if any(part in {"entities", "episodes", "knowledge", "evidence", "claims", "state", "open_loops", "decisions", "patterns", "drafts"} for part in path.parts)
        ]
        self.assertFalse(durable_paths)

    def test_capture_appends_lisan_response_to_transcript(self) -> None:
        transcript_path = self.vault / "transcripts" / "2026-05-26.md"
        append_transcript(vault=self.vault, conversation_id="demo", speaker="USER", text="Please remember this.")
        fake_result = SimpleNamespace(
            transcript_path=transcript_path,
            draft_path=None,
            listener={"action": "full"},
            writer={},
            skeptic={},
            interlocutor={},
            elicitor={"response": "I will keep that in mind."},
            mode="elicitor",
            action="full",
            narrative_state_path=None,
            narrative_state=None,
        )

        with patch("lisan.tools.capture.run_memory_pipeline", return_value=fake_result):
            out = capture_text(
                vault=self.vault,
                text="Please remember this.",
                conversation_id="demo",
                append_response_to_transcript=True,
                queue_background=False,
            )

        transcript = transcript_path.read_text(encoding="utf-8")
        self.assertIn("USER: Please remember this.", transcript)
        self.assertIn("LISAN: I will keep that in mind.", transcript)
        self.assertEqual(out["response"], "I will keep that in mind.")

    def test_decision_turn_synthesizes_record_when_writer_is_sparse(self) -> None:
        _create_decisions(
            self.vault,
            {
                "record_type": "decision",
                "summary": "Person A decided to automate the weekly reminder.",
                "significance": "medium",
                "frontmatter": {
                    "domain_primary": "work",
                    "alternatives_considered": ["Keep doing it manually"],
                    "revisit_conditions": ["If the reminder stops being useful"],
                },
                "decisions_to_create": [],
            },
        )

        decision_docs = list((self.vault / "decisions").glob("*.md"))
        self.assertEqual(len(decision_docs), 1)
        fm = load_markdown(decision_docs[0]).frontmatter
        self.assertEqual(fm["summary"], "Person A decided to automate the weekly reminder.")
        self.assertEqual(fm["domain_primary"], "work")


if __name__ == "__main__":
    unittest.main()
