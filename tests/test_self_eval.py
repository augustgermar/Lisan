"""The self-evaluation organ: real transcripts in, private report + honest
suggestion loops out. Invented cast only."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.self_eval import (
    SelfEvalJudgeUnavailable,
    _parse_transcript,
    recent_exchanges,
    run_self_evaluation,
)

_TODAY = date(2026, 7, 5)


def _transcript(vault: Path, day: str, pairs: list[tuple[str, str]]) -> None:
    lines = []
    for user, assistant in pairs:
        lines += [f"## Conversation — 10:00 [telegram-1-{day}]", "", f"USER: {user}",
                  f"## Conversation — 10:01 [telegram-1-{day}]", "", f"LISAN: {assistant}"]
    path = vault / "transcripts" / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fake_scores(score: int):
    def fake(rubric, user, assistant, *, provider=None, model=None, context=None, llm=None):
        return [
            {"id": "continuity", "score": score, "rationale": "test"},
            {"id": "non-confabulation", "score": min(5, score + 1), "rationale": "test"},
        ]
    return fake


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "lisan.sqlite"
        core = self.vault / "primer" / "identity-core.md"
        core.parent.mkdir(parents=True, exist_ok=True)
        core.write_text(
            '---\nprincipal:\n  name: "Vega Owner"\n  aliases: ["Vega"]\n'
            'assistant:\n  name: "Scout"\n---\n\n# Identity Core\n\n## Voice\n\n'
            "- It never uses exclamation points.\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()


class TranscriptParsingTests(_Env):
    def test_parses_real_format_and_pairs_exchanges(self):
        _transcript(self.vault, "2026-07-05", [
            ("Tell me about Ruth and the greenhouse project we discussed", "Ruth is helping with the rebuild."),
            ("ok", "Noted."),  # trivial ack — dropped
        ])
        ex = recent_exchanges(self.vault, days=3, now=_TODAY)
        self.assertEqual(len(ex), 1)
        self.assertIn("greenhouse", ex[0]["user"])
        self.assertEqual(ex[0]["assistant"], "Ruth is helping with the rebuild.")

    def test_multiline_turns_survive(self):
        path = self.vault / "transcripts" / "2026-07-05.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "## Conversation — 09:00 [c1]\n\nUSER: line one of a longer question\n"
            "and line two\n## Conversation — 09:01 [c1]\n\nLISAN: reply\n", encoding="utf-8")
        turns = _parse_transcript(path, "2026-07-05")
        self.assertEqual(len(turns), 2)
        self.assertIn("line two", turns[0]["text"])


class RunTests(_Env):
    def test_full_run_writes_report_history_and_scores(self):
        _transcript(self.vault, "2026-07-05", [
            ("What did I say about the trail-mapping app yesterday?", "You said it hit a wall."),
        ] * 2)
        with patch("lisan.tools.judge.judge_exchange", side_effect=_fake_scores(5)):
            result = run_self_evaluation(self.vault, db_path=self.db, now=_TODAY)
        self.assertEqual(result["judged"], 2)
        self.assertGreaterEqual(result["overall_mean"], 4.5)
        self.assertEqual(result["suggestions"], [])
        report = Path(result["report"])
        self.assertTrue(report.exists())
        self.assertIn("Self-evaluation", report.read_text())
        history = (self.vault / "reports" / "self-eval-history.jsonl").read_text().splitlines()
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0])["judged"], 2)

    def test_low_scores_become_suggestion_loops_with_dedup(self):
        _transcript(self.vault, "2026-07-05", [
            ("Remind me what Dana said about the cabin last week?", "I have no idea."),
        ] * 3)
        with patch("lisan.tools.judge.judge_exchange", side_effect=_fake_scores(2)):
            result = run_self_evaluation(self.vault, db_path=self.db, now=_TODAY)
        self.assertTrue(any("continuity" in s for s in result["suggestions"]))
        loops = list((self.vault / "open_loops").glob("*.md"))
        self.assertGreaterEqual(len(loops), 1)
        fm = load_markdown(loops[0]).frontmatter
        self.assertEqual(fm["origin"], "self")
        self.assertEqual(fm["deviation_class"], "self_eval")
        self.assertTrue(any("self-eval" in str(l) for l in [fm["deviation_fingerprint"]]))
        # report is linked as evidence
        self.assertTrue(any("reports/self-eval" in str(l) for l in fm["links"]))
        # re-run: fingerprint dedup — no second loop for the same ache
        with patch("lisan.tools.judge.judge_exchange", side_effect=_fake_scores(2)):
            run_self_evaluation(self.vault, db_path=self.db, now=_TODAY)
        same = [p for p in (self.vault / "open_loops").glob("*.md")
                if load_markdown(p).frontmatter.get("deviation_fingerprint") == fm["deviation_fingerprint"]]
        self.assertEqual(len(same), 1)

    def test_regression_against_history_is_flagged(self):
        hist = self.vault / "reports" / "self-eval-history.jsonl"
        hist.parent.mkdir(parents=True, exist_ok=True)
        hist.write_text(json.dumps({"date": "2026-06-28", "overall_mean": 4.8}) + "\n", encoding="utf-8")
        _transcript(self.vault, "2026-07-05", [
            ("Walk me through what we planned for the river cleanup?", "We planned things."),
        ] * 3)
        with patch("lisan.tools.judge.judge_exchange", side_effect=_fake_scores(3)):
            result = run_self_evaluation(self.vault, db_path=self.db, now=_TODAY)
        self.assertTrue(any("dropped" in s for s in result["suggestions"]))

    def test_judge_failure_degrades_never_fakes_but_is_never_silent(self):
        """Refusing to invent scores is right. Returning success is not.

        This test previously asserted that a total judge outage returns
        normally with judged=0 — which is exactly what the organ then did,
        every week from 2026-07-15 to 2026-08-13, while `_judge_sample`
        swallowed all ten exceptions. The old assertions were true and the
        behaviour they pinned was the bug: the report is honest, and nobody
        is told. A run that measured nothing now reaches the escalation
        ladder, and still leaves its evidence on disk.
        """
        _transcript(self.vault, "2026-07-05", [
            ("Tell me about the letterpress zine progress please?", "It moved forward."),
        ])

        def boom(*a, **k):
            raise RuntimeError("judge offline")

        with patch("lisan.tools.judge.judge_exchange", side_effect=boom):
            with self.assertRaises(SelfEvalJudgeUnavailable) as caught:
                run_self_evaluation(self.vault, db_path=self.db, now=_TODAY)

        self.assertIn("judged 0 of", str(caught.exception))
        # The evidence survives the failure — report and history are written
        # before the raise, so the ladder has something to point at.
        report = self.vault / "reports" / f"self-eval-{_TODAY.isoformat().replace('-', '')}.md"
        self.assertTrue(report.exists())
        history = self.vault / "reports" / "self-eval-history.jsonl"
        self.assertTrue(history.exists())
        entry = json.loads(history.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(entry["judged"], 0)
        self.assertIsNone(entry["overall_mean"])  # never a fabricated number

    def test_an_empty_window_is_quiet_not_an_error(self):
        """No exchanges to judge is a normal week, not a failure."""
        result = run_self_evaluation(self.vault, db_path=self.db, now=_TODAY)
        self.assertEqual(result["exchanges"], 0)
        self.assertEqual(result["judged"], 0)

    def test_absent_judge_model_reaches_the_provider_as_none_not_the_word(self):
        """The root cause of the five-week outage, pinned.

        `_judge_sample` wrapped the model in `str()`. While the default was
        "openai/gpt-4o" that was a harmless no-op; f5de272 moved the judge to
        the codex provider and set the default to None, and `str(None)` is the
        four-character string "None" — which reaches the CLI as `--model None`
        and exits 1 in under three seconds. Ten calls, ten fast failures, a
        report reading "judge: codex/None; 0/10 scored", and a green job.

        "Let the provider choose" must arrive as None, not as its own spelling.
        """
        _transcript(self.vault, "2026-07-05", [
            ("How did the zine layout turn out in the end?", "Two spreads are done."),
        ])
        seen: dict[str, object] = {}

        def capture(rubric, user, assistant, *, provider=None, model=None, context=None, llm=None):
            seen["provider"] = provider
            seen["model"] = model
            return [{"id": "continuity", "score": 4, "rationale": "test"}]

        with patch("lisan.tools.judge.judge_exchange", side_effect=capture):
            run_self_evaluation(self.vault, db_path=self.db, now=_TODAY)

        self.assertIsNone(seen["model"])
        self.assertNotEqual(seen["model"], "None")
        self.assertEqual(seen["provider"], "codex")

    def test_an_explicit_configured_judge_model_still_passes_through(self):
        _transcript(self.vault, "2026-07-05", [
            ("What happened with the zine deadline this week?", "It slipped a day."),
        ])
        seen: dict[str, object] = {}

        def capture(rubric, user, assistant, *, provider=None, model=None, context=None, llm=None):
            seen["model"] = model
            return [{"id": "continuity", "score": 4, "rationale": "test"}]

        cfg = {"self_eval": {"judge_model": "some/model", "judge_provider": "openrouter"}}
        with patch("lisan.tools.judge.judge_exchange", side_effect=capture):
            run_self_evaluation(self.vault, db_path=self.db, config=cfg, now=_TODAY)

        self.assertEqual(seen["model"], "some/model")


if __name__ == "__main__":
    unittest.main()
