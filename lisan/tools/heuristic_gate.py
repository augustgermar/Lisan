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


_WORD_THRESHOLD = 25  # messages this long are structurally narrative — route to extraction


def score_text(text: str, config: dict[str, Any] | None = None) -> HeuristicResult:
    """Fast pre-filter. Structural signals only; semantics go to the LLM."""
    lowered = text.strip().lower()

    if "/forget" in lowered:
        return HeuristicResult(
            score=-100, seed_score=0, narrative_score=0,
            action="skip", mode="skip", reasons=["forget flag"],
        )

    if "/remember" in lowered:
        return HeuristicResult(
            score=10, seed_score=5, narrative_score=5,
            action="full", mode="extraction", reasons=["remember flag"],
        )

    stripped = text.strip()
    if len(stripped) <= 5:
        return HeuristicResult(
            score=0, seed_score=0, narrative_score=0,
            action="skip", mode="skip", reasons=["too short"],
        )

    # Word count is structural, not semantic — long messages are narratively complete
    # by definition and should be extracted, not elicited.
    word_count = len(stripped.split())
    if word_count >= _WORD_THRESHOLD:
        return HeuristicResult(
            score=7, seed_score=1, narrative_score=6,
            action="full", mode="extraction", reasons=["long message"],
        )

    return HeuristicResult(
        score=5, seed_score=3, narrative_score=2,
        action="lightweight", mode="elicitor", reasons=["llm_required"],
    )
