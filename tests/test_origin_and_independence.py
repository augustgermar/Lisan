"""Origin class, and the difference between two records and two sources.

Two mechanisms, two questions, deliberately not conflated:

**Trust** — how much authority a memory carries. Bound at the write boundary
from the authenticated channel, never inferred from content, and inherited as
the *least* trusted of a derived record's sources. The malleability result says
content-derived and lineage-derived trust both fail, because an adversary can
transform either without forging a credential.

**Independence** — whether two memories are really two. Measured on a production
vault 2026-07-30: 339 of 340 knowledge records carry a `source_document`, and
one document had produced **38 chunks**. Against `len(support) >= 2`, that single
document could found a pattern about a person nineteen times over, by itself,
and the citation list would look thorough. One of those groups was an 8-chunk
document about family members.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lisan.frontmatter import write_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.analyst_ops import independent_support_count
from lisan.tools.origin import (
    DEFAULT_ORIGIN,
    ORIGIN_AGENT,
    ORIGIN_EXTERNAL,
    ORIGIN_OWNER,
    ORIGIN_TOOL,
    effective_origin,
    independent_lineages,
    least_trusted,
    lineage_key,
    may_authorize,
    origin_for_speaker,
    record_origin,
    trust,
)


def _record(record_id: str, **extra) -> dict:
    base = {
        "id": record_id, "type": "knowledge", "created": "2026-07-30",
        "updated": "2026-07-30", "status": "active", "significance": "low",
        "domain_primary": "cross_arena", "domain_secondary": [], "privacy": "personal",
        "summary": "s", "links": [], "confidence": "low", "confidence_basis": "b",
        "last_confirmed": "2026-07-30", "review_after": "2026-07-30",
    }
    base.update(extra)
    return base


class TrustLatticeTests(unittest.TestCase):
    def test_trust_is_ordered_owner_agent_tool_external(self):
        self.assertGreater(trust(ORIGIN_OWNER), trust(ORIGIN_AGENT))
        self.assertGreater(trust(ORIGIN_AGENT), trust(ORIGIN_TOOL))
        self.assertGreater(trust(ORIGIN_TOOL), trust(ORIGIN_EXTERNAL))

    def test_unknown_origin_is_least_trusted_not_most(self):
        """Fails closed. A memory whose provenance cannot be established is
        exactly the one that must not authorize anything."""
        self.assertEqual(trust("something-invented"), trust(ORIGIN_EXTERNAL))
        self.assertEqual(trust(None), trust(ORIGIN_EXTERNAL))
        self.assertEqual(DEFAULT_ORIGIN, ORIGIN_EXTERNAL)

    def test_derived_records_inherit_the_least_trusted_source(self):
        """The non-malleability property: summarizing untrusted text cannot
        launder it into agent-authored knowledge, because the summary's origin
        is computed rather than written."""
        self.assertEqual(least_trusted([ORIGIN_OWNER, ORIGIN_EXTERNAL]), ORIGIN_EXTERNAL)
        self.assertEqual(least_trusted([ORIGIN_OWNER, ORIGIN_AGENT]), ORIGIN_AGENT)
        self.assertEqual(least_trusted([ORIGIN_OWNER]), ORIGIN_OWNER)

    def test_deriving_from_nothing_traceable_is_not_trustworthy(self):
        self.assertEqual(least_trusted([]), ORIGIN_EXTERNAL)
        self.assertEqual(least_trusted([None, "", "nonsense"]), ORIGIN_EXTERNAL)

    def test_only_owner_and_agent_memory_may_authorize(self):
        self.assertTrue(may_authorize(ORIGIN_OWNER))
        self.assertTrue(may_authorize(ORIGIN_AGENT))
        self.assertFalse(may_authorize(ORIGIN_TOOL))
        self.assertFalse(may_authorize(ORIGIN_EXTERNAL))

    def test_speaker_maps_to_origin_and_unknown_speakers_are_external(self):
        self.assertEqual(origin_for_speaker("USER"), ORIGIN_OWNER)
        self.assertEqual(origin_for_speaker("lisan"), ORIGIN_AGENT)
        self.assertEqual(origin_for_speaker("ADJUTANT"), ORIGIN_AGENT)
        # The system reporting on itself is agent-authored, however much the
        # text may read like an instruction.
        self.assertEqual(origin_for_speaker("SYSTEM"), ORIGIN_AGENT)
        self.assertEqual(origin_for_speaker("some-third-party"), ORIGIN_EXTERNAL)

    def test_unlabelled_records_are_distinguishable_from_external_ones(self):
        """Every record written before this field existed is unlabelled, which
        is not the same fact as being external — a corpus report wants the
        difference, an authority decision does not."""
        self.assertEqual(record_origin(_record("k.1")), "")
        self.assertEqual(effective_origin(_record("k.1")), ORIGIN_EXTERNAL)
        self.assertEqual(record_origin(_record("k.2", origin="owner")), ORIGIN_OWNER)


class LineageTests(unittest.TestCase):
    def test_chunks_of_one_document_share_a_lineage(self):
        chunks = [_record(f"knowledge.doc-chunk-{n}", source_document="acgskills") for n in range(38)]
        self.assertEqual(len(independent_lineages(chunks)), 1,
                         "38 chunks of one document are one source")

    def test_different_documents_are_independent(self):
        a = _record("knowledge.a", source_document="doc-one")
        b = _record("knowledge.b", source_document="doc-two")
        self.assertEqual(len(independent_lineages([a, b])), 2)

    def test_the_most_specific_available_source_wins(self):
        """artifact_ref beats source_document beats batch_id: two chunks of one
        document that came from two different artifacts are two sources."""
        a = _record("k.a", artifact_ref="artifact.one", source_document="shared", batch_id="b1")
        b = _record("k.b", artifact_ref="artifact.two", source_document="shared", batch_id="b1")
        self.assertEqual(len(independent_lineages([a, b])), 2)

    def test_a_record_with_no_traceable_source_is_its_own_lineage(self):
        a, b = _record("k.a"), _record("k.b")
        self.assertEqual(len(independent_lineages([a, b])), 2)
        self.assertTrue(lineage_key(a).startswith("id:"))

    def test_an_item_cannot_corroborate_itself(self):
        one = _record("knowledge.same", source_document="doc")
        self.assertEqual(len(independent_lineages([one, one, one])), 1)


class EvidenceGateTests(unittest.TestCase):
    """The gate as the analyst actually calls it, against records on disk."""

    def _vault(self, tmp: str) -> Path:
        vault = Path(tmp) / "vault"
        ensure_vault_layout(vault)
        return vault

    def test_two_chunks_of_one_document_do_not_clear_the_bar(self):
        with TemporaryDirectory() as tmp:
            vault = self._vault(tmp)
            ids = []
            for n in range(5):
                rid = f"knowledge.report-chunk-{n}"
                write_markdown(vault / "knowledge" / f"chunk{n}.md",
                               _record(rid, source_document="one-report"), "# c\n")
                ids.append(rid)
            self.assertEqual(independent_support_count(vault, ids), 1)

    def test_two_documents_do_clear_the_bar(self):
        with TemporaryDirectory() as tmp:
            vault = self._vault(tmp)
            write_markdown(vault / "knowledge" / "a.md",
                           _record("knowledge.a", source_document="doc-one"), "# a\n")
            write_markdown(vault / "knowledge" / "b.md",
                           _record("knowledge.b", source_document="doc-two"), "# b\n")
            self.assertEqual(
                independent_support_count(vault, ["knowledge.a", "knowledge.b"]), 2)

    def test_an_unresolvable_reference_counts_as_its_own_lineage(self):
        """It must not silently *help* a hypothesis clear the bar by being
        grouped into an existing source, and it must not be treated as
        corroboration it cannot provide. Counting it alone is the honest
        middle: the analyst is told, and the number does not lie either way."""
        with TemporaryDirectory() as tmp:
            vault = self._vault(tmp)
            write_markdown(vault / "knowledge" / "a.md",
                           _record("knowledge.a", source_document="doc-one"), "# a\n")
            self.assertEqual(
                independent_support_count(vault, ["knowledge.a", "knowledge.ghost"]), 2)

    def test_empty_support_is_zero_not_an_error(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(independent_support_count(self._vault(tmp), []), 0)


if __name__ == "__main__":
    unittest.main()


class GateRefusalTests(unittest.TestCase):
    """The gate, not just the counter.

    The first version of this suite passed with `independent = len(support)`
    reintroduced, because every test called the counting helper directly and
    none exercised the branch that uses it. A test that cannot fail against the
    bug it was written for is decoration.
    """

    def _vault_with_chunks(self, tmp: str, *, source_document: str, count: int) -> tuple[Path, list[str]]:
        vault = Path(tmp) / "vault"
        ensure_vault_layout(vault)
        ids = []
        for n in range(count):
            rid = f"knowledge.{source_document}-chunk-{n}"
            write_markdown(vault / "knowledge" / f"{source_document}-{n}.md",
                           _record(rid, source_document=source_document), "# c\n")
            ids.append(rid)
        return vault, ids

    def test_a_pattern_supported_only_by_one_document_is_refused(self):
        from lisan.tools.analyst_ops import _materialize_pattern

        with TemporaryDirectory() as tmp:
            vault, ids = self._vault_with_chunks(tmp, source_document="one-report", count=6)
            created = _materialize_pattern(
                vault, "bundle",
                {"hypothesis": "The subject avoids difficult conversations on weekends",
                 "pattern_type": "avoidance_loop", "supporting_records": ids},
                [],
            )
        self.assertIsNone(created, "six chunks of one document must not found a pattern")

    def test_the_same_pattern_is_accepted_once_a_second_document_agrees(self):
        from lisan.tools.analyst_ops import _materialize_pattern

        with TemporaryDirectory() as tmp:
            vault, ids = self._vault_with_chunks(tmp, source_document="one-report", count=6)
            write_markdown(vault / "knowledge" / "other.md",
                           _record("knowledge.other", source_document="a-second-report"), "# o\n")
            created = _materialize_pattern(
                vault, "bundle",
                {"hypothesis": "The subject avoids difficult conversations on weekends",
                 "pattern_type": "avoidance_loop",
                 "supporting_records": ids + ["knowledge.other"]},
                [],
            )
        self.assertIsNotNone(created, "two independent sources clear the bar")
