"""Heuristic pre-filter for the capture pipeline.

Design principle: this gate uses STRUCTURAL signals (message length, punctuation,
code formatting, explicit decision/action verbs, entity references) to score input
cheaply before an LLM call. Content-interpretation signals (what topics are
high-stakes, what words are emotionally significant) are config-driven and
vault-local, not hardcoded, because importance is personal and varies by user.

The gate's job is to filter obvious noise and escalate obvious significance. The
model handles everything in between. When uncertain, prefer escalation (let the
model see it) over suppression (silently drop it).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
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


# ── Phrase banks ──────────────────────────────────────────────────────────────

_DECISION_PHRASES = (
    "i decided", "i've decided", "we decided", "i'm going to", "we're going to",
    "going forward", "from now on", "made a decision", "decided to", "i will",
    "we will", "the plan is", "i plan to", "we plan to",
)

_OPEN_LOOP_PHRASES = (
    "i need to", "i should", "remind me to", "i have to", "don't let me forget",
    "i want to remember", "need to follow up", "need to check", "follow up on",
    "i must", "we need to", "we should",
)

_DURABLE_PLAN_PHRASES = (
    "plan for", "template for", "checklist", "how should i", "what's the best way",
    "best approach", "strategy for", "system for", "routine for",
)

_NARRATIVE_PHRASES = (
    "let me tell you", "the whole story", "here's what happened", "so what happened",
    "to summarize", "long story", "basically what happened",
)

_CLOSURE_PHRASES = (
    "anyway", "so that's that", "moving on", "next topic", "change the subject",
    "that's the story", "end of story",
)

_CORRECTION_PHRASES = (
    # Explicit admissions — essentially zero false positive rate
    "correction:",
    "i misspoke",
    "i was wrong",
    "i got that wrong",
    "to correct myself",
    "i need to correct",
    # Compound forms — specific enough when the corrective word leads the clause
    "that's wrong,",
    "to correct that,",
    "i meant ",          # "I meant Tuesday" — trailing space avoids "I mentioned"
    "no, it's ",         # "no, it's 30, not 28"
    "no, he's ",
    "no, she's ",
    "no, they're ",
    "it's actually ",    # "It's actually 30"
    "it is actually ",
    "actually, it's ",   # "Actually, it's 30 not 28"
    "actually, he's ",
    "actually, she's ",
    "actually, they're ",
)

_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-z]{1,}\b")

_SKIP_WORDS = frozenset({
    "I", "My", "The", "A", "An", "It", "He", "She", "They", "We", "You",
    "No", "Yes", "Ok", "Okay", "So", "But", "And", "Or", "In", "On", "At",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
})

_DEFAULT_BIOGRAPHICAL_TERMS = (
    "born", "mom", "dad", "mother", "father", "sister", "brother",
    "wife", "husband", "daughter", "son",
    "grew up", "hometown", "birthday", "years old",
)

_DEFAULT_AFFECT_TERMS = [
    "angry", "sad", "anxious", "excited", "afraid", "frustrated",
    "happy", "proud", "surprised", "confused", "hurt", "nervous",
    "grateful", "relieved", "disappointed", "awful", "amazing",
    "terrible", "wonderful", "great", "fantastic", "incredible",
    "beautiful", "lovely", "loving", "loved", "love", "enjoy",
    "enjoyed", "enjoying", "hate", "hated", "miss", "missing",
    "tired", "rough", "tough", "exhausted", "drained", "overwhelmed",
    "stressed", "annoyed", "bored", "sick", "lonely",
    "scared", "scary", "fear", "fearful", "worried", "worry", "dread",
    "dreading", "panic", "panicked", "terrified", "uneasy", "shaken",
    "unsettled", "heartbroken", "devastated", "ashamed", "guilty",
    "regretful", "bitter", "resentful", "betrayed", "blindsided",
    "humiliated",
]


def score_text(
    text: str,
    config: dict[str, Any] | None = None,
    db_path: Path | None = None,
    vault: Path | None = None,
) -> HeuristicResult:
    """Full spec-compliant heuristic scoring. Structural signals only; semantics go to the LLM."""
    lowered = text.strip().lower()
    stripped = text.strip()

    # Hard overrides
    if "/forget" in lowered:
        return HeuristicResult(
            score=-100, seed_score=0, narrative_score=0,
            action="skip", mode="skip", reasons=["forget flag"],
        )
    if "/remember" in lowered:
        return HeuristicResult(
            score=10, seed_score=0, narrative_score=10,
            action="full", mode="extraction", reasons=["remember flag"],
        )
    if len(stripped) <= 5:
        return HeuristicResult(
            score=0, seed_score=0, narrative_score=0,
            action="skip", mode="skip", reasons=["too short"],
        )

    score = 0
    reasons: list[str] = []

    # ── Positive signals ──────────────────────────────────────────────────────

    # Named entity already in vault (+3 per entity, cap at +6)
    vault_hits = _count_vault_entity_hits(stripped, db_path)
    if vault_hits:
        entity_bonus = min(vault_hits * 3, 6)
        score += entity_bonus
        reasons.append("vault entity match")

    # Proper noun repeated 3+ times (+2)
    repeated = _count_repeated_proper_nouns(stripped)
    if repeated:
        score += 2
        reasons.append("repeated proper noun")

    # Decision phrase (+3)
    if any(phrase in lowered for phrase in _DECISION_PHRASES):
        score += 3
        reasons.append("decision phrase")

    # Open-loop phrase (+3)
    if any(phrase in lowered for phrase in _OPEN_LOOP_PHRASES):
        score += 3
        reasons.append("open-loop phrase")

    # Correction phrase (+4, forces full extraction so the supersede path fires)
    if is_correction_turn(stripped):
        score += 4
        reasons.append("correction phrase")

    # Vault-local high-stakes term (+4)
    high_stakes = _get_high_stakes_terms(config, vault=vault)
    if high_stakes and any(term in lowered for term in high_stakes):
        score += 4
        reasons.append("high-stakes term")

    # Affect terms: +2 for first hit, +1 per additional, cap at +4
    affect_terms = _get_affect_terms(config)
    affect_hits = sum(1 for term in affect_terms if term in lowered)
    if affect_hits:
        affect_bonus = min(affect_hits + 1, 4)
        score += affect_bonus
        reasons.append("affect term")

    # Durable plan/template request (+2)
    if any(phrase in lowered for phrase in _DURABLE_PLAN_PHRASES):
        score += 2
        reasons.append("durable plan request")

    # Biographical density: multiple family/life facts in one message (+3)
    if _has_biographical_density(lowered, len(stripped.split()), config=config):
        score += 3
        reasons.append("biographical content")

    # ── Negative signals ──────────────────────────────────────────────────────

    # Pure code formatting (>80% code blocks) (-3)
    if _is_mostly_code(stripped):
        score -= 3
        reasons.append("mostly code")

    # Pure factual lookup — single question, no personal stake (-3)
    if _is_factual_lookup(stripped, lowered):
        score -= 3
        reasons.append("factual lookup")

    # ── Action level ──────────────────────────────────────────────────────────
    thresholds = (config or {}).get("heuristic", {}).get("thresholds", {})
    skip_threshold = int(thresholds.get("skip", 3))
    lightweight_threshold = int(thresholds.get("lightweight", 6))

    # Use < not <= so the boundary score itself steps up to the next level.
    # e.g. skip_threshold=3: score<3 → skip, score=3 → lightweight
    if score < skip_threshold:
        action = "skip"
    elif score < lightweight_threshold:
        action = "lightweight"
    else:
        action = "full"

    # ── Mode: seed vs narrative ───────────────────────────────────────────────
    word_count = len(stripped.split())
    seed_score, narrative_score, mode = _classify_mode(
        text=stripped,
        lowered=lowered,
        word_count=word_count,
        reasons=reasons,
        config=config,
    )

    # Hard overrides based on action
    if action == "skip":
        mode = "skip"

    return HeuristicResult(
        score=score,
        seed_score=seed_score,
        narrative_score=narrative_score,
        action=action,
        mode=mode,
        reasons=reasons,
    )


def is_correction_turn(text: str) -> bool:
    """Return True when the text contains an unambiguous self-correction signal."""
    lowered = text.strip().lower()
    return any(phrase in lowered for phrase in _CORRECTION_PHRASES)


# ── Mode classification ───────────────────────────────────────────────────────

def _classify_mode(
    text: str,
    lowered: str,
    word_count: int,
    reasons: list[str],
    config: dict[str, Any] | None = None,
) -> tuple[int, int, str]:
    seed_score = 0
    narrative_score = 0

    # Narrative indicators
    if word_count >= 250:
        narrative_score += 5
    if "\n\n" in text:
        # Multiple paragraphs — check for temporal sequencing
        if any(w in lowered for w in ("then", "after", "before", "first", "next", "finally", "later")):
            narrative_score += 3
        else:
            narrative_score += 1
    if any(phrase in lowered for phrase in _NARRATIVE_PHRASES):
        narrative_score += 2
    # Biographical facts: multiple life/family facts → extraction (score > seed)
    if _has_biographical_density(lowered, word_count, config=config):
        narrative_score += 4

    # Seed indicators
    if word_count < 60:
        # Short personal statement — check for event/emotional content
        first_person = any(w in lowered for w in ("i ", "i'm", "i've", "i had", "i was", "i feel", "my ", "me "))
        has_event_hint = any(w in lowered for w in (
            "happened", "went", "met", "saw", "heard", "got", "had", "did",
            "found", "tried", "made", "said", "told", "showed", "noticed",
        ))
        if first_person and has_event_hint:
            seed_score += 5
        elif first_person:
            seed_score += 3
        elif has_event_hint:
            # Implied event without explicit "I" — still a seed
            seed_score += 2
    if any(phrase in lowered for phrase in ("ask me about", "ask me how", "want to talk about")):
        seed_score += 3
    # Implies story without telling it
    if word_count < 40 and any(w in lowered for w in ("something", "a thing", "this thing", "an idea")):
        seed_score += 2
    # Exclamatory expressions that imply a notable event ("what a day", "oh man", etc.)
    _EXCLAMATORY_SEEDS = (
        "what a day", "what a week", "what a night", "what a year", "what a time",
        "what a mess", "what a trip", "oh man", "oh wow", "oh no", "oh my",
    )
    if any(phrase in lowered for phrase in _EXCLAMATORY_SEEDS):
        seed_score += 3
    elif word_count < 20 and "!" in text and not first_person:
        # Short exclamation without "I" — someone reacting to an event
        seed_score += 2

    if narrative_score > seed_score:
        mode = "extraction"
    else:
        mode = "elicitor"

    return seed_score, narrative_score, mode


# ── Vault entity lookup ───────────────────────────────────────────────────────

def _count_vault_entity_hits(text: str, db_path: Path | None) -> int:
    if not db_path or not db_path.exists():
        return 0
    proper_nouns = {m.group(0) for m in _PROPER_NOUN_RE.finditer(text) if m.group(0) not in _SKIP_WORDS}
    if not proper_nouns:
        return 0
    try:
        conn = sqlite3.connect(db_path)
        try:
            hits = 0
            for name in proper_nouns:
                row = conn.execute(
                    "SELECT 1 FROM entity_aliases WHERE alias = ? LIMIT 1", (name,)
                ).fetchone()
                if row:
                    hits += 1
                    continue
                row = conn.execute(
                    "SELECT 1 FROM files WHERE type='entity' AND summary LIKE ? LIMIT 1",
                    (f"%{name}%",)
                ).fetchone()
                if row:
                    hits += 1
            return hits
        finally:
            conn.close()
    except Exception:
        return 0


# ── Repeated proper noun ──────────────────────────────────────────────────────

def _count_repeated_proper_nouns(text: str) -> int:
    counts: dict[str, int] = {}
    for m in _PROPER_NOUN_RE.finditer(text):
        word = m.group(0)
        if word not in _SKIP_WORDS:
            counts[word] = counts.get(word, 0) + 1
    return sum(1 for c in counts.values() if c >= 3)


# ── Code block detection ──────────────────────────────────────────────────────

def _is_mostly_code(text: str) -> bool:
    lines = text.splitlines()
    if not lines:
        return False
    in_block = False
    code_lines = 0
    for line in lines:
        if line.strip().startswith("```"):
            in_block = not in_block
        if in_block:
            code_lines += 1
    return (code_lines / len(lines)) > 0.80


# ── Factual lookup detection ──────────────────────────────────────────────────

def _is_factual_lookup(text: str, lowered: str) -> bool:
    sentences = [s.strip() for s in re.split(r"[.!?\n]", text) if s.strip()]
    if len(sentences) > 2:
        return False
    if not lowered.endswith("?") and "?" not in lowered:
        return False
    first_person = any(w in lowered for w in ("i ", "i'm", "my ", "me ", "i've"))
    if first_person:
        return False
    # Single question with no personal stake → factual lookup
    return True


def _unquote_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_yaml_terms_list(text: str) -> list[str]:
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.startswith("terms:"):
            continue
        raw = stripped.partition(":")[2].strip()
        if raw:
            if raw.startswith("[") and raw.endswith("]"):
                inner = raw[1:-1].strip()
                if not inner:
                    return []
                return [
                    _unquote_yaml_scalar(item).strip().lower()
                    for item in inner.split(",")
                    if _unquote_yaml_scalar(item).strip()
                ]
            value = _unquote_yaml_scalar(raw).strip().lower()
            return [value] if value else []
        terms: list[str] = []
        for child in lines[idx + 1 :]:
            child_stripped = child.strip()
            if not child_stripped or child_stripped.startswith("#"):
                continue
            indent = len(child) - len(child.lstrip())
            if indent == 0:
                break
            if not child_stripped.startswith("- "):
                continue
            item = _unquote_yaml_scalar(child_stripped[2:]).strip().lower()
            if item:
                terms.append(item)
        return terms
    return []


def _get_high_stakes_terms(
    config: dict[str, Any] | None,
    vault: Path | None = None,
) -> list[str]:
    """Read high-stakes terms from the user's vault-local config.

    These are personal — what topics matter to THIS user. Not hardcoded in
    source. Vault-local primer/high-stakes.yaml takes priority; config fallback
    exists for callers who prefer to keep the list in config.yaml.
    """
    if vault:
        hs_path = vault / "primer" / "high-stakes.yaml"
        if hs_path.exists():
            try:
                return _parse_yaml_terms_list(hs_path.read_text(encoding="utf-8"))
            except Exception:
                return []

    if config:
        terms = config.get("heuristic", {}).get("high_stakes_terms")
        if terms:
            return [str(term).strip().lower() for term in terms if str(term).strip()]

    return []


# ── Biographical density ──────────────────────────────────────────────────────

def _get_biographical_terms(config: dict[str, Any] | None) -> tuple[str, ...]:
    if config:
        terms = config.get("heuristic", {}).get("biographical_terms")
        if terms is not None:
            return tuple(str(term).strip().lower() for term in terms if str(term).strip())
    return _DEFAULT_BIOGRAPHICAL_TERMS


def _has_biographical_density(
    lowered: str,
    word_count: int,
    config: dict[str, Any] | None = None,
) -> bool:
    """Multiple distinct biographical nouns in one message suggest extraction mode."""
    if word_count < 15:
        return False
    bio_terms = _get_biographical_terms(config)
    hits = sum(1 for t in bio_terms if t in lowered)
    return hits >= 2


# ── Affect terms ─────────────────────────────────────────────────────────────

def _get_affect_terms(config: dict[str, Any] | None) -> list[str]:
    if config:
        terms = config.get("heuristic", {}).get("affect_terms")
        if terms is not None:
            return [str(term).strip().lower() for term in terms if str(term).strip()]
    return list(_DEFAULT_AFFECT_TERMS)
