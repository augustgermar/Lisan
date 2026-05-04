from __future__ import annotations

import json
from typing import Any

from .base import PromptAgent


class InterlocutorAgent(PromptAgent):
    name = "interlocutor"
    prompt_file = "interlocutor_v1"
    output_schema_name = "interlocutor_output"

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        questions = self._questions(user_input)
        payload = {
            "response": "I need a little more detail to proceed.",
            "updated_narrative_state": {
                "open_questions": questions,
                "next_step": "Ask the highest-priority clarification question.",
            },
            "questions": questions,
        }
        return json.dumps(payload, indent=2, ensure_ascii=True)

    def _questions(self, text: str) -> list[str]:
        questions: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if line.endswith("?"):
                questions.append(line)
        if not questions:
            questions = ["What is the one detail that would change the draft most?"]
        return questions[:3]
