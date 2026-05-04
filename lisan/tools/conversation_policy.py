from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    """Pass the LLM router's decision through as policy. No keyword matching."""
    route = str((route_hint or {}).get("route") or "memory").lower()
    if route not in {"advice", "memory", "skip"}:
        route = "memory"
    topic = _topic_label(text, state)
    return ConversationPolicy(
        route=route,
        turn_kind="",
        tone="",
        topic=topic,
        response_style="",
        transition="",
        should_acknowledge=True,
    )


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
