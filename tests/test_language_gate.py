"""The hypothesis language gate: owner-configurable, machine-side only.

Two contracts, owner-decreed 2026-07-15:
1. User text is stored VERBATIM everywhere, whatever vocabulary it uses —
   the gate never touches notes, claims, check-ins, or frameworks. A
   deployment for a clinical professional depends on this.
2. The gate on machine-authored psychology (analyst patterns, prediction
   expectations, pattern validation) reads config
   `psyche.banned_hypothesis_terms`: null = defaults, [] = disabled
   (clinician mode), list = custom. One source of truth; refusals are
   loud, never silent.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.epistemic import (
    DEFAULT_HYPOTHESIS_GATE_TERMS,
    hypothesis_gate_terms,
    pattern_contains_diagnostic_language,
)

CLINICAL = "presents as a grandiose narcissist; narcissistic supply-seeking with borderline features"


class GateConfigTests(unittest.TestCase):
    def test_null_uses_defaults(self):
        self.assertEqual(hypothesis_gate_terms({"psyche": {"banned_hypothesis_terms": None}}),
                         DEFAULT_HYPOTHESIS_GATE_TERMS)
        self.assertEqual(hypothesis_gate_terms({}), DEFAULT_HYPOTHESIS_GATE_TERMS)

    def test_empty_list_disables_the_gate(self):
        cfg = {"psyche": {"banned_hypothesis_terms": []}}
        self.assertEqual(hypothesis_gate_terms(cfg), frozenset())
        self.assertFalse(pattern_contains_diagnostic_language(CLINICAL, cfg))

    def test_custom_list_replaces_defaults(self):
        cfg = {"psyche": {"banned_hypothesis_terms": ["sociopath"]}}
        self.assertTrue(pattern_contains_diagnostic_language("a textbook sociopath", cfg))
        self.assertFalse(pattern_contains_diagnostic_language("narcissistic behavior", cfg))


class VerbatimStorageTests(unittest.TestCase):
    """Contract 1: whatever the user says is added, unaltered. Nothing in
    the system rewrites, filters, or refuses user-authored content on
    vocabulary grounds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_claims_store_clinical_vocabulary_verbatim(self):
        from lisan.tools.record_factory import new_claim

        created = new_claim(self.vault, f"Ruth {CLINICAL}", owner="user",
                            claim_class="interpretation", confidence=0.6)
        self.assertEqual(load_markdown(created.path).frontmatter["claim_text"], f"Ruth {CLINICAL}")

    def test_checkins_store_clinical_vocabulary_verbatim(self):
        from lisan.tools.checkin import record_checkin
        from lisan.tools.record_factory import new_entity

        new_entity(self.vault, "Ruth Feld", subtype="person", summary="A person.")
        out = record_checkin(self.vault, "Ruth Feld", CLINICAL)
        fm = load_markdown(Path(out["path"])).frontmatter
        self.assertIn(CLINICAL, fm["observed_facts"])

    def test_ratified_frameworks_store_clinical_vocabulary_verbatim(self):
        from lisan.tools.decode import ratify_framework

        out = ratify_framework(self.vault, "Practice framework", CLINICAL)
        self.assertTrue(out["ok"])
        self.assertEqual(load_markdown(Path(out["path"])).frontmatter["summary"], CLINICAL)


class MachineSideGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _pattern_source(self):
        from lisan.tools.record_factory import new_evidence, new_pattern

        seed = new_evidence(
            self.vault, title="Seed observation", source_type="manual_note",
            actors=["Ruth"], observed_facts=["seed"], summary="seed",
        )
        seed_id = str(load_markdown(seed.path).frontmatter["id"])
        created = new_pattern(
            self.vault, pattern_type="relational_loop", hypothesis="A source",
            supporting_records=[seed_id],
            alternative_explanations=["x"], evidence_needed=["y"],
        )
        return str(load_markdown(created.path).frontmatter["id"])

    def test_prediction_refusal_is_loud_and_names_the_switch(self):
        from lisan.tools.predictions import record_prediction

        source = self._pattern_source()
        out = record_prediction(
            self.vault, "her narcissistic supply will dry up by fall",
            source=source, review_after="2099-01-01",
        )
        self.assertFalse(out["ok"])
        self.assertIn("language gate", out["error"])
        self.assertIn("banned_hypothesis_terms", out["error"])

    def test_prediction_passes_in_clinician_mode(self):
        from lisan.tools.predictions import record_prediction

        source = self._pattern_source()
        with patch("lisan.config.load_config",
                   return_value={"psyche": {"banned_hypothesis_terms": []}}):
            out = record_prediction(
                self.vault, "her narcissistic supply will dry up by fall",
                source=source, review_after="2099-01-01",
            )
        self.assertTrue(out["ok"], out)

    def test_validator_honors_clinician_mode(self):
        from lisan.tools.validator import validate_vault

        self._pattern_source()
        patterns = sorted((self.vault / "patterns").glob("*.md"))
        from lisan.frontmatter import write_markdown

        doc = load_markdown(patterns[0])
        fm = dict(doc.frontmatter)
        fm["hypothesis"] = "Ruth is narcissistic when stressed"
        write_markdown(patterns[0], fm, doc.body)
        report_default = validate_vault(self.vault)
        self.assertFalse(report_default.ok)
        with patch("lisan.config.load_config",
                   return_value={"psyche": {"banned_hypothesis_terms": []}}):
            report_open = validate_vault(self.vault)
        self.assertTrue(report_open.ok, report_open.summary())

    def test_analyst_refusal_is_logged_not_silent(self):
        from lisan.tools.analyst_ops import _materialize_pattern

        with self.assertLogs("lisan", level="WARNING") as captured:
            out = _materialize_pattern(
                self.vault, "bundle text",
                {"hypothesis": "Ruth is a narcissistic presence in the household",
                 "pattern_type": "relational_loop",
                 "supporting_records": ["a", "b"]},
                existing_patterns=[],
            )
        self.assertIsNone(out)
        self.assertTrue(any("language gate" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
