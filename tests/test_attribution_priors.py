"""IIP Phase 2: the corpus-adversarial register and its challenge.

Acceptance tests from the brief, invented cast throughout:
- the miner detects, counts, and registers known attribution patterns
  from synthetic story sets (and refuses under-supported ones);
- the challenge fires only when EVERY hypothesis instantiates a
  registered prior, shares the regeneration budget, and marks the reply
  when it cannot be cleared;
- regression: an anomaly-in-the-household query must end with at least
  one hypothesis that does not route through the registered antagonist;
- the weekly summarizer reads the same JSONL the fires write.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.attribution_priors import (
    MIN_SUPPORT,
    hypotheses_all_in_register,
    load_attribution_register,
    mine_attribution_priors,
)
from lisan.tools.record_factory import new_claim, new_entity
from lisan.tools.validator import validate_vault


class PriorsMinerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        new_entity(self.vault, "Ruth Feld", subtype="person", summary="Ruth Feld is a person.")
        new_entity(self.vault, "Dana Varga", subtype="person", summary="Dana Varga is a person.")

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_attribution_claims(self, n: int) -> None:
        texts = [
            "Ruth closed the bakery early, attributed to Dana taking the register",
            "Ruth skipped the reunion because of Dana insisting on the schedule",
            "The change in Ruth's plans was caused by Dana rearranging the weekend",
            "Ruth sold the equipment, a move driven by Dana and her requirements",
        ]
        for i in range(n):
            new_claim(
                self.vault,
                texts[i % len(texts)] + f" (instance {i})",
                owner="user",
                claim_class="interpretation",
                confidence=0.5,
            )


class PriorsMinerTests(PriorsMinerBase):
    def test_pairwise_pattern_detected_counted_and_registered(self):
        self._seed_attribution_claims(MIN_SUPPORT)
        summary = mine_attribution_priors(self.vault)
        self.assertEqual(summary["register_entries_written"], 1)
        register = load_attribution_register(self.vault)
        self.assertEqual(len(register), 1)
        self.assertEqual(register[0]["kind"], "pairwise")
        entries = [p for p in (self.vault / "patterns").glob("*attribution-prior*")]
        fm = load_markdown(entries[0]).frontmatter
        self.assertEqual(fm["pattern_type"], "attribution_prior")
        self.assertEqual(fm["support_count"], MIN_SUPPORT)
        self.assertEqual(len(fm["supporting_records"]), MIN_SUPPORT)
        self.assertLessEqual(float(fm["confidence"]), 0.5)
        report = validate_vault(self.vault)
        self.assertTrue(report.ok, report.summary())

    def test_under_supported_patterns_do_not_register(self):
        self._seed_attribution_claims(MIN_SUPPORT - 1)
        summary = mine_attribution_priors(self.vault)
        self.assertEqual(summary["register_entries_written"], 0)
        self.assertEqual(load_attribution_register(self.vault), [])

    def test_non_originating_frame_detected(self):
        for i in range(MIN_SUPPORT):
            new_claim(
                self.vault,
                f"Ruth never chose the arrangement herself, she went along with it (instance {i})",
                owner="user",
                claim_class="interpretation",
                confidence=0.5,
            )
        mine_attribution_priors(self.vault)
        register = load_attribution_register(self.vault)
        kinds = {entry["kind"] for entry in register}
        self.assertIn("non_originating", kinds)

    def test_rerun_is_idempotent(self):
        self._seed_attribution_claims(MIN_SUPPORT)
        mine_attribution_priors(self.vault)
        mine_attribution_priors(self.vault)
        entries = list((self.vault / "patterns").glob("*attribution-prior*"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(load_markdown(entries[0]).frontmatter["support_count"], MIN_SUPPORT)

    def test_non_agentive_entities_never_register(self):
        # The live first-run bug: a topic entity ("The ONE Thing"-shaped,
        # kind thing) whose leading article matched every sentence became a
        # registered causal agent. Things don't have agency.
        new_entity(self.vault, "The Big Move", subtype="thing", summary="A concept entity.")
        for i in range(MIN_SUPPORT):
            new_claim(
                self.vault,
                f"Ruth skipped the visit because of the big move looming (instance {i})",
                owner="user", claim_class="interpretation", confidence=0.5,
            )
        mine_attribution_priors(self.vault)
        register = load_attribution_register(self.vault)
        targets = {entry["target"] for entry in register}
        self.assertNotIn("entity.the-big-move", targets)


class ChallengeConditionTests(PriorsMinerBase):
    def _register(self):
        self._seed_attribution_claims(MIN_SUPPORT)
        mine_attribution_priors(self.vault)
        return load_attribution_register(self.vault)

    def _payload(self, hypothesis_texts: list[str]) -> dict:
        return {
            "response": "...",
            "interpretation": {
                "hypotheses": [
                    {"text": t, "locus": "other_person", "provenance": []}
                    for t in hypothesis_texts
                ],
                "discriminators": ["x"],
                "convergent_action": "y",
            },
        }

    def test_all_in_register_fires(self):
        register = self._register()
        payload = self._payload([
            "Dana is orchestrating the schedule again",
            "This is Dana applying pressure through the kids",
        ])
        matched = hypotheses_all_in_register(payload, register)
        self.assertTrue(matched)

    def test_one_outside_hypothesis_clears_it(self):
        # The antagonist-routing regression: one reading that does not
        # route through the registered entity means no challenge.
        register = self._register()
        payload = self._payload([
            "Dana is orchestrating the schedule again",
            "Ordinary end-of-summer logistics; plans always churn in August",
        ])
        self.assertIsNone(hypotheses_all_in_register(payload, register))

    def test_empty_register_never_fires(self):
        payload = self._payload(["Dana did it"])
        self.assertIsNone(hypotheses_all_in_register(payload, []))


class ChallengeEnforcementTests(PriorsMinerBase):
    def setUp(self):
        super().setUp()
        self._seed_attribution_claims(MIN_SUPPORT)
        mine_attribution_priors(self.vault)
        self.query = "why does Ruth keep changing the weekend plans on me?"

    def _turn(self, responses: list[dict]) -> tuple[dict, int]:
        from lisan.tools.conversation import run_conversation_turn

        calls = {"n": 0}

        def _scripted(agent_self, user_input, **kwargs):
            out = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return out

        with patch("lisan.agents.conversation.ConversationAgent.run_json", _scripted), \
                patch("lisan.config.load_config", return_value={"iip": {}}):
            result = run_conversation_turn(
                vault=self.vault, text=self.query,
                conversation_id="t1", db_path=self.root / "x.sqlite",
                queue_capture=False,
            )
        return result, calls["n"]

    def _structured(self, hypothesis_texts: list[str]) -> dict:
        return {
            "response": "Here's how I read it...",
            "interpretation": {
                "hypotheses": [
                    {"text": hypothesis_texts[0], "locus": "user_causal", "provenance": []},
                    {"text": hypothesis_texts[1], "locus": "situational_baserate", "provenance": []},
                ],
                "discriminators": ["compare the last three weekends"],
                "convergent_action": "confirm the plan in writing once",
            },
        }

    def test_challenge_regenerates_and_clears(self):
        all_in = self._structured([
            "your reaction feeds what Dana is engineering",
            "Dana rearranges things; that's the baseline with Dana",
        ])
        cleared = self._structured([
            "your own replies have been noncommittal",
            "summer schedules churn for every family",
        ])
        result, calls = self._turn([all_in, cleared])
        self.assertEqual(calls, 2)
        self.assertNotIn("hold them extra loosely", result["response"])
        events = [json.loads(l) for l in (self.vault / "logs" / "iip-challenges.jsonl").read_text().splitlines()]
        challenge = events[-1]["challenge"]
        self.assertEqual(challenge["outcome"], "cleared")
        self.assertTrue(challenge["priors_matched"])

    def test_uncleared_challenge_marks_the_reply(self):
        all_in = self._structured([
            "your reaction feeds what Dana is engineering",
            "Dana rearranges things; that's the baseline with Dana",
        ])
        result, calls = self._turn([all_in, all_in, all_in])
        self.assertEqual(calls, 2)  # shares the N=1 budget
        self.assertIn("hold them extra loosely", result["response"])
        events = [json.loads(l) for l in (self.vault / "logs" / "iip-challenges.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["challenge"]["outcome"], "still_in_register")


class SummarizerTests(unittest.TestCase):
    def test_summarizer_reads_the_log(self):
        from lisan.tools.interpretation import log_iip_event, summarize_iip_log

        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        ensure_repo_layout(root)
        vault = vault_root(root)
        log_iip_event(vault, {"detector": "interpretation", "validated": "pass", "regenerations": 0})
        log_iip_event(vault, {
            "detector": "interpretation", "validated": "pass", "regenerations": 1,
            "challenge": {"priors_matched": ["pattern.x"], "outcome": "cleared"},
        })
        out = summarize_iip_log(vault, weeks=1)
        self.assertIn("fires=2", out)
        self.assertIn("challenges=1", out)
        self.assertIn("challenge_cleared=1", out)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
