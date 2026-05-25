from __future__ import annotations

import json
from typing import Any

from .base import PromptAgent
from ..tools.epistemic import discover_pattern_hypotheses


class AnalystAgent(PromptAgent):
    name = "analyst"
    prompt_file = "analyst_v1"
    output_schema_name = "analyst_output"

    def fallback_output(self, user_input: str, significance: str = "high", **kwargs: Any) -> str:
        patterns = discover_pattern_hypotheses(user_input)
        payload = {
            "summary": "Deterministic analyst pass over longitudinal memory.",
            "patterns": patterns,
            "notes": [
                "Patterns are hypotheses only.",
                "Each pattern should be reviewed by Skeptic before Dreamer can use it.",
            ],
        }
        return json.dumps(payload, indent=2, ensure_ascii=True)
