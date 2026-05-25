from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agents import RouterAgent
from .narrative_state import conversation_history, load_narrative_state
from .chat_turns import classify_turn


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
    # Structural fast-fails that don't require semantic judgment
    stripped = text.strip()
    if not stripped:
        return _skip("empty turn")
    if stripped.startswith("/") and not stripped.lower().startswith("/remember") and not stripped.lower().startswith("/forget"):
        return _skip("command input")

    fast_path = classify_turn(text)
    if fast_path.fast_path_used:
        route = fast_path.route if fast_path.route in {"advice", "memory", "skip"} else "advice"
        return TurnRouteDecision(
            route=route,
            confidence="high",
            reason=fast_path.reason,
            topic_hint="",
            source="heuristic",
            raw=fast_path.as_dict(),
        )

    state = load_narrative_state(vault, conversation_id)
    history = conversation_history(vault, conversation_id)
    prompt_context = _render_prompt_context(
        conversation_id=conversation_id,
        state=state.as_dict(),
        history=history,
        listener_score=listener_score or {},
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
        if route not in {"advice", "memory", "skip"}:
            route = "memory"
        return TurnRouteDecision(
            route=route,
            confidence=str(result.get("confidence") or "low").lower(),
            reason=str(result.get("reason") or "").strip(),
            topic_hint=str(result.get("topic_hint") or "").strip(),
            source="llm",
            raw=result,
        )
    except Exception:
        fallback = json.loads(agent.fallback_output(text))
        return TurnRouteDecision(
            route=str(fallback.get("route") or "memory").lower(),
            confidence="low",
            reason="router fallback",
            topic_hint="",
            source="heuristic",
            raw=fallback,
        )


def _skip(reason: str) -> TurnRouteDecision:
    return TurnRouteDecision(
        route="skip", confidence="high", reason=reason,
        topic_hint="", source="heuristic",
        raw={"route": "skip", "confidence": "high", "reason": reason},
    )


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
        f"turn_count: {state.get('turn_count') or 0}",
        f"last_user_text: {state.get('last_user_text') or ''}",
        f"advice_context_active: {advice_context_active}",
        f"advice_topic: {advice_topic}",
    ]
    recent_lines = [f"{t['speaker']}: {t['text']}" for t in history[-6:]]
    recent_text = "\n".join(recent_lines) if recent_lines else "No prior turns recorded."
    return (
        "CURRENT STATE:\n"
        + "\n".join(state_lines)
        + "\n\nRECENT TURNS:\n"
        + recent_text
        + "\n\nCURRENT TEXT:\n"
        + current_text
    )
