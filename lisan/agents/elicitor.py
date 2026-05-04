from __future__ import annotations

import json
from typing import Any

from .base import PromptAgent


class ElicitorAgent(PromptAgent):
    name = "elicitor"
    prompt_file = "elicitor_v1"
    output_schema_name = "elicitor_output"

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        current_state = kwargs.get("current_state")
        story_thread = self._story_thread(user_input, bool(current_state))
        entities = self._entities(user_input)
        first_clause = self._first_clause(user_input) or "No content provided yet."
        payload = {
            "response": "Could you say a little more about that?",
            "updated_narrative_state": {
                "open_questions": ["What detail matters most here?"],
                "next_step": "Follow the user's lead and ask one question.",
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

    def _story_thread(self, text: str, continuing: bool) -> str:
        if continuing:
            return "Continuation of the current story thread."
        first = self._first_clause(text)
        return first[:120] or "New story thread"

    def _first_clause(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        for separator in [".", "!", "?", "\n"]:
            if separator in text:
                return text.split(separator, 1)[0].strip()
        return text[:160].strip()

    def _entities(self, text: str) -> list[str]:
        import re

        entities: list[str] = []
        for match in re.finditer(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+)?\b", text):
            value = match.group(0)
            if value not in entities:
                entities.append(value)
        return entities[:6]
