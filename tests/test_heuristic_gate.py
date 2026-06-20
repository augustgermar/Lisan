from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.tools.heuristic_gate import score_text


class HeuristicGateThresholdTests(unittest.TestCase):
    """Tests for skip/lightweight/full action boundaries.

    Default thresholds: skip < 3, lightweight 3–5, full >= 6.
    The gate uses strict < so the boundary score itself steps up.
    """

    def _score(self, text: str) -> tuple[str, int]:
        result = score_text(text)
        return result.action, result.score

    # ── Hard overrides ────────────────────────────────────────────────────────

    def test_forget_flag_forces_skip(self) -> None:
        action, score = self._score("/forget all that")
        self.assertEqual(action, "skip")
        self.assertEqual(score, -100)

    def test_remember_flag_forces_full(self) -> None:
        action, _ = self._score("/remember this important thing")
        self.assertEqual(action, "full")

    def test_too_short_forces_skip(self) -> None:
        action, _ = self._score("Hi")
        self.assertEqual(action, "skip")

    # ── Below skip threshold ──────────────────────────────────────────────────

    def test_score_zero_is_skip(self) -> None:
        # Neutral greeting, no positive signals.
        action, score = self._score("Hello there, how are you doing today")
        self.assertEqual(action, "skip")
        self.assertLess(score, 3)

    def test_factual_lookup_is_skip(self) -> None:
        # Single question with no personal stake → -3 from factual lookup.
        action, score = self._score("What is the capital of France?")
        self.assertEqual(action, "skip")
        self.assertLess(score, 3)

    # ── Skip/lightweight boundary (score == 3) ────────────────────────────────

    def test_score_exactly_3_is_lightweight(self) -> None:
        # Decision phrase alone = +3. No other positive signals in a short,
        # impersonal sentence with no affect terms.
        text = "Going forward we will use the new process."
        result = score_text(text)
        self.assertEqual(result.score, 3)
        self.assertEqual(result.action, "lightweight")

    def test_score_2_is_skip(self) -> None:
        # Repeated proper noun (3+ times) = +2, below the skip threshold.
        text = "Jordan said Jordan would ask Jordan about it."
        result = score_text(text)
        self.assertLess(result.score, 3)
        self.assertEqual(result.action, "skip")

    # ── Inside lightweight band (3 <= score < 6) ─────────────────────────────

    def test_score_4_is_lightweight(self) -> None:
        # Vault-local/config-driven high-stakes term alone = +4.
        text = "Some important topic came up at the meeting."
        result = score_text(text, config={"heuristic": {"high_stakes_terms": ["important topic"]}})
        self.assertGreaterEqual(result.score, 3)
        self.assertLess(result.score, 6)
        self.assertEqual(result.action, "lightweight")

    def test_no_high_stakes_bonus_without_config_or_vault(self) -> None:
        text = "Some important topic came up at the meeting."
        result = score_text(text)
        self.assertEqual(result.score, 0)
        self.assertEqual(result.action, "skip")

    def test_vault_local_high_stakes_bonus_fires(self) -> None:
        text = "Some important topic came up at the meeting."
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir(parents=True, exist_ok=True)
            (vault / "primer" / "high-stakes.yaml").write_text(
                'terms: ["important topic"]\n',
                encoding="utf-8",
            )
            result = score_text(text, vault=vault)

        self.assertGreaterEqual(result.score, 3)
        self.assertLess(result.score, 6)
        self.assertEqual(result.action, "lightweight")

    def test_config_high_stakes_bonus_fires_without_vault(self) -> None:
        text = "Some important topic came up at the meeting."
        result = score_text(text, config={"heuristic": {"high_stakes_terms": ["important topic"]}})
        self.assertGreaterEqual(result.score, 3)
        self.assertLess(result.score, 6)
        self.assertEqual(result.action, "lightweight")

    def test_trimmed_affect_defaults_do_not_fire_on_broad_adjectives(self) -> None:
        result = score_text("The weather is cold and nice today.")
        self.assertEqual(result.score, 0)
        self.assertEqual(result.action, "skip")
        self.assertNotIn("affect term", result.reasons)

    def test_unambiguous_affect_terms_still_fire(self) -> None:
        result = score_text("I'm devastated and heartbroken about what happened yesterday.")
        self.assertGreaterEqual(result.score, 2)
        self.assertIn("affect term", result.reasons)

    def test_biographical_density_defaults_to_family_life_terms(self) -> None:
        text = "My mom and dad visited my hometown last weekend, and we talked about my birthday plans for the summer."
        result = score_text(text)
        self.assertIn("biographical content", result.reasons)

    def test_biographical_density_can_be_disabled_via_config(self) -> None:
        text = "My mom and dad visited my hometown last weekend, and we talked about my birthday plans for the summer."
        result = score_text(text, config={"heuristic": {"biographical_terms": []}})
        self.assertNotIn("biographical content", result.reasons)

    def test_score_5_is_lightweight(self) -> None:
        # Decision phrase (+3) + one affect term (+2) = 5.
        text = "Going forward I will handle this. I feel excited."
        result = score_text(text)
        self.assertGreaterEqual(result.score, 3)
        self.assertLess(result.score, 6)
        self.assertEqual(result.action, "lightweight")

    # ── Lightweight/full boundary (score == 6) ────────────────────────────────

    def test_score_exactly_6_is_full(self) -> None:
        # Decision phrase (+3) + open-loop phrase (+3) = 6.
        text = "Going forward I will handle this. I need to follow up on the details."
        result = score_text(text)
        self.assertEqual(result.score, 6)
        self.assertEqual(result.action, "full")

    def test_score_above_6_is_full(self) -> None:
        # High-stakes term (+4) + decision phrase (+3) = 7.
        text = "I decided this important topic needs an immediate fix."
        result = score_text(text, config={"heuristic": {"high_stakes_terms": ["important topic"]}})
        self.assertGreaterEqual(result.score, 6)
        self.assertEqual(result.action, "full")

    # ── Mode classification ───────────────────────────────────────────────────

    def test_short_personal_event_is_elicitor_mode(self) -> None:
        # Short first-person event scores >= 3 (open-loop phrase) so the mode
        # override for skip-action doesn't apply. Seed score beats narrative.
        result = score_text("I had an unusual day at work. I need to follow up with my boss.")
        self.assertNotEqual(result.action, "skip")
        self.assertEqual(result.mode, "elicitor")

    def test_long_narrative_is_extraction_mode(self) -> None:
        # 250+ words + temporal connectors → narrative_score 8; must also score
        # >= 3 so the skip-action mode override doesn't apply.
        base = "I went to the meeting and then after the meeting, before the call, "
        words = base * 30 + "and I decided to follow up."
        result = score_text(words.strip())
        self.assertNotEqual(result.action, "skip")
        self.assertEqual(result.mode, "extraction")

    # ── Negative signal interaction ───────────────────────────────────────────

    def test_code_block_reduces_score(self) -> None:
        # >80% code content → -3. With a decision phrase (+3) the net is 0 → skip.
        code = "Going forward:\n```\n" + "\n".join(["x = 1"] * 20) + "\n```"
        result = score_text(code)
        # The combined score should be lower than the decision phrase alone.
        decision_only = score_text("Going forward we use the new process.")
        self.assertLess(result.score, decision_only.score)


if __name__ == "__main__":
    unittest.main()
