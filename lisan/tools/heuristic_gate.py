from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..config import load_config


DECISION_PHRASES = ["i decided", "going forward", "from now on"]
LOOP_PHRASES = ["i need to", "i should", "remind me to"]
HIGH_RISK_KEYWORDS = ["legal", "medical", "child", "custody", "financial", "work conflict"]


@dataclass(slots=True)
class HeuristicResult:
    score: int
    seed_score: int
    narrative_score: int
    action: str
    mode: str
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "seed_score": self.seed_score,
            "narrative_score": self.narrative_score,
            "action": self.action,
            "mode": self.mode,
            "reasons": self.reasons,
        }


def score_text(text: str, config: dict[str, Any] | None = None) -> HeuristicResult:
    config = config or load_config()
    affect_terms = [term.lower() for term in config["heuristic"].get("affect_terms", [])]
    thresholds = config["heuristic"].get("thresholds", {})
    skip_threshold = int(thresholds.get("skip", 3))
    lightweight_threshold = int(thresholds.get("lightweight", 6))

    lowered = text.lower()
    score = 0
    seed_score = 0
    narrative_score = 0
    reasons: list[str] = []

    if "/remember" in lowered:
        score += 5
        narrative_score += 5
        reasons.append("remember flag present")
    if "/forget" in lowered:
        score -= 100
        reasons.append("forget flag present")

    if re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", text):
        score += 3
        seed_score += 3
        reasons.append("possible named entity")

    if sum(1 for _ in re.finditer(r"\b[A-Z][a-z]{2,}\b", text)) >= 3:
        score += 2
        seed_score += 2
        reasons.append("repeated proper nouns")

    if any(phrase in lowered for phrase in DECISION_PHRASES):
        score += 3
        seed_score += 3
        reasons.append("decision phrase")

    if any(phrase in lowered for phrase in LOOP_PHRASES):
        score += 3
        seed_score += 3
        reasons.append("open loop phrase")

    if any(keyword in lowered for keyword in HIGH_RISK_KEYWORDS):
        score += 4
        seed_score += 2
        reasons.append("high-risk keyword")

    if any(term in lowered for term in affect_terms):
        score += 2
        seed_score += 2
        reasons.append("affect term")

    if "make a plan" in lowered or "template" in lowered:
        score += 2
        seed_score += 2
        reasons.append("durable plan request")

    if text.count("```") >= 2 or text.count("\n") <= 2 and len(text) > 150:
        score -= 3
        reasons.append("code-heavy or short factual lookup")

    if len(text) >= 250:
        score += 5
        narrative_score += 5
        reasons.append("long narrative")
    if text.count("\n\n") >= 2:
        score += 3
        narrative_score += 3
        reasons.append("multiple paragraphs")
    if "let me tell you the whole story" in lowered:
        score += 2
        narrative_score += 2
        reasons.append("explicit narrative framing")

    if score <= skip_threshold:
        action = "skip"
    elif score <= lightweight_threshold:
        action = "lightweight"
    else:
        action = "full"

    if seed_score > narrative_score:
        mode = "elicitor"
    elif narrative_score > seed_score:
        mode = "extraction"
    else:
        mode = "undetermined"

    return HeuristicResult(
        score=score,
        seed_score=seed_score,
        narrative_score=narrative_score,
        action=action,
        mode=mode,
        reasons=reasons,
    )

