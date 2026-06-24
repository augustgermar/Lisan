from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.tools.agent_namer import AgentIdentity
from lisan.tools.onboarding import _write_identity_core, run_onboarding


class OnboardingTests(unittest.TestCase):
    def test_fresh_onboarding_writes_third_person_primer_and_self_entity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir(parents=True, exist_ok=True)

            fixed_identity = AgentIdentity(
                seed="seed-value",
                sha256="a" * 64,
                konstel_hash="KONSTEL",
                name="Dabiku",
            )
            stdout = io.StringIO()
            inputs = iter(["1", "August Germar", "I work in finance and play in a band."])
            with (
                patch("lisan.tools.onboarding.generate_agent_identity", return_value=fixed_identity),
                patch("lisan.tools.chat.startup_check", return_value=True),
                patch("builtins.input", side_effect=lambda *_: next(inputs)),
                contextlib.redirect_stdout(stdout),
            ):
                completed = run_onboarding(vault)

            self.assertTrue(completed)
            identity = vault / "primer" / "identity.md"
            core = vault / "primer" / "identity-core.md"
            operating = vault / "primer" / "operating-style.md"
            high_stakes = vault / "primer" / "high-stakes.yaml"
            self_entity = vault / "entities" / "agents" / "dabiku.md"

            self.assertTrue(identity.exists())
            self.assertTrue(core.exists())
            self.assertTrue(operating.exists())
            self.assertTrue(high_stakes.exists())
            self.assertTrue(self_entity.exists())

            identity_text = identity.read_text(encoding="utf-8")
            core_text = core.read_text(encoding="utf-8")
            self_text = self_entity.read_text(encoding="utf-8")

            self.assertIn("# About the Principal", identity_text)
            self.assertNotIn("You are", identity_text)
            self.assertIn("The principal is August Germar.", identity_text)
            self.assertIn("I / me / Dabiku", core_text)
            self.assertIn('hash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"', core_text)
            self.assertIn('seed: "seed-value"', core_text)
            self.assertIn("kind", self_text)
            self.assertIn("Dabiku is a freshly initialized Lisan instance", self_text)

    def test_existing_core_skips_regeneration_and_repairs_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir(parents=True, exist_ok=True)
            identity = AgentIdentity(
                seed="seed-value",
                sha256="b" * 64,
                konstel_hash="KONSTEL",
                name="Dabiku",
            )
            _write_identity_core(vault / "primer" / "identity-core.md", "August Germar", agent_identity=identity)

            stdout = io.StringIO()
            with (
                patch("lisan.tools.onboarding.generate_agent_identity") as mock_generate,
                patch("builtins.input") as mock_input,
                contextlib.redirect_stdout(stdout),
            ):
                completed = run_onboarding(vault)

            self.assertTrue(completed)
            mock_generate.assert_not_called()
            mock_input.assert_not_called()
            self.assertTrue((vault / "primer" / "identity.md").exists())
            self.assertTrue((vault / "primer" / "operating-style.md").exists())
            self.assertTrue((vault / "primer" / "high-stakes.yaml").exists())
            self.assertTrue((vault / "entities" / "agents" / "dabiku.md").exists())


if __name__ == "__main__":
    unittest.main()
