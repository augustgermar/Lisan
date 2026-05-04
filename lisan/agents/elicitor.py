from __future__ import annotations

import json
from typing import Any

from .base import PromptAgent


class ElicitorAgent(PromptAgent):
    name = "elicitor"
    prompt_file = "elicitor_v1"
    output_schema_name = "elicitor_output"

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        payload = {
            "response": "Could you say a little more about that?",
            "updated_narrative_state": {
                "open_questions": ["What detail matters most here?"],
                "next_step": "Follow the user's lead and ask one question.",
            },
        }
        return json.dumps(payload, indent=2, ensure_ascii=True)
