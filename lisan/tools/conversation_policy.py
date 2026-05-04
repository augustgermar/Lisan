from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .heuristic_gate import is_general_advice_question


RECOVERY_MARKERS = [
    "actually",
    "wait",
    "no,",
    "no ",
    "not quite",
    "instead",
    "rather",
    "correction",
    "on second thought",
]

SERIOUS_MARKERS = [
    "worried",
    "anxious",
    "frustrated",
    "sad",
    "angry",
    "upset",
    "stress",
    "stressed",
    "tired",
    "overwhelmed",
]

POSITIVE_MARKERS = [
    "excited",
    "glad",
    "happy",
    "proud",
    "relieved",
    "hopeful",
    "cleaner",
    "safer",
    "fun",
    "great",
    "good",
]

ADVICE_MARKERS = [
    "what can i make",
    "what should i make",
    "what do you think",
    "how do i make",
    "how should i",
    "can i make",
    "could i make",
    "what is a good",
]


@dataclass(slots=True)
class ConversationPolicy:
    route: str
    turn_kind: str
    tone: str
    topic: str
    should_acknowledge: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "turn_kind": self.turn_kind,
            "tone": self.tone,
            "topic": self.topic,
            "should_acknowledge": self.should_acknowledge,
        }


def assess_conversation_turn(
    text: str,
    state: Any | None = None,
    listener: dict[str, Any] | None = None,
    advice_context_active: bool = False,
) -> ConversationPolicy:
    lowered = text.lower().strip()
    listener_action = str((listener or {}).get("action", "")).lower()
    route = "memory"
    if is_general_advice_question(text) or any(marker in lowered for marker in ADVICE_MARKERS):
        route = "advice"
    elif advice_context_active and listener_action == "skip":
        route = "advice"
    elif listener and str(listener.get("mode", "")).lower() == "elicitor":
        route = "memory"

    turn_kind = _turn_kind(lowered, route)
    topic = _topic_label(text, state)
    tone = _tone_for_turn(lowered, state, route, turn_kind)
    should_acknowledge = route == "memory" or turn_kind in {"recovery", "reflection"}
    return ConversationPolicy(
        route=route,
        turn_kind=turn_kind,
        tone=tone,
        topic=topic,
        should_acknowledge=should_acknowledge,
    )


def _turn_kind(lowered: str, route: str) -> str:
    if any(marker in lowered for marker in RECOVERY_MARKERS):
        return "recovery"
    if route == "advice":
        return "answer"
    if lowered.endswith("?"):
        return "question"
    if any(marker in lowered for marker in ["i am ", "i'm ", "i was ", "i have ", "i had "]):
        return "reflection"
    return "reflection"


def _tone_for_turn(lowered: str, state: Any | None, route: str, turn_kind: str) -> str:
    turn_count = int(getattr(state, "turn_count", 0) or 0)
    emotional_texture = str(getattr(state, "emotional_texture", "") or "").lower()
    if route == "advice":
        return "wry" if any(marker in lowered for marker in ["tuna", "pasta", "celery", "mayo", "salad"]) else "plain"
    if turn_kind == "recovery":
        return "dry"
    if emotional_texture in {"tense"} or any(marker in lowered for marker in SERIOUS_MARKERS):
        return "steady"
    if emotional_texture in {"positive"} or any(marker in lowered for marker in POSITIVE_MARKERS):
        return "wry" if turn_count % 2 == 0 else "warm"
    if turn_count == 0:
        return "warm"
    if turn_count % 3 == 0:
        return "wry"
    if turn_count % 3 == 1:
        return "warm"
    return "dry"


def _topic_label(text: str, state: Any | None) -> str:
    if state is not None:
        story_thread = str(getattr(state, "story_thread", "") or "").strip()
        if story_thread:
            return story_thread
    text = text.strip()
    if not text:
        return "that"
    for separator in [".", "!", "?", "\n", ","]:
        if separator in text:
            return text.split(separator, 1)[0].strip()[:80] or "that"
    return text[:80] or "that"
