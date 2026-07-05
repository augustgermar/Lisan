"""The reply-query retrieval pass: the assistant's previous reply runs as
its own retrieval lanes, so a record only the assistant's active thread
mentions can surface when the user references it without naming it."""
from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from lisan.config import load_config
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.rebuild_index import rebuild_index
from lisan.tools.record_factory import new_knowledge
from lisan.tools.retrieval import retrieve_context
from lisan.tools.transcripts import append_transcript


class ReplyQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"

        new_knowledge(
            self.vault,
            "Maple syrup tasting",
            summary="The quarterly maple syrup tasting schedule at the Homestead.",
            body="The quarterly maple syrup tasting happens at the Homestead in autumn.",
        )
        new_knowledge(
            self.vault,
            "Greenhouse irrigation valve",
            summary="The greenhouse irrigation valve replacement project and its parts order.",
            body="The greenhouse irrigation valve replacement project needs brass fittings from Larkspur.",
        )
        rebuild_index(vault=self.vault, db_path=self.db_path, embeddings_file=self.root / "embeddings.bin")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _config(self, **fusion_overrides):
        config = deepcopy(load_config())
        config.setdefault("retrieval", {}).setdefault("fusion", {})
        config["retrieval"]["fusion"].update({"enabled": True, "method": "rrf", **fusion_overrides})
        return config

    def _retrieve(self, query: str, conversation_id: str | None, **fusion_overrides):
        with patch("lisan.tools.retrieval.load_config", return_value=self._config(**fusion_overrides)):
            return retrieve_context(
                query,
                domain="environmental",
                vault=self.vault,
                db_path=self.db_path,
                conversation_id=conversation_id,
            )

    def test_reply_lane_surfaces_the_assistants_thread(self) -> None:
        append_transcript(
            vault=self.vault, conversation_id="c1", speaker="USER",
            text="Any progress on that thing you were checking?",
        )
        append_transcript(
            vault=self.vault, conversation_id="c1", speaker="LISAN",
            text="I checked on the greenhouse irrigation valve replacement project — the brass fittings arrived.",
        )
        result = self._retrieve("okay go ahead and schedule the next step for it", "c1")
        loaded_ids = {item.id for item in result.loaded}
        valve = next((i for i in result.loaded if "irrigation" in i.summary.lower()), None)
        self.assertIsNotNone(valve, f"reply-lane record missing; loaded={loaded_ids}")
        self.assertIn("_reply", valve.reason)

    def test_trivial_reply_is_skipped(self) -> None:
        append_transcript(vault=self.vault, conversation_id="c2", speaker="USER", text="thanks")
        append_transcript(vault=self.vault, conversation_id="c2", speaker="LISAN", text="Got it.")
        result = self._retrieve("okay schedule the next step", "c2")
        self.assertFalse(any("_reply" in item.reason for item in result.loaded))

    def test_no_conversation_means_no_reply_lanes(self) -> None:
        result = self._retrieve("greenhouse irrigation valve", None)
        self.assertTrue(result.loaded)  # direct match still works
        self.assertFalse(any("_reply" in item.reason for item in result.loaded))

    def test_reply_lanes_can_be_disabled(self) -> None:
        append_transcript(
            vault=self.vault, conversation_id="c3", speaker="LISAN",
            text="I checked on the greenhouse irrigation valve replacement project — fittings arrived.",
        )
        result = self._retrieve("okay do the next step", "c3", reply_query_enabled=False)
        self.assertFalse(any("_reply" in item.reason for item in result.loaded))


if __name__ == "__main__":
    unittest.main()
