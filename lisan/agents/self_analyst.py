from __future__ import annotations

import json
from typing import Any

from .base import PromptAgent
from ..tools.epistemic import discover_self_pattern_hypotheses


class SelfAnalystAgent(PromptAgent):
    name = "self_analyst"
    prompt_file = "analyst_self_v1"
    output_schema_name = "analyst_output"

    def fallback_output(self, user_input: str, significance: str = "high", **kwargs: Any) -> str:
        patterns = discover_self_pattern_hypotheses(user_input)
        payload = {
            "summary": "Deterministic self-analyst pass over agent operational history.",
            "patterns": patterns,
            "notes": [
                "Self-patterns describe tendencies, not identity.",
                "Each pattern should be reviewed by Skeptic before integration.",
            ],
        }
        return json.dumps(payload, indent=2, ensure_ascii=True)
