from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class InterlocutorCapabilityDocsTests(unittest.TestCase):
    """The interlocutor prompt documents Lisan's own ingestion CLI so the
    agent describes its abilities accurately. If the CLI changes, this
    cross-check fails before the prompt starts telling the user lies."""

    @classmethod
    def setUpClass(cls):
        cls.prompt = (REPO / "prompts" / "interlocutor_v1.md").read_text(encoding="utf-8")
        cls.cli_source = (REPO / "lisan" / "cli.py").read_text(encoding="utf-8")

    def test_documented_ingest_flags_exist_in_cli(self):
        for flag in ("--reference", "--link-entity", "--plan", "--on-exists"):
            self.assertIn(flag, self.prompt, f"prompt no longer documents {flag}")
            self.assertIn(f'"{flag}"', self.cli_source, f"prompt documents {flag} but the CLI lacks it")

    def test_documented_ingest_subcommands_exist_in_cli(self):
        for sub in ("scan", "run", "status", "audit"):
            self.assertIn(f"lisan ingest {sub}", self.prompt.replace("`", ""), f"prompt no longer documents ingest {sub}")
            self.assertIn(f'"{sub}"', self.cli_source, f"prompt documents ingest {sub} but the CLI lacks it")

    def test_prompt_keeps_the_no_claimed_actions_rule(self):
        self.assertRegex(self.prompt, re.compile(r"NEVER claim you performed an action", re.IGNORECASE))

    def test_prompt_admits_obsidian_ingestion_is_unbuilt(self):
        lowered = self.prompt.lower()
        self.assertIn("obsidian", lowered)
        self.assertIn("not built", lowered)


if __name__ == "__main__":
    unittest.main()
