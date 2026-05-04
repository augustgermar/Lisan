from __future__ import annotations

import json

from ..tools.heuristic_gate import score_text
from .base import AgentResult, PromptAgent


class ListenerAgent(PromptAgent):
    name = "listener"
    prompt_file = "listener_v1"
    output_schema_name = "listener_output"

    def run(self, user_input: str, significance: str = "medium", provider: str | None = None, model: str | None = None, schema=None, **kwargs) -> AgentResult:
        score = score_text(user_input, self.config)
        payload = {
            "worth_remembering": score.action != "skip",
            "mode": score.mode if score.action != "skip" else "skip",
            "reason": score.reasons,
            "memory_events": [],
            "action": score.action,
            "score": score.score,
            "seed_score": score.seed_score,
            "narrative_score": score.narrative_score,
        }
        return AgentResult(text=json.dumps(payload, indent=2), data=payload)
