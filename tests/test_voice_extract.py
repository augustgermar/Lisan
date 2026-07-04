"""WO-2: the voice extraction pass and ratification ceremony.

Fixture transcripts carry planted regularities (invented cast). The
deterministic stages are tested directly; stage 2 is tested with a fake
agent so no provider is needed — including the case where the model
invents evidence, which the gate must reject.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lisan.tools.kernel import kernel_voice_block, verify_kernel
from lisan.tools.voice_extract import (
    Turn,
    collect_agent_turns,
    ratify_voice,
    run_extraction,
    sample_for_model,
    surface_stats,
    tag_provenance,
    validate_candidates,
)

_DAY1 = """---
{"date": "2026-06-01"}
---

## Conversation — 09:00 [garden-1]

USER: Ruth planted wisteria at the Homestead today.

## Conversation — 09:01 [garden-1]

LISAN: That sounds like a good day's work. I'll remember that about Ruth.

## Conversation — 09:05 [garden-1]

LISAN: Noted — the wisteria goes in the south bed. I'll remember that.
"""

_DAY2 = """---
{"date": "2026-06-02"}
---

## Conversation — 10:00 [cats-1]

USER: Momo knocked a glass off the counter again.

## Conversation — 10:01 [cats-1]

LISAN: Classic Momo. I'll remember that about her — the counter is her domain now.

## Conversation — 10:02 [cats-1]

LISAN: Wait — Boots did it too? I had Momo as the counter cat. What happened there?
"""

_DAY3 = """---
{"date": "2026-06-03"}
---

## Conversation — 11:00 [work-1]

LISAN: That's a hard week, Dana leaving. I'll remember that, and the handoff date.

## Conversation — 11:30

