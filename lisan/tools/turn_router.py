from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agents import RouterAgent
from .heuristic_gate import is_general_advice_question, score_text
from .narrative_state import conversation_history, load_narrative_state


@dataclass(slots=True)
class TurnRouteDecision:
    route: str
    confidence: str
    reason: str
    topic_hint: str
    source: str
    raw: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "confidence": self.confidence,
            "reason": self.reason,
            "topic_hint": self.topic_hint,
            "source": self.source,
            "raw": self.raw,
        }


def decide_turn_route(
    vault: Path,
    text: str,
    conversation_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    listener_score: dict[str, Any] | None = None,
    advice_context_active: bool = False,
    advice_topic: str | None = None,
) -> TurnRouteDecision:
    score = listener_score or score_text(text).as_dict()

    heuristic = _heuristic_decision(text, score)
    if heuristic is not None:
        return heuristic

    lowered = text.lower().strip()
    if advice_context_active and _looks_like_advice_followup(lowered, advice_topic):
        return TurnRouteDecision(
            route="advice",
            confidence="medium",
            reason="heuristic advice follow-up",
            topic_hint=advice_topic or _topic_hint(text, "advice"),
            source="heuristic",
            raw={
                "route": "advice",
                "confidence": "medium",
                "reason": "heuristic advice follow-up",
                "topic_hint": advice_topic or _topic_hint(text, "advice"),
            },
        )

    state = load_narrative_state(vault, conversation_id)
    history = conversation_history(vault, conversation_id)
    prompt_context = _render_prompt_context(
        conversation_id=conversation_id,
        state=state.as_dict(),
        history=history,
        listener_score=score,
        advice_context_active=advice_context_active,
        advice_topic=advice_topic or "",
        current_text=text,
    )
    agent = RouterAgent(vault=vault)
    try:
        result = agent.run_json(
            json.dumps(prompt_context, indent=2, ensure_ascii=True),
            significance="low",
            provider=provider,
            model=model,
        )
        route = str(result.get("route") or "memory").lower()
        confidence = str(result.get("confidence") or "low").lower()
        reason = str(result.get("reason") or "").strip() or "router returned no reason"
        topic_hint = str(result.get("topic_hint") or "").strip()
        if route not in {"advice", "memory", "skip"}:
            raise ValueError(f"Invalid route from router: {route}")
        return TurnRouteDecision(
            route=route,
            confidence=confidence,
            reason=reason,
            topic_hint=topic_hint,
            source="llm",
            raw=result,
        )
    except Exception:
        fallback = json.loads(agent.fallback_output(text))
        return TurnRouteDecision(
            route=str(fallback.get("route") or "memory").lower(),
            confidence=str(fallback.get("confidence") or "low").lower(),
            reason=str(fallback.get("reason") or "fallback heuristic"),
            topic_hint=str(fallback.get("topic_hint") or "").strip(),
            source="heuristic",
            raw=fallback,
        )


def _heuristic_decision(text: str, score: dict[str, Any]) -> TurnRouteDecision | None:
    lowered = text.lower().strip()
    if not lowered:
        return TurnRouteDecision(
            route="skip",
            confidence="high",
            reason="empty turn",
            topic_hint="",
            source="heuristic",
            raw={"route": "skip", "confidence": "high", "reason": "empty turn"},
        )

    if lowered.startswith("/"):
        return TurnRouteDecision(
            route="skip",
            confidence="high",
            reason="command-like input",
            topic_hint="",
            source="heuristic",
            raw={"route": "skip", "confidence": "high", "reason": "command-like input"},
        )

    if is_general_advice_question(text):
        return TurnRouteDecision(
            route="advice",
            confidence="high",
            reason="heuristic advice request",
            topic_hint=_topic_hint(text, "advice"),
            source="heuristic",
            raw={
                "route": "advice",
                "confidence": "high",
                "reason": "heuristic advice request",
                "topic_hint": _topic_hint(text, "advice"),
            },
        )

    if _looks_like_memory_turn(lowered):
        return TurnRouteDecision(
            route="memory",
            confidence="high",
            reason="heuristic memory seed",
            topic_hint=_topic_hint(text, "memory"),
            source="heuristic",
            raw={
                "route": "memory",
                "confidence": "high",
                "reason": "heuristic memory seed",
                "topic_hint": _topic_hint(text, "memory"),
            },
        )

    mode = str(score.get("mode") or "").lower()
    if mode in {"elicitor", "extraction"}:
        return TurnRouteDecision(
            route="memory",
            confidence="high",
            reason=f"heuristic {mode}",
            topic_hint=_topic_hint(text, "memory"),
            source="heuristic",
            raw={
                "route": "memory",
                "confidence": "high",
                "reason": f"heuristic {mode}",
                "topic_hint": _topic_hint(text, "memory"),
            },
        )

    action = str(score.get("action") or "").lower()
    if action == "skip" and len(lowered) <= 6:
        return TurnRouteDecision(
            route="skip",
            confidence="high",
            reason="very short filler",
            topic_hint="",
            source="heuristic",
            raw={"route": "skip", "confidence": "high", "reason": "very short filler"},
        )

    return None


