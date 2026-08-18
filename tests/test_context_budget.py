"""Gates on the context budget and the dreamer's chunked passes.

The defect these pin (2026-07 → 2026-08-18): the dreamer's compress bundle
loaded every entity in full with no cutoff and no cap. Entity biographies grow
by design, so the bundle crossed codex's 1,048,576-char input ceiling in late
July and every dreamer run for the next seventeen days died with
`input_too_large` — while `dreamer.maintenance` recorded success and
`lisan self state` reported a healthy dreamer, because a ProviderError fell
through to `fallback_output`.

Two things had to become true. A bundle must fit the window no matter how
large the vault grows, *without* dropping records — partition, not truncate.
And a maintenance organ that cannot do its work must fail loudly.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.config import DEFAULT_CONFIG
from lisan.tools.context_budget import (
    DEFAULT_PROVIDER_INPUT_CHARS,
    Record,
    Section,
    budget_from_config,
    plan_chunks,
    render,
)


def _section(title: str, n: int, size: int, prefix: str = "r") -> Section:
    return Section(
        title,
        tuple(Record(f"### {prefix}{i}.md", "x" * size) for i in range(n)),
    )


class BudgetResolutionTests(unittest.TestCase):
    def test_defaults_come_from_the_provider_ceiling(self) -> None:
        budget = budget_from_config(DEFAULT_CONFIG)
        self.assertLess(budget["budget_chars"], DEFAULT_PROVIDER_INPUT_CHARS)
        self.assertGreater(budget["budget_chars"], 0)

    def test_missing_block_still_yields_a_usable_budget(self) -> None:
        self.assertGreater(budget_from_config({})["budget_chars"], 0)
        self.assertGreater(budget_from_config(None)["budget_chars"], 0)

    def test_garbage_values_fall_back_rather_than_crash(self) -> None:
        budget = budget_from_config({"context": {"provider_input_chars": "banana", "max_chunks": 0}})
        self.assertGreater(budget["budget_chars"], 0)
        self.assertGreater(budget["max_chunks"], 0)

    def test_reserve_larger_than_the_ceiling_does_not_zero_the_budget(self) -> None:
        budget = budget_from_config({"context": {"provider_input_chars": 1000, "reserve_chars": 9999}})
        self.assertGreater(budget["budget_chars"], 0)


class PackingTests(unittest.TestCase):
    def test_a_bundle_under_budget_is_one_chunk_and_unchanged(self) -> None:
        sections = [_section("## Entities", 3, 100)]
        plan = plan_chunks(sections, budget_chars=100_000)
        self.assertEqual(plan.chunk_count, 1)
        self.assertEqual(plan.chunks[0], render(sections))

    def test_an_oversized_bundle_splits_and_every_chunk_fits(self) -> None:
        sections = [_section("## Entities", 40, 5_000)]
        plan = plan_chunks(sections, budget_chars=50_000)
        self.assertGreater(plan.chunk_count, 1)
        for chunk in plan.chunks:
            self.assertLessEqual(len(chunk), 50_000)

    def test_partition_loses_no_record(self) -> None:
        """The whole point. Truncating to fit would have been easy and would
        have produced a dreamer reasoning from a vault it could not see."""
        sections = [_section("## Entities", 40, 5_000), _section("## State Files", 10, 3_000, "s")]
        plan = plan_chunks(sections, budget_chars=50_000)
        joined = "\n".join(plan.chunks)
        for section in sections:
            for record in section.records:
                self.assertEqual(joined.count(record.label + "\n"), 1, f"{record.label} appears once")
        self.assertEqual(plan.truncated, [])
        self.assertEqual(plan.dropped, [])

    def test_each_chunk_repeats_the_section_title_it_carries(self) -> None:
        """A chunk has to be readable alone — a bare run of records with no
        heading tells the model nothing about what it is looking at."""
        plan = plan_chunks([_section("## Entities", 40, 5_000)], budget_chars=50_000)
        for chunk in plan.chunks:
            self.assertTrue(chunk.lstrip().startswith("## Entities"), chunk[:60])

    def test_record_order_is_preserved_across_chunks(self) -> None:
        sections = [_section("## Entities", 30, 5_000)]
        plan = plan_chunks(sections, budget_chars=40_000)
        joined = "\n".join(plan.chunks)
        positions = [joined.index(r.label) for r in sections[0].records]
        self.assertEqual(positions, sorted(positions))

    def test_packing_is_deterministic(self) -> None:
        sections = [_section("## Entities", 40, 5_000)]
        self.assertEqual(
            plan_chunks(sections, budget_chars=50_000).chunks,
            plan_chunks(sections, budget_chars=50_000).chunks,
        )

    def test_a_record_too_large_for_any_chunk_is_truncated_and_announced(self) -> None:
        """The backstop. Truncation is allowed only when a single record
        cannot fit alone — and it says so in the text the model reads."""
        sections = [Section("## Entities", (Record("### huge.md", "x" * 90_000),))]
        plan = plan_chunks(sections, budget_chars=20_000, per_record_chars=10_000)
        self.assertEqual(len(plan.truncated), 1)
        label, kept, original = plan.truncated[0]
        self.assertEqual(label, "### huge.md")
        self.assertLess(kept, original)
        self.assertIn("truncated by Lisan", plan.chunks[0])
        self.assertIn("90,0", plan.chunks[0])
        self.assertIn("truncated to", " ".join(plan.notes()))

    def test_exceeding_max_chunks_names_what_was_dropped(self) -> None:
        """Bounded, but never silent — a silent drop is the failure mode this
        whole module exists to prevent."""
        sections = [_section("## Entities", 60, 5_000)]
        plan = plan_chunks(sections, budget_chars=20_000, max_chunks=3)
        self.assertLessEqual(plan.chunk_count, 3)
        self.assertTrue(plan.dropped)
        self.assertIn("dropped", " ".join(plan.notes()))

    def test_empty_sections_are_a_valid_single_chunk(self) -> None:
        plan = plan_chunks([Section("## Confidence Candidates", (), "- None")], budget_chars=1_000)
        self.assertEqual(plan.chunk_count, 1)
        self.assertIn("- None", plan.chunks[0])

    def test_no_chunk_ever_exceeds_the_budget_across_many_shapes(self) -> None:
        """Property gate: the budget is a hard bound, not an estimate. It was
        briefly off by one char per record because join() inserts separators
        the accounting did not count."""
        for count, size, budget in [
            (5, 1_000, 3_000), (50, 800, 5_000), (200, 300, 2_000),
            (7, 20_000, 25_000), (120, 1_500, 10_000),
        ]:
            plan = plan_chunks([_section("## Entities", count, size)], budget_chars=budget, max_chunks=999)
            for chunk in plan.chunks:
                self.assertLessEqual(len(chunk), budget, f"count={count} size={size} budget={budget}")


class DreamerBundleTests(unittest.TestCase):
    """Every dreamer task must be packable, whatever the vault holds."""

    def test_every_task_bundle_fits_after_packing(self) -> None:
        from lisan.paths import ensure_repo_layout, vault_root
        from lisan.tools import dreamer_ops

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            vault = vault_root(root)
            entities = vault / "entities" / "people"
            entities.mkdir(parents=True, exist_ok=True)
            for i in range(30):
                (entities / f"p{i}.md").write_text(
                    '---\n{"id": "entity.p%d", "type": "entity"}\n---\n\n' % i + "y" * 6_000,
                    encoding="utf-8",
                )
            budget = {"budget_chars": 40_000, "per_record_chars": 30_000, "max_chunks": 99}
            for task in ("compress", "primer", "epoch", "identity_anchor", "contradict", "hindsight"):
                sections = dreamer_ops._sections_for_task(vault, task)
                plan = plan_chunks(sections, **budget)
                self.assertEqual(plan.dropped, [], task)
                for chunk in plan.chunks:
                    self.assertLessEqual(len(chunk), budget["budget_chars"], task)


class DreamerMergeTests(unittest.TestCase):
    def test_lists_concatenate_and_dedupe_across_parts(self) -> None:
        from lisan.tools.dreamer_ops import _merge_dreamer_outputs

        merged = _merge_dreamer_outputs(
            [
                {"task": "compress", "summary": "a", "findings": [{"type": "x", "message": "m"}],
                 "recommendations": ["r1"]},
                {"task": "compress", "summary": "b", "findings": [{"type": "x", "message": "m"},
                                                                  {"type": "y", "message": "n"}],
                 "recommendations": ["r1", "r2"]},
            ],
            task="compress",
        )
        self.assertEqual(len(merged["findings"]), 2)
        self.assertEqual(merged["recommendations"], ["r1", "r2"])
        self.assertIn("part 1/2", merged["summary"])
        self.assertIn("part 2/2", merged["summary"])
        self.assertEqual(merged["chunked_parts"], 2)

    def test_approval_requires_every_part(self) -> None:
        """One part withholding approval is a reason not to approve."""
        from lisan.tools.dreamer_ops import _merge_dreamer_outputs

        self.assertFalse(
            _merge_dreamer_outputs(
                [{"approved": True}, {"approved": False}], task="compress"
            )["approved"]
        )
        self.assertTrue(
            _merge_dreamer_outputs(
                [{"approved": True}, {"approved": True}], task="compress"
            )["approved"]
        )


class DreamerRunTests(unittest.TestCase):
    def setUp(self) -> None:
        from lisan.paths import ensure_repo_layout, vault_root

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        entities = self.vault / "entities" / "people"
        entities.mkdir(parents=True, exist_ok=True)
        for i in range(20):
            (entities / f"p{i}.md").write_text(
                '---\n{"id": "entity.p%d", "type": "entity"}\n---\n\n' % i + "y" * 5_000,
                encoding="utf-8",
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _config(self, budget: int) -> dict:
        return {"context": {"provider_input_chars": budget, "reserve_chars": budget // 8,
                            "per_record_chars": budget, "max_chunks": 99}}

    def test_a_large_vault_runs_as_several_passes_and_reports_them(self) -> None:
        from lisan.tools.dreamer_ops import run_dreamer_task

        calls: list[str] = []

        def fake_run_json(text, **kwargs):
            calls.append(text)
            return {"task": "compress", "summary": "s", "findings": [], "recommendations": []}

        with patch("lisan.agents.DreamerAgent.run_json", side_effect=fake_run_json):
            out = run_dreamer_task(vault=self.vault, task="compress", config=self._config(40_000))

        self.assertGreater(len(calls), 1, "an oversized vault must run as several passes")
        for n, text in enumerate(calls, start=1):
            self.assertIn(f"part {n} of {len(calls)}", text)
        body = out.read_text(encoding="utf-8")
        self.assertIn("## Passes", body)
        self.assertIn(f"{len(calls)} passes", body)

    def test_a_small_vault_still_runs_as_one_pass(self) -> None:
        from lisan.tools.dreamer_ops import run_dreamer_task

        calls: list[str] = []

        def fake_run_json(text, **kwargs):
            calls.append(text)
            return {"task": "compress", "summary": "s", "findings": []}

        with patch("lisan.agents.DreamerAgent.run_json", side_effect=fake_run_json):
            out = run_dreamer_task(vault=self.vault, task="compress", config=self._config(4_000_000))

        self.assertEqual(len(calls), 1)
        self.assertNotIn("part 1 of", calls[0])
        self.assertNotIn("## Passes", out.read_text(encoding="utf-8"))

    def test_a_provider_failure_is_raised_not_swallowed(self) -> None:
        """The seventeen silent days. `dreamer.maintenance` recorded success
        on every one of them because the agent fell back instead of raising."""
        from lisan.providers.base import ProviderError
        from lisan.tools.dreamer_ops import run_dreamer_task

        with patch("lisan.agents.DreamerAgent.run_json", side_effect=ProviderError("input_too_large")):
            with self.assertRaises(ProviderError):
                run_dreamer_task(vault=self.vault, task="compress", config=self._config(4_000_000))

    def test_run_json_is_asked_to_raise(self) -> None:
        from lisan.tools.dreamer_ops import run_dreamer_task

        seen: dict = {}

        def fake_run_json(text, **kwargs):
            seen.update(kwargs)
            return {"task": "compress", "summary": "s"}

        with patch("lisan.agents.DreamerAgent.run_json", side_effect=fake_run_json):
            run_dreamer_task(vault=self.vault, task="compress", config=self._config(4_000_000))
        self.assertEqual(seen.get("provider_error_mode"), "raise")


if __name__ == "__main__":
    unittest.main()
