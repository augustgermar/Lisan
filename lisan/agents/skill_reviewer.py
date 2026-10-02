from __future__ import annotations

import json
from typing import Any

from .base import PromptAgent


class SkillReviewerAgent(PromptAgent):
    """Reads finished work and proposes changes to skills. The model proposes; a
    deterministic gate (tools/skill_gate.py) disposes — it checks every cited
    event, every name, every byte it would write. The reviewer is never the agent
    that did the work (examiner != examinee), and it never touches the filesystem:
    it returns JSON and nothing else."""

    name = "skill_reviewer"
    prompt_file = "skill_reviewer_v1"
    output_schema_name = "skill_review"

    def render_input(self, user_input: str, **kwargs: Any) -> str:
        # No assistant-identity block: the reviewer is a separate examiner, and
        # handing it Lisan's self-description would invite it to review as Lisan.
        extras = [f"{key.upper()}:\n{value}" for key, value in kwargs.items()
                  if value is not None and key not in self._INTERNAL_KWARGS]
        return "\n\n".join([self.prompt(), *extras, "INPUT:\n" + user_input])

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        # Callers run this agent in raise-on-failure mode so a provider outage is
        # never mistaken for "nothing to learn"; this exists only to satisfy the base.
        return json.dumps({"operations": [], "summary": "the reviewer was unavailable"})
