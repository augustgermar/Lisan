from __future__ import annotations

import json
from typing import Any

from ..tools.heuristic_gate import score_text
from .base import PromptAgent


class RouterAgent(PromptAgent):
    name = "router"
    prompt_file = "mode_router_v1"
    output_schema_name = "mode_router_output"

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        score = score_text(user_input, self.config)
        lowered = user_input.lower().strip()
        if lowered.startswith("/"):
            route = "skip"
            reason = "command-like input"
        elif score.action == "skip":
            if score.mode == "elicitor":
                route = "memory"
                reason = "heuristic seed"
            else:
                route = "advice" if any(term in lowered for term in ["?", "recommend", "review", "edit", "help"]) else "skip"
                reason = "heuristic skip fallback"
        elif score.mode == "elicitor":
            route = "memory"
            reason = "heuristic seed"
        elif score.mode == "extraction":
            route = "memory"
            reason = "heuristic extraction"
        else:
            route = "advice" if any(term in lowered for term in ["?", "should i", "could i", "can i", "what do you think"]) else "memory"
            reason = "heuristic ambiguity fallback"
        confidence = "high" if route != "skip" and score.action != "skip" else "medium"
        payload = {
            "route": route,
            "confidence": confidence,
            "reason": reason,
            "topic_hint": self._topic_hint(user_input, route=route),
        }
        return json.dumps(payload, indent=2, ensure_ascii=True)

    def _topic_hint(self, text: str, route: str) -> str:
        text = text.strip()
        if not text:
            return ""
        if route == "advice":
            for marker in ["review", "recommend", "recipe", "edit", "make", "should i", "could i", "help"]:
                if marker in text.lower():
                    return marker
        for separator in [".", "!", "?", "\n", ","]:
            if separator in text:
                return text.split(separator, 1)[0].strip()[:80]
        return text[:80]