def _looks_like_memory_turn(lowered: str) -> bool:
    if not lowered.startswith(("i ", "i'm ", "i am ", "i’ve ", "i've ", "my ", "we ", "we're ", "we are ")):
        return False
    return any(
        marker in lowered
        for marker in [
            "i hope",
            "i'm hoping",
            "i am hoping",
            "my goal",
            "i want to",
            "i'm trying to",
            "i am trying to",
            "i'm planning to",
            "i am planning to",
            "i'd like to",
            "i would like to",
            "i think",
            "i believe",
            "i feel",
            "i'm feeling",
            "i was",
            "i have been",
            "i've been",
            "i am",
            "i'm",
            "we are",
            "we're",
        ]
    )


def _looks_like_advice_followup(lowered: str, advice_topic: str | None) -> bool:
    if not lowered:
        return False
    if lowered.startswith(("also", "and", "what about", "how about", "plus", "i also", "i have", "i've got")):
        return True
    if any(marker in lowered for marker in ["also", "too", "still", "another", "more", "plus"]):
        if advice_topic:
            topic_terms = [term for term in advice_topic.lower().split() if len(term) > 2]
            if any(term in lowered for term in topic_terms):
                return True
        return len(lowered) <= 120
    return False


def _topic_hint(text: str, route: str) -> str:
    text = text.strip()
    if not text:
        return ""
    if route == "advice":
        lower = text.lower()
        marker_map = [
            ("review my", "review"),
            ("review this", "review"),
            ("please review", "review"),
            ("proofread", "proofread"),
            ("edit my", "edit"),
            ("feedback on", "feedback"),
            ("recommend", "recommendation"),
            ("suggest", "suggestion"),
            ("help me choose", "choice"),
            ("what kind of", "what kind of"),
            ("what type of", "what type of"),
            ("best way to", "best way"),
            ("what should i", "what should i"),
        ]
        for marker, label in marker_map:
            if marker in lower:
                return _compact_topic_hint(text, marker, label)
    for separator in [".", "!", "?", "\n", ","]:
        if separator in text:
            return text.split(separator, 1)[0].strip()[:80]
    return text[:80]


def _compact_topic_hint(text: str, marker: str, label: str) -> str:
    lower = text.lower()
    idx = lower.find(marker)
    if idx >= 0:
        tail = text[idx + len(marker):].strip(" :,-")
    else:
        tail = text
    tail = re.sub(r"^(to|the|a|an|some|my|your|our)\s+", "", tail, flags=re.IGNORECASE)
    tail = re.sub(r"\s+", " ", tail).strip()
    if not tail:
        return label
    if len(tail) > 60:
        tail = tail[:57].rstrip() + "..."
    if label in {"review", "proofread", "edit", "feedback", "suggestion"}:
        return f"{label}: {tail}"
    return tail


def _render_prompt_context(
    conversation_id: str | None,
    state: dict[str, Any],
    history: list[dict[str, str]],
    listener_score: dict[str, Any],
    advice_context_active: bool,
    advice_topic: str,
    current_text: str,
) -> str:
    state_lines = [
        f"conversation_id: {conversation_id or 'default'}",
        f"story_thread: {state.get('story_thread') or ''}",
        f"mode_status: {state.get('mode_status') or ''}",
        f"emotional_texture: {state.get('emotional_texture') or ''}",
        f"turn_count: {state.get('turn_count') or 0}",
        f"last_user_text: {state.get('last_user_text') or ''}",
        f"last_agent_response: {state.get('last_agent_response') or ''}",
        f"advice_context_active: {advice_context_active}",
        f"advice_topic: {advice_topic}",
        f"listener_score_action: {listener_score.get('action')}",
        f"listener_score_mode: {listener_score.get('mode')}",
        f"listener_score_reasons: {', '.join(listener_score.get('reasons', []))}",
    ]
    recent_lines = []
    for turn in history[-6:]:
        recent_lines.append(f"{turn['speaker']}: {turn['text']}")
    recent_text = "\n".join(recent_lines) if recent_lines else "No prior turns recorded."
    return (
        "CURRENT STATE:\n"
        + "\n".join(state_lines)
        + "\n\nRECENT TURNS:\n"
        + recent_text
        + "\n\nCURRENT TEXT:\n"
        + current_text
    )
