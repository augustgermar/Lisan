from __future__ import annotations

import json
from typing import Any

from ..tools.heuristic_gate import score_text
from ..utils import approx_word_count
from .base import PromptAgent


class WriterAgent(PromptAgent):
    name = "writer"
    prompt_file = "writer_episode_v1"
    output_schema_name = "writer_output"

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        task = str(kwargs.get("task") or "episode")
        summary = self._summary_from_input(user_input)
        payload = {
            "record_type": task,
            "summary": summary,
            "significance": significance,
            "frontmatter": {
                "summary": summary,
                "significance": significance,
                "confidence": "low",
                "confidence_basis": "Deterministic fallback writer",
                "review_after": kwargs.get("review_after") or "",
                "links": kwargs.get("links") or [],
            },
            "sections": self._sections(task, user_input),
            "questions": self._questions(user_input),
            "significance_rationale": self._significance_rationale(user_input, significance),
        }
        return json.dumps(payload, indent=2, ensure_ascii=True)

    def parse_output(self, text: str) -> Any | None:
        parsed = super().parse_output(text)
        if isinstance(parsed, dict):
            return parsed
        return None

    def _summary_from_input(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return "Draft memory"
        first = lines[0]
        return first[:120]

    def _sections(self, task: str, text: str) -> dict[str, str]:
        if task == "questions":
            questions = self._questions(text)
            return {"questions": "\n".join(f"- {q}" for q in questions)}
        if task == "state":
            return {"current_state": text.strip() or "No state summary provided."}
        if task == "decision":
            return {"decision": text.strip() or "No decision text provided."}
        if task == "open_loop":
            return {"open_loop": text.strip() or "No open loop text provided."}
        if task == "knowledge":
            return {"knowledge": text.strip() or "No knowledge statement provided."}
        if task == "entity":
            return {"identity": text.strip() or "No identity details provided."}
        return {
            "event_timeline": text.strip() or "No event timeline provided.",
            "documented_evidence": "No evidence recorded yet.",
            "user_reported_context": "No additional context recorded yet.",
            "interpretations": "No interpretations recorded yet.",
            "operational_consequences": "No consequences recorded yet.",
            "open_questions": "No open questions recorded yet.",
        }

    def _questions(self, text: str) -> list[str]:
        heuristics = score_text(text)
        questions: list[str] = []
        if "decision phrase" in heuristics.reasons:
            questions.append("What alternative options were considered?")
        if "high-risk keyword" in heuristics.reasons:
            questions.append("What factual details need verification before recording this?")
        if "possible named entity" in heuristics.reasons:
            questions.append("Which person or entity is this referring to?")
        if approx_word_count(text) > 60:
            questions.append("What is the simplest summary that still preserves the durable point?")
        if not questions:
            questions.append("What detail would most change the meaning of this memory?")
        return questions[:5]

    def _significance_rationale(self, text: str, significance: str) -> str:
        if significance == "high":
            return "Marked high significance because the input contains durable, review-worthy content."
        if approx_word_count(text) > 80:
            return "Marked medium significance because the input is substantive and may recur."
        return "Marked low significance because the input appears routine."
