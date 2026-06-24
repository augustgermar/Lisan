from __future__ import annotations

import unittest

from lisan.tools.agent_namer import generate_agent_identity_from_seed


class AgentNamerTests(unittest.TestCase):
    def test_generate_agent_identity_from_seed_is_reproducible(self) -> None:
        identity = generate_agent_identity_from_seed("seed-value")
        again = generate_agent_identity_from_seed("seed-value")

        self.assertEqual(identity, again)
        self.assertEqual(len(identity.sha256), 64)
        self.assertTrue(all(ch in "0123456789abcdef" for ch in identity.sha256))
        self.assertGreaterEqual(len(identity.name), 4)
        self.assertTrue(identity.name[0].isupper())
        self.assertFalse(identity.name[-1].isdigit())


if __name__ == "__main__":
    unittest.main()
