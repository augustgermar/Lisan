from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.tools.deixis import render_for_display
from lisan.tools.eval_seed import seed_eval_primer
from lisan.tools.primer_index import principal_display_name, roster_kind


class EvalSeedPrimerTests(unittest.TestCase):
    def test_seed_eval_primer_writes_identity_core_and_roster(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            seed_eval_primer(
                vault,
                principal_name="Marcus Delgado",
                background="Senior network admin.",
                values="Keep production stable.",
                relationships="Lucia runs compliance.",
                principal_aliases=["Marcus"],
                roster_entries=[
                    {"name": "Lucia", "kind": "person"},
                    {"name": "Renata", "kind": "person"},
                    {"name": "Greg", "kind": "person"},
                    {"name": "Devin", "kind": "person"},
                    {"name": "Halverson Networks", "kind": "organization"},
                    {"name": "Project Northgate", "kind": "project"},
                    {"name": "Bastion", "kind": "system"},
                    {"name": "Aurora", "kind": "system"},
                ],
            )

            identity = vault / "primer" / "identity.md"
            core = vault / "primer" / "identity-core.md"

            self.assertTrue(identity.exists())
            self.assertTrue(core.exists())
            self.assertEqual(principal_display_name(vault), "Marcus")
            self.assertEqual(render_for_display("{{principal}} approved Bastion.", vault), "Marcus approved Bastion.")
            self.assertEqual(roster_kind(vault, "Halverson Networks"), "organization")
            self.assertEqual(roster_kind(vault, "Bastion"), "system")
            self.assertIn('name: "Marcus Delgado"', core.read_text(encoding="utf-8"))
            self.assertIn('  aliases: ["Marcus"]', core.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