LISAN: Huh — Varga is back in Larkspur? I had him in the city. What changed?
"""


def _seed_vault(tmp_path: Path) -> Path:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir(parents=True)
    (transcripts / "2026-06-01.md").write_text(_DAY1, encoding="utf-8")
    (transcripts / "2026-06-02.md").write_text(_DAY2, encoding="utf-8")
    (transcripts / "2026-06-03.md").write_text(_DAY3, encoding="utf-8")
    return tmp_path


def _seed_kernel(vault: Path) -> None:
    from lisan.tools.onboarding import _write_identity_core

    path = vault / "primer" / "identity-core.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_identity_core(path, name="Ruth")


class FakeAgent:
    def __init__(self, candidates):
        self.candidates = candidates
        self.last_input = None

    def run_json(self, user_input, **kwargs):
        self.last_input = user_input
        return {"candidates": self.candidates}


# ── Stage 1 ──────────────────────────────────────────────────────────────────


def test_collect_agent_turns_finds_only_agent_turns(tmp_path):
    vault = _seed_vault(tmp_path)
    turns = collect_agent_turns(vault)
    assert len(turns) == 6
    assert all("USER:" not in t.text for t in turns)
    assert {t.conversation for t in turns} == {"garden-1", "cats-1", "work-1", "day:2026-06-03"}


def test_surface_stats_shape(tmp_path):
    stats = surface_stats(collect_agent_turns(_seed_vault(tmp_path)))
    assert stats["turns"] == 6
    assert stats["conversations"] == 4
    assert stats["sentences_median"] >= 1
    assert 0 <= stats["question_rate"] <= 1


def test_sample_spans_head_and_tail():
    turns = [Turn(date="d", time="t", conversation=str(i), text=str(i)) for i in range(200)]
    sample = sample_for_model(turns, cap=40)
    assert len(sample) == 40
    assert sample[0].text == "0" and sample[-1].text == "199"


# ── Stage 2: the evidence gate ───────────────────────────────────────────────


def _good_candidate():
    return {
        "statement": "States explicitly that it will remember something.",
        "category": "move",
        "evidence": [
            {"quote": "I'll remember that about Ruth."},
            {"quote": "I'll remember that about her"},
            {"quote": "I'll remember that, and the handoff date."},
        ],
    }


def test_valid_candidate_passes_with_resolved_evidence(tmp_path):
    turns = collect_agent_turns(_seed_vault(tmp_path))
    valid, rejected = validate_candidates([_good_candidate()], turns)
    assert len(valid) == 1 and not rejected
    assert len(valid[0].evidence) == 3
    assert len(valid[0].conversations) == 3


def test_fabricated_evidence_is_rejected(tmp_path):
    turns = collect_agent_turns(_seed_vault(tmp_path))
    fabricated = {
        "statement": "Quotes poetry when the user is sad.",
        "category": "move",
        "evidence": [
            {"quote": "As the bard said, all the world's a stage."},
            {"quote": "Poetry heals what prose cannot."},
            {"quote": "I'll remember that about Ruth."},
        ],
    }
    valid, rejected = validate_candidates([fabricated], turns)
    assert not valid
    assert "insufficient resolved evidence" in rejected[0].rejected_reason


def test_single_conversation_evidence_is_rejected(tmp_path):
    turns = collect_agent_turns(_seed_vault(tmp_path))
    narrow = {
        "statement": "Mentions the wisteria constantly.",
        "category": "temperament",
        "evidence": [
            {"quote": "I'll remember that about Ruth."},
            {"quote": "the wisteria goes in the south bed"},
            {"quote": "That sounds like a good day's work."},
        ],
    }
    valid, rejected = validate_candidates([narrow], turns)
    assert not valid
    assert "conversation" in rejected[0].rejected_reason


def test_malformed_candidates_are_rejected(tmp_path):
    turns = collect_agent_turns(_seed_vault(tmp_path))
    valid, rejected = validate_candidates(
        [{"statement": "", "category": "move", "evidence": []},
         {"statement": "Real statement.", "category": "sonnet", "evidence": []}],
        turns,
    )
    assert not valid and len(rejected) == 2


# ── Stage 3: provenance ──────────────────────────────────────────────────────


def test_provenance_factory_vs_earned(tmp_path):
    turns = collect_agent_turns(_seed_vault(tmp_path))
    valid, _ = validate_candidates([_good_candidate()], turns)
    tag_provenance(valid, "Plainspoken, warm, confident. One clean answer beats three hedged ones.")
    assert valid[0].provenance == "earned"
    tag_provenance(valid, "It states explicitly that it will remember something the user says.")
    assert valid[0].provenance == "factory"


# ── The full pass ────────────────────────────────────────────────────────────


def test_run_extraction_writes_artifact(tmp_path):
    vault = _seed_vault(tmp_path)
    curious = {
        "statement": "Surfaces a stored discrepancy as a curious question.",
        "category": "move",
        "evidence": [
            {"quote": "I had Momo as the counter cat."},
            {"quote": "I had him in the city. What changed?"},
            {"quote": "What happened there?"},
        ],
    }
    agent = FakeAgent([_good_candidate(), curious])
    result = run_extraction(vault, agent=agent, min_invariants=2, min_conversations=2, config={})
    assert len(result["candidates"]) == 2
    assert result["eligible"] is True
    artifact = Path(result["artifact"])
    assert artifact.exists()
    assert "agent_turns" in str(agent.last_input)
    from lisan.frontmatter import load_markdown

    fm = load_markdown(artifact).frontmatter
    assert fm["voice_extraction"]["eligible"] is True


def test_run_extraction_ineligible_below_threshold(tmp_path):
    vault = _seed_vault(tmp_path)
    result = run_extraction(vault, agent=FakeAgent([_good_candidate()]),
                            min_invariants=5, min_conversations=3, config={})
    assert result["eligible"] is False


# ── The ceremony ─────────────────────────────────────────────────────────────


def test_ratify_writes_kernel_voice_with_provenance(tmp_path):
    vault = _seed_vault(tmp_path)
    _seed_kernel(vault)
    result = run_extraction(vault, agent=FakeAgent([_good_candidate()]),
                            min_invariants=1, min_conversations=2, config={})
    path = ratify_voice(vault, artifact_path=Path(result["artifact"]), provisional=True)
    text = path.read_text(encoding="utf-8")
    assert "## Voice" in text
    assert "States explicitly that it will remember" in text
    assert "## Voice Provenance" in text
    assert "agent-provisional — pending owner review" in text
    assert verify_kernel(vault) == "ok"
    voice = kernel_voice_block(vault)
    assert "States explicitly" in voice
    assert "Provenance" not in voice  # provenance never enters the prompt


def test_ratified_voice_reaches_conversation_prompt(tmp_path):
    from lisan.agents.conversation import ConversationAgent

    vault = _seed_vault(tmp_path)
    _seed_kernel(vault)
    result = run_extraction(vault, agent=FakeAgent([_good_candidate()]),
                            min_invariants=1, min_conversations=2, config={})
    ratify_voice(vault, artifact_path=Path(result["artifact"]), provisional=True)
    rendered = ConversationAgent(vault=vault).prompt()
    assert "States explicitly that it will remember" in rendered
    assert "Plainspoken, warm, confident" not in rendered


def test_ratify_refuses_empty_artifact(tmp_path):
    vault = _seed_vault(tmp_path)
    _seed_kernel(vault)
    result = run_extraction(vault, agent=FakeAgent([]), min_invariants=1, min_conversations=1, config={})
    with pytest.raises(ValueError):
        ratify_voice(vault, artifact_path=Path(result["artifact"]))


def test_reratification_replaces_not_duplicates(tmp_path):
    vault = _seed_vault(tmp_path)
    _seed_kernel(vault)
    result = run_extraction(vault, agent=FakeAgent([_good_candidate()]),
                            min_invariants=1, min_conversations=2, config={})
    ratify_voice(vault, artifact_path=Path(result["artifact"]), provisional=True)
    path = ratify_voice(vault, artifact_path=Path(result["artifact"]), provisional=False)
    text = path.read_text(encoding="utf-8")
    assert text.count("## Voice Provenance") == 1
    assert text.count("States explicitly that it will remember") == 1
    assert "by owner" in text
