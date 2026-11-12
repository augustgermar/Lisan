"""Deterministic fallbacks for the Advice, Dreamer and Router agents.

These run when no provider answers, so they are the behaviour a user sees
offline. None of them may invent content.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lisan.agents import router as router_module
from lisan.agents.advice import AdviceAgent, _fallback_answer, _policy_dict
from lisan.agents.dreamer import DreamerAgent
from lisan.agents.router import RouterAgent
from lisan.schemas import get_schema
from lisan.tools.heuristic_gate import HeuristicResult

CONFIG = {"providers": {}}


# ── Advice ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"tone": "wry"}, {"tone": "wry"}),
        ('{"tone": "warm"}', {"tone": "warm"}),
        ("not json", {}),
        ('["a list"]', {}),
        ("   ", {}),
        (None, {}),
        (7, {}),
    ],
)
def test_policy_dict(value: object, expected: dict) -> None:
    assert _policy_dict(value) == expected


def test_identity_question_uses_the_vault_name(tmp_path: Path) -> None:
    assert _fallback_answer("Who are you?") == (
        "My name is Lisan. I am your local personal assistant and memory system."
    )
    primer = tmp_path / "primer"
    primer.mkdir()
    (primer / "identity-core.md").write_text(
        '---\nassistant:\n  name: "Vega"\n  aliases: ["Vega"]\n---\n\n# Identity Core\n', encoding="utf-8"
    )
    assert _fallback_answer("what's your name", vault=tmp_path).startswith("My name is Vega.")


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ({}, "Yep. Start with the simplest version first. The fancy version can wait its turn."),
        (None, "Yep. Start with the simplest version first. The fancy version can wait its turn."),
        ({"transition": "switch_advice_topic", "tone": "wry"},
         "Yep, different topic, same rule: start simple and keep the first version honest."),
        ({"response_style": "direct_answer", "tone": "wry"},
         "Yep. Start with the simplest version first, then add the clever bits if the dish earns them."),
        ({"response_style": "practical_answer", "tone": "warm"},
         "Yep, start with the simplest version first and see what it actually needs."),
        ({"response_style": "reset"}, "Yep, fair. Let’s use the corrected version."),
        ({"response_style": "short_ack", "tone": "warm"}, "Yep."),
        ({"tone": "WRY"}, "Yep. Start with the simplest version first. The fancy version can wait its turn."),
        ({"tone": "warm"}, "Yep, start with the simplest version first and see what it actually needs."),
        ({"tone": "steady"}, "Yes. Start simple, test it, then tighten the screws."),
    ],
)
def test_generic_fallback_follows_the_conversation_policy(policy: dict | None, expected: str) -> None:
    assert _fallback_answer("How should I approach this?", policy=policy) == expected


@pytest.mark.parametrize(
    ("policy", "opening"),
    [
        ({}, "Yep. tuna pasta salad works."),
        ({"tone": "steady"}, "Yes. tuna pasta salad works."),
        ({"transition": "switch_advice_topic", "tone": "warm"}, "Yep, new topic, same pantry."),
        ({"response_style": "direct_answer", "tone": "wry"}, "Yep — tuna pasta salad is old reliable for a reason."),
        ({"response_style": "practical_answer", "tone": "warm"}, "Yep, tuna pasta salad is a good call."),
        ({"response_style": "direct_answer", "tone": "dry"}, "Yep. tuna pasta salad works."),
    ],
)
def test_pantry_fallback_varies_only_the_opening(policy: dict, opening: str) -> None:
    answer = _fallback_answer("I have tuna and celery, ideas?", policy=policy)
    assert answer.startswith(opening)
    assert "mix the pasta with tuna, mayo, and chopped celery" in answer.lower()


def test_advice_agent_accepts_a_json_string_policy(tmp_path: Path) -> None:
    agent = AdviceAgent(vault=tmp_path, config=CONFIG)
    answer = agent.fallback_output(
        "What next?", conversation_policy='{"tone": "steady"}', conversation_history="ignored"
    )
    assert answer == "Yes. Start simple, test it, then tighten the screws."
    assert agent.fallback_output("who are you").startswith("My name is Lisan.")


# ── Dreamer ──────────────────────────────────────────────────────────────────

DREAMER_TASKS = ["compress", "primer", "contradict", "epoch", "confidence", "overfitting", "identity_anchor"]


@pytest.fixture
def dreamer(tmp_path: Path) -> DreamerAgent:
    return DreamerAgent(vault=tmp_path, config=CONFIG)


def test_custom_contract_tasks_degrade_to_honest_empties(dreamer: DreamerAgent) -> None:
    text = "A long bundle. However, it contradicts itself."
    assert json.loads(dreamer.fallback_output(text, task="reconcile")) == {"revisions": []}
    assert json.loads(dreamer.fallback_output(text, task="hindsight")) == {"elevations": []}


@pytest.mark.parametrize("task", DREAMER_TASKS)
def test_every_task_satisfies_the_output_schema(dreamer: DreamerAgent, task: str) -> None:
    payload = json.loads(dreamer.fallback_output("\n\n  First real line.  \nSecond line.", task=task))

    assert set(get_schema("dreamer_output")["required"]) <= set(payload)
    assert payload["task"] == task
    assert payload["summary"] == "First real line."
    assert payload["approved"] is (task not in {"contradict", "overfitting"})
    assert payload["notes"] == f"Task={task}; words=5"
    assert payload["recommendations"] and all(isinstance(item, str) for item in payload["recommendations"])
    assert len(payload["questions"]) == 1
    finding_types = [finding["type"] for finding in payload["findings"]]
    assert finding_types == ([] if task == "contradict" else [{"compress": "compression"}.get(task, task)])


def test_task_defaults_to_compress(dreamer: DreamerAgent) -> None:
    payload = json.loads(dreamer.fallback_output("Notes."))
    assert payload["task"] == "compress"
    assert payload["recommendations"][0] == "Preserve claims tables."
    assert payload["questions"] == ["What detail should be preserved without overfitting?"]


@pytest.mark.parametrize("marker", ["but", "However", "yet"])
def test_contradict_flags_tension_markers_only(dreamer: DreamerAgent, marker: str) -> None:
    flagged = json.loads(dreamer.fallback_output(f"She agreed, {marker} then declined.", task="contradict"))
    assert flagged["findings"] == [
        {"type": "contradiction_candidate", "message": "Potential internal tension in the source text."}
    ]
    assert flagged["approved"] is False
    assert flagged["questions"] == ["Which statement conflicts with the strongest evidence?"]


def test_task_specific_questions(dreamer: DreamerAgent) -> None:
    assert json.loads(dreamer.fallback_output("x", task="epoch"))["questions"] == [
        "What fundamental state change justifies the epoch transition?"
    ]
    assert json.loads(dreamer.fallback_output("x", task="primer"))["questions"] == [
        "Which recent state or episode most changes the primer?"
    ]


def test_long_input_adds_a_volume_finding_and_summary_is_capped(dreamer: DreamerAgent) -> None:
    long_line = "word " * 121
    payload = json.loads(dreamer.fallback_output(long_line, task="compress"))
    assert [finding["type"] for finding in payload["findings"]] == ["compression", "volume"]
    assert len(payload["summary"]) == 140

    short = json.loads(dreamer.fallback_output("word " * 120, task="compress"))
    assert [finding["type"] for finding in short["findings"]] == ["compression"]


def test_empty_input_gets_a_placeholder_summary(dreamer: DreamerAgent) -> None:
    assert json.loads(dreamer.fallback_output("  \n ", task="compress"))["summary"] == "Dreamer analysis"


# ── Router ───────────────────────────────────────────────────────────────────


@pytest.fixture
def route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Route `text` with the heuristic gate pinned to a given action and mode."""
    agent = RouterAgent(vault=tmp_path, config=CONFIG)

    def _route(text: str, action: str = "capture", mode: str = "ambiguous") -> dict:
        result = HeuristicResult(score=0, seed_score=0, narrative_score=0, action=action, mode=mode, reasons=[])
        monkeypatch.setattr(router_module, "score_text", lambda *args, **kwargs: result)
        return json.loads(agent.fallback_output(text))

    return _route


