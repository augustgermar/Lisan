from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..config import load_config


DECISION_PHRASES = ["i decided", "going forward", "from now on"]
LOOP_PHRASES = ["i need to", "i should", "remind me to"]
HIGH_RISK_KEYWORDS = ["legal", "medical", "child", "custody", "financial", "work conflict"]
PRACTICAL_ADVICE_PHRASES = [
    "what do you think",
    "could they go well together",
    "could it go well",
    "does this go well",
    "should i make",
    "how do i make",
    "can i make",
]
ADVICE_REQUEST_PHRASES = [
    "please review",
    "review my",
    "review this",
    "look over",
    "proofread",
    "edit my",
    "feedback on",
    "can you recommend",
    "would you recommend",
    "help me choose",
    "what kind of",
    "what type of",
    "best way to",
    "what should i",
    "should i",
    "what would you do",
    "can you help me decide",
]
GENERAL_ADVICE_PHRASES = [
    "should i",
    "what do you think",
    "how do i",
    "how should i",
    "could i",
    "can i",
    "do you think",
    "would you recommend",
    "recommend",
    "best way to",
]
FOOD_CONTEXT_TERMS = [
    "pasta",
    "tuna",
    "mayo",
    "celery",
    "salad",
    "recipe",
    "cooking",
    "ingredients",
    "dinner",
    "lunch",
]


def is_practical_advice_question(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in PRACTICAL_ADVICE_PHRASES) and any(
        term in lowered for term in FOOD_CONTEXT_TERMS
    )


def is_general_advice_question(text: str) -> bool:
    lowered = text.lower()
    return (
        is_practical_advice_question(text)
        or any(phrase in lowered for phrase in ADVICE_REQUEST_PHRASES)
        or (text.rstrip().endswith("?") and any(phrase in lowered for phrase in GENERAL_ADVICE_PHRASES))
    )

TEMPORAL_PHRASES = [
    "this weekend", "last weekend", "this week", "last week",
    "today", "yesterday", "last night", "this morning", "this afternoon",
    "tonight", "earlier today", "earlier this week", "earlier this month",
]

# Short declarative statements that imply a story without telling it — classic elicitor seeds
SEED_PHRASES = [
    # Classic short event seeds ("I had a...")
    "i had a", "i've had", "had a conversation", "had a meeting",
    "had a great", "had a rough", "had a hard", "had a terrible",
    "had a strange", "had a tough", "had a good", "had an interesting",
    # Exclamatory / experiential openers
    "what a ", "what an ", "man i", "oh man", "oh my",
    "it was so ", "it was such a", "i was loving", "i loved",
    "that was so ", "that was such",
    # Implicit story seeds
    "something happened", "something weird", "something interesting",
    "so something", "guess what",
    # Explicit memory / reflection seeds
    "want to talk about", "tell you about", "talk about",
    "i've been thinking about", "my relationship", "lately",
    "ask me about",
    # First-person present-moment sharing
    "i love", "i hate", "i miss", "i feel ", "i'm feeling",
    "i'm having", "i'm making", "just had", "just got",
    "just finished", "just started", "right now i",
]


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

    if any(phrase in lowered for phrase in SEED_PHRASES):
        score += 3
        seed_score += 3
        reasons.append("narrative seed")

    if any(phrase in lowered for phrase in TEMPORAL_PHRASES):
        score += 2
        seed_score += 2
        reasons.append("temporal marker")

    exclamations = min(text.count("!"), 2)
    if exclamations:
        score += exclamations
        seed_score += exclamations
        reasons.append("exclamation")

    if any(keyword in lowered for keyword in HIGH_RISK_KEYWORDS):
        score += 4
        seed_score += 2
        reasons.append("high-risk keyword")

    if any(term in lowered for term in affect_terms):
        score += 2
        seed_score += 2
        reasons.append("affect term")

    if is_practical_advice_question(text):
        score -= 4
        reasons.append("practical food question")
    elif is_general_advice_question(text):
        score += 4
        reasons.append("advice request")

    if "make a plan" in lowered or "template" in lowered:
        score += 2
        seed_score += 2
        reasons.append("durable plan request")

    has_experiential = (
        any(term in lowered for term in affect_terms)
        or any(phrase in lowered for phrase in SEED_PHRASES + TEMPORAL_PHRASES)
    )
    if text.count("```") >= 2:
        score -= 3
        reasons.append("code-heavy")
    elif text.count("\n") <= 1 and len(text) < 120 and not has_experiential and not any(
        phrase in lowered for phrase in DECISION_PHRASES + LOOP_PHRASES
    ):
        score -= 3
        reasons.append("short factual lookup")

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
        if any(reason in reasons for reason in [
            "decision phrase", "open loop phrase", "narrative seed",
            "temporal marker", "exclamation", "affect term",
            "remember flag present",
        ]):
            action = "lightweight"
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
