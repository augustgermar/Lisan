from __future__ import annotations

import json
from typing import Any

from ..paths import sqlite_path
from ..tools.heuristic_gate import score_text
from .base import AgentResult, PromptAgent


class ListenerAgent(PromptAgent):
    name = "listener"
    prompt_file = "listener_v1"
    output_schema_name = "listener_output"

    def fallback_output(self, user_input: str, **kwargs: Any) -> str:
        """Heuristic fallback when no LLM provider is available."""
        score = score_text(user_input, self.config, db_path=sqlite_path())
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
        return json.dumps(payload)

    def run(
        self,
        user_input: str,
        significance: str = "low",
        provider: str | None = None,
        model: str | None = None,
        schema: Any = None,
        **kwargs: Any,
    ) -> AgentResult:
        # Fast-fail: skip the LLM for clearly empty or command-like inputs (≤5 chars)
        stripped = user_input.strip()
        if len(stripped) <= 5 and not any(
            phrase in stripped.lower() for phrase in ["love", "hate", "miss", "feel", "fun"]
        ):
            fallback = self.fallback_output(user_input)
            return AgentResult(text=fallback, data=json.loads(fallback))
        return super().run(
            user_input,
            significance=significance,
            provider=provider,
            model=model,
            schema=schema,
            **kwargs,
        )