@pytest.mark.parametrize(
    ("text", "action", "mode", "expected_route", "reason", "confidence"),
    [
        ("/forget that", "capture", "extraction", "skip", "command-like input", "medium"),
        ("ok", "skip", "elicitor", "memory", "heuristic seed", "medium"),
        ("can you help", "skip", "none", "advice", "heuristic skip fallback", "medium"),
        ("thanks", "skip", "none", "skip", "heuristic skip fallback", "medium"),
        ("My sister moved", "capture", "elicitor", "memory", "heuristic seed", "high"),
        ("Long story about the move", "capture", "extraction", "memory", "heuristic extraction", "high"),
        ("Should I take the job", "capture", "ambiguous", "advice", "heuristic ambiguity fallback", "high"),
        ("I took the job", "capture", "ambiguous", "memory", "heuristic ambiguity fallback", "high"),
    ],
)
def test_router_fallback_routes(route, text, action, mode, expected_route, reason, confidence) -> None:
    payload = route(text, action=action, mode=mode)
    assert set(get_schema("mode_router_output")["required"]) <= set(payload)
    assert payload["route"] == expected_route
    assert payload["reason"] == reason
    assert payload["confidence"] == confidence


def test_router_topic_hint(route, tmp_path: Path) -> None:
    assert route("Could you review my draft?", action="skip", mode="none")["topic_hint"] == "review"
    assert route("What do you think?")["topic_hint"] == "What do you think"
    assert route("We moved to Lisbon, finally. It was hard.", mode="extraction")["topic_hint"] == (
        "We moved to Lisbon, finally"
    )
    assert route("We moved to Lisbon, finally", mode="extraction")["topic_hint"] == "We moved to Lisbon"
    assert route("x" * 200, mode="extraction")["topic_hint"] == "x" * 80
    agent = RouterAgent(vault=tmp_path, config=CONFIG)
    assert agent._topic_hint("   ", route="memory") == ""


def test_router_fallback_with_the_real_heuristic_gate(tmp_path: Path) -> None:
    agent = RouterAgent(vault=tmp_path, config=CONFIG)
    payload = json.loads(agent.fallback_output("/help"))
    assert payload == {"route": "skip", "confidence": "medium", "reason": "command-like input", "topic_hint": "/help"}
