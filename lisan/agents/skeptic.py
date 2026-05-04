from __future__ import annotations

import json
from typing import Any

from .base import PromptAgent


class SkepticAgent(PromptAgent):
    name = "skeptic"
    prompt_file = "skeptic_v1"
    output_schema_name = "skeptic_output"

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        issues = self._issues(user_input)
        payload = {
            "approved": not issues,
            "issues": issues,
            "risk": "low" if not issues else "medium",
            "recommended_action": "promote" if not issues else "revise",
        }
        return json.dumps(payload, indent=2, ensure_ascii=True)

    def _issues(self, text: str) -> list[dict[str, str]]:
        lowered = text.lower()
        issues: list[dict[str, str]] = []
        if "maybe" in lowered or "probably" in lowered:
            issues.append({"type": "uncertainty", "message": "Draft uses uncertain language that should be scoped explicitly."})
        if "i think" in lowered or "i feel" in lowered:
            issues.append({"type": "interpretation", "message": "Distinguish report from interpretation."})
        if "placeholder" in lowered:
            issues.append({"type": "placeholder", "message": "Replace placeholder text with concrete evidence or leave it unresolved."})
        if "legal" in lowered or "medical" in lowered or "financial" in lowered:
            issues.append({"type": "high_risk", "message": "High-risk material needs careful verification and privacy review."})
        return issues
