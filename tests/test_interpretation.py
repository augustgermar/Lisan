"""IIP Phase 1: detector, validator, enforcement loop, logging, kill switch.

The class this closes: asked to interpret a person's behavior, the agent
produced hypotheses that all lived in one person's psychology — no
user-causal branch, no boring base-rate branch, no discriminators, no
convergent action. Structure is now demanded deterministically; the model
still writes the prose. All fixtures use the repository's invented cast.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.interpretation import (
    LOG_NAME,
    incompleteness_notice,
    is_interpretation_query,
    validate_interpretation,
)


def _good_interpretation() -> dict:
    return {
        "response": "A few ways to read it...",
        "interpretation": {
            "hypotheses": [
                {"text": "Josie re-asks because repetition is her regulation strategy",
                 "locus": "situational_baserate", "provenance": []},
                {"text": "Your own answers have been hedged, so re-asking is rational",
                 "locus": "user_causal", "provenance": []},
                {"text": "Ruth may be seeking reassurance about the new dynamic",
                 "locus": "other_person", "provenance": []},
            ],
            "discriminators": ["Replay your last three answers: were any unqualified?"],
            "convergent_action": "One definitive written answer with logistics attached.",
        },
    }


class DetectorTests(unittest.TestCase):
    def test_golden_phrasings_fire(self):
        for text in (
            "why do they keep asking me if I'm coming on the camping trip?",
            "what do you make of this text from Ruth?",
            "how should I read her silence since Tuesday?",
            "help me understand what Dana meant by that",
            "why does Ruth keep bringing up the schedule?",
            "she keeps texting me about the weekend — is she hoping I'll cancel?",
        ):
            self.assertTrue(is_interpretation_query(text), text)

    def test_negative_controls(self):
        for text in (
            "are you there?",                                # self-question
            "what's your current system status",             # self-question
            "why does the scheduler keep failing?",          # system, not a person
            "I finished the laundry and started dinner",     # statement
            "remind me to call the dentist at 3",            # task
            "",
        ):
            self.assertFalse(is_interpretation_query(text), text)


class ValidatorTests(unittest.TestCase):
    def test_compliant_payload_passes(self):
        self.assertEqual(validate_interpretation(_good_interpretation()), [])

    def test_missing_object_is_one_complaint(self):
        self.assertEqual(
            validate_interpretation({"response": "hm"}),
            ["missing the required interpretation object"],
        )

    def test_single_locus_hypothesis_space_fails_with_named_gaps(self):
        payload = _good_interpretation()
        payload["interpretation"]["hypotheses"] = [
            {"text": "She is anxious", "locus": "other_person", "provenance": []},
            {"text": "She is conflict-avoidant", "locus": "other_person", "provenance": []},
        ]
        complaints = " | ".join(validate_interpretation(payload))
        self.assertIn("user_causal", complaints)
        self.assertIn("situational_baserate", complaints)

    def test_empty_discriminators_and_action_fail(self):
        payload = _good_interpretation()
        payload["interpretation"]["discriminators"] = []
        payload["interpretation"]["convergent_action"] = "  "
        complaints = " | ".join(validate_interpretation(payload))
        self.assertIn("discriminators", complaints)
        self.assertIn("convergent_action", complaints)

    def test_explicit_none_action_passes(self):
        payload = _good_interpretation()
        payload["interpretation"]["convergent_action"] = "none — the decision forks on whether the trip is on"
        self.assertEqual(validate_interpretation(payload), [])

    def test_empty_provenance_is_valid_but_fake_refs_are_not(self):
        # Owner decree: [] is legitimate — an out-of-register hypothesis has
        # nothing in the corpus to cite. Cited refs must resolve, though.
        payload = _good_interpretation()
        self.assertEqual(validate_interpretation(payload), [])
        payload["interpretation"]["hypotheses"][0]["provenance"] = ["claim.invented"]
        with patch("lisan.tools.interpretation._ref_resolves", return_value=False):
            complaints = " | ".join(validate_interpretation(payload))
        self.assertIn("claim.invented", complaints)

    def test_invalid_locus_is_named(self):
        payload = _good_interpretation()
        payload["interpretation"]["hypotheses"][0]["locus"] = "vibes"
        self.assertTrue(any("vibes" in c for c in validate_interpretation(payload)))

    def test_notice_names_whats_missing(self):
        note = incompleteness_notice(["no user_causal hypothesis present"])
        self.assertIn("you are a causal factor", note)


class EnforcementLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.query = "why does Ruth keep asking whether I'm coming to the reunion?"

    def tearDown(self):
        self.tmp.cleanup()

    def _run_turn(self, responses: list[dict], config: dict | None = None) -> tuple[dict, int]:
        from lisan.tools.conversation import run_conversation_turn

        calls = {"n": 0}

        def _scripted(agent_self, user_input, **kwargs):
            out = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return out

        with patch("lisan.agents.conversation.ConversationAgent.run_json", _scripted), \
                patch("lisan.tools.conversation.load_config",
                      create=True, return_value={"iip": config or {}}), \
                patch("lisan.config.load_config", return_value={"iip": config or {}}):
            result = run_conversation_turn(
                vault=self.vault, text=self.query,
                conversation_id="t1", db_path=self.root / "x.sqlite",
                queue_capture=False,
            )
        return result, calls["n"]

    def _log_lines(self) -> list[dict]:
        path = self.vault / "logs" / LOG_NAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_compliant_first_pass_no_regeneration(self):
        result, calls = self._run_turn([_good_interpretation()])
        self.assertEqual(calls, 1)
        self.assertNotIn("incomplete", result["response"])
        events = self._log_lines()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["validated"], "pass")
        self.assertEqual(events[0]["regenerations"], 0)
        self.assertNotIn(self.query, json.dumps(events))  # digest only, never text

    def test_noncompliant_then_compliant_regenerates_once(self):
        result, calls = self._run_turn([
            {"response": "she's just anxious"},
            _good_interpretation(),
        ])
        self.assertEqual(calls, 2)
        events = self._log_lines()
        self.assertEqual(events[0]["validated"], "pass")
        self.assertEqual(events[0]["regenerations"], 1)

    def test_exhaustion_renders_notice_and_stops_at_cap(self):
        bad = {"response": "she's just anxious, that's all"}
        result, calls = self._run_turn([bad, bad, bad])
        self.assertEqual(calls, 2)  # owner cap N=1: initial + one regeneration
        self.assertIn("may be incomplete", result["response"])
        events = self._log_lines()
        self.assertEqual(events[0]["validated"], "incomplete")

    def test_kill_switch_disables_enforcement_but_still_logs(self):
        result, calls = self._run_turn(
            [{"response": "she's just anxious"}],
            config={"validator_enabled": False},
        )
        self.assertEqual(calls, 1)
        self.assertNotIn("incomplete", result["response"])
        events = self._log_lines()
        self.assertEqual(events[0]["validated"], "disabled")

    def test_ordinary_turn_fires_nothing(self):
        from lisan.tools.conversation import run_conversation_turn

        with patch(
            "lisan.agents.conversation.ConversationAgent.run_json",
            lambda a, u, **k: {"response": "noted"},
        ):
            run_conversation_turn(
                vault=self.vault, text="I finished the laundry today",
                conversation_id="t2", db_path=self.root / "x.sqlite",
                queue_capture=False,
            )
        self.assertEqual(self._log_lines(), [])


if __name__ == "__main__":
    unittest.main()
