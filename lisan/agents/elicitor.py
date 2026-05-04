from __future__ import annotations

import json
import re
from typing import Any

from .base import PromptAgent


class ElicitorAgent(PromptAgent):
    name = "elicitor"
    prompt_file = "elicitor_v1"
    output_schema_name = "elicitor_output"

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        """Minimal fallback when no LLM provider is available."""
        first_clause = self._first_clause(user_input) or "No content provided yet."
        entities = self._entities(user_input)
        story_thread = first_clause[:120] if not kwargs.get("current_state") else "Continuation of the current story thread."
        response = self._fallback_response(user_input, entities)
        payload = {
            "response": response,
            "updated_narrative_state": {
                "open_questions": ["What detail matters most here?"],
                "next_step": "Follow the user's lead.",
                "mode_status": "developing",
                "story_thread": story_thread,
                "entities_involved": entities,
                "established": [first_clause],
                "emotional_texture": "unclear",
                "open_threads": [],
                "unresolved": [],
            },
            "questions": ["What detail matters most here?"],
        }
        return json.dumps(payload, indent=2, ensure_ascii=True)

    def _fallback_response(self, text: str, entities: list[str]) -> str:
        """Build a minimally specific response by anchoring to a noun in the input."""
        if entities:
            noun = entities[0]
            return f"Tell me more about {noun}."
        first = self._first_clause(text)
        if first and len(first) > 8:
            return f"Tell me more about that — {first.rstrip('.!?').lower()}."
        return "Tell me more."

    def _first_clause(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        for separator in [".", "!", "?", "\n"]:
            if separator in text:
                return text.split(separator, 1)[0].strip()
        return text[:160].strip()

    def _entities(self, text: str) -> list[str]:
        entities: list[str] = []
        for match in re.finditer(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+)?\b", text):
            value = match.group(0)
            if value not in entities:
                entities.append(value)
        return entities[:6]
