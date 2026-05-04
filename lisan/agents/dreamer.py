from __future__ import annotations

import json
from typing import Any

from ..utils import approx_word_count
from .base import PromptAgent


class DreamerAgent(PromptAgent):
    name = "dreamer"
    prompt_file = "dreamer_compress_v1"
    output_schema_name = "dreamer_output"

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        task = str(kwargs.get("task") or "compress")
        payload = {
            "task": task,
            "summary": self._summary(user_input),
            "findings": self._findings(task, user_input),
            "recommendations": self._recommendations(task, user_input),
            "questions": self._questions(task, user_input),
            "approved": task not in {"contradict", "overfitting"},
            "notes": self._notes(task, user_input),
        }
        return json.dumps(payload, indent=2, ensure_ascii=True)

    def _summary(self, text: str) -> str:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        return first_line[:140] if first_line else "Dreamer analysis"

    def _findings(self, task: str, text: str) -> list[dict[str, str]]:
        lowered = text.lower()
        findings: list[dict[str, str]] = []
        if task == "compress":
            findings.append({"type": "compression", "message": "Preserve claims and history while trimming operational detail."})
        elif task == "primer":
            findings.append({"type": "primer", "message": "Primer should rely only on state, entities, recent episodes, and operating style."})
        elif task == "contradict":
            if "but" in lowered or "however" in lowered or "yet" in lowered:
                findings.append({"type": "contradiction_candidate", "message": "Potential internal tension in the source text."})
        elif task == "epoch":
            findings.append({"type": "epoch", "message": "Only propose an epoch transition for a fundamental state change."})
        elif task == "confidence":
            findings.append({"type": "confidence", "message": "Review explicit confidence decay triggers, not intuition."})
        elif task == "overfitting":
            findings.append({"type": "overfitting", "message": "Check for coherent stories with low evidence density."})
        elif task == "identity_anchor":
            findings.append({"type": "identity_anchor", "message": "Surface recurring narrative patterns as hypotheses."})
        if approx_word_count(text) > 120:
            findings.append({"type": "volume", "message": "Input is long enough to justify a summary or compression pass."})
        return findings

    def _recommendations(self, task: str, text: str) -> list[str]:
        if task == "primer":
            return [
                "Draft a primer from current state and recent episodes only.",
                "Exclude the existing primer from the source bundle.",
            ]
        if task == "contradict":
            return ["Write unresolved contradictions to the contradiction log.", "Do not resolve them unilaterally."]
        if task == "epoch":
            return ["Require Interlocutor approval before applying an epoch.", "Archive the old epoch before replacing it."]
        if task == "confidence":
            return ["Surface candidates for human review.", "Use explicit deterministic decay rules."]
        if task == "overfitting":
            return ["Send candidates to the Skeptic for re-review.", "Prefer low-evidence, high-coherence flags."]
        if task == "identity_anchor":
            return ["Treat anchors as hypotheses.", "Let the user decide what is true or useful."]
        return ["Preserve claims tables.", "Release operational detail first.", "Never destroy source history."]

    def _questions(self, task: str, text: str) -> list[str]:
        if task == "epoch":
            return ["What fundamental state change justifies the epoch transition?"]
        if task == "primer":
            return ["Which recent state or episode most changes the primer?"]
        if task == "contradict":
            return ["Which statement conflicts with the strongest evidence?"]
        return ["What detail should be preserved without overfitting?"]

    def _notes(self, task: str, text: str) -> str:
        return f"Task={task}; words={approx_word_count(text)}"
