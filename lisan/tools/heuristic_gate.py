from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    """Fast pre-filter. Only handles three structural cases; everything else goes to the LLM."""
    lowered = text.strip().lower()

    if "/forget" in lowered:
        return HeuristicResult(
            score=-100, seed_score=0, narrative_score=0,
            action="skip", mode="skip", reasons=["forget flag"],
        )

    if "/remember" in lowered:
        return HeuristicResult(
            score=10, seed_score=5, narrative_score=5,
            action="full", mode="elicitor", reasons=["remember flag"],
        )

    if len(text.strip()) <= 5:
        return HeuristicResult(
            score=0, seed_score=0, narrative_score=0,
            action="skip", mode="skip", reasons=["too short"],
        )

    return HeuristicResult(
        score=5, seed_score=3, narrative_score=2,
        action="lightweight", mode="elicitor", reasons=["llm_required"],
    )
