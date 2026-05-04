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

MEMORY_CONTEXT_MARKERS = [
    "i am ",
    "i'm ",
    "i was ",
    "i have ",
    "i had ",
    "i just ",
    "i finally ",
    "i decided ",
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
    response_style: str
    transition: str
    should_acknowledge: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "turn_kind": self.turn_kind,
            "tone": self.tone,
            "topic": self.topic,
            "response_style": self.response_style,
            "transition": self.transition,
            "should_acknowledge": self.should_acknowledge,
        }


def assess_conversation_turn(
    text: str,
    state: Any | None = None,
    listener: dict[str, Any] | None = None,
    advice_context_active: bool = False,
    advice_topic: str | None = None,
    route_hint: dict[str, Any] | None = None,
) -> ConversationPolicy:
    lowered = text.lower().strip()
    listener_action = str((listener or {}).get("action", "")).lower()
    route = str((route_hint or {}).get("route") or "").lower()
    if route not in {"advice", "memory", "skip"}:
        route = "memory"
    if not route:
        route = "memory"
    if not route_hint:
        route = "memory"
        if is_general_advice_question(text) or any(marker in lowered for marker in ADVICE_MARKERS):
            route = "advice"
        elif advice_context_active and listener_action == "skip" and _looks_like_advice_followup(lowered, advice_topic):
            route = "advice"
        elif listener and str(listener.get("mode", "")).lower() == "elicitor":
            route = "memory"
    elif route == "skip" and listener and str(listener.get("mode", "")).lower() == "elicitor":
        route = "memory"

    if route == "skip" and not route_hint:
        route = "advice"

    turn_kind = _turn_kind(lowered, route)
    topic = _topic_label(text, state)
    tone = _tone_for_turn(lowered, state, route, turn_kind)
    response_style = _response_style(route, turn_kind, tone, lowered)
    transition = _transition(route, turn_kind, response_style, topic=topic, advice_topic=advice_topic)
    should_acknowledge = route == "memory" or turn_kind in {"recovery", "reflection"}
    return ConversationPolicy(
        route=route,
        turn_kind=turn_kind,
        tone=tone,
        topic=topic,
        response_style=response_style,
        transition=transition,
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


def _response_style(route: str, turn_kind: str, tone: str, lowered: str) -> str:
    if route == "advice":
        if any(term in lowered for term in ["?", "should i", "could i", "what do you think"]):
            return "direct_answer"
        return "practical_answer"
    if turn_kind == "recovery":
        return "reset"
    if turn_kind == "question":
        return "light_question" if tone in {"wry", "warm"} else "direct_question"
    if turn_kind == "reflection":
        if tone == "wry":
            return "dry_reflection"
        if tone == "warm":
            return "warm_reflection"
        return "steady_reflection"
    return "short_ack"


def _transition(
    route: str,
    turn_kind: str,
    response_style: str,
    topic: str | None = None,
    advice_topic: str | None = None,
) -> str:
    if route == "advice":
        if response_style == "reset":
            return "handoff_memory"
        if topic and advice_topic:
            if _topic_overlap(topic, advice_topic):
                return "continue_advice"
            return "switch_advice_topic"
        return "continue_advice"
    if turn_kind == "recovery":
        return "reset_memory"
    if response_style == "short_ack":
        return "soft_ack"
    return "continue_memory"


def _looks_like_memory_turn(lowered: str) -> bool:
    if any(marker in lowered for marker in MEMORY_CONTEXT_MARKERS):
        return True
    if any(marker in lowered for marker in RECOVERY_MARKERS):
        return True
    return any(marker in lowered for marker in SERIOUS_MARKERS + POSITIVE_MARKERS)


def _looks_like_advice_followup(lowered: str, advice_topic: str | None) -> bool:
    if _looks_like_memory_turn(lowered):
        return False
    if is_general_advice_question(lowered):
        return True
    if not advice_topic:
        return False

    topic_terms = [term for term in advice_topic.lower().split() if len(term) > 2]
    if any(term in lowered for term in topic_terms):
        return True
    if any(marker in lowered for marker in ["also", "too", "still", "another", "more", "and"]):
        return any(term in lowered for term in topic_terms)
    return False


def _topic_overlap(a: str, b: str) -> bool:
    a_terms = {term for term in a.lower().split() if len(term) > 2}
    b_terms = {term for term in b.lower().split() if len(term) > 2}
    if not a_terms or not b_terms:
        return False
    return bool(a_terms & b_terms)


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
