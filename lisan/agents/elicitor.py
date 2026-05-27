from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from ..tools.operating_style import load_operating_style
from ..tools.primer_index import known_names as _primer_known_names, name_in_text
from ..tools.stopwords import SENTENCE_INITIAL_OR_TOOL_STOPWORDS, MONTH_STOPWORDS, DAY_STOPWORDS
from .base import PromptAgent


class ElicitorAgent(PromptAgent):
    name = "elicitor"
    prompt_file = "elicitor_v1"
    output_schema_name = "elicitor_output"

    # Optional follow-up: when the structured prompt fails, attempt one
    # last-ditch single-sentence prose call before resorting to the heuristic
    # rotation. Tracked per-instance to bound the recursion at one extra call.
    _attempted_prose_recovery: bool = False

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        """Build a minimal but persona-aware fallback elicitor payload.

        Finding #9: primer-aware entity selection + rotating openers seeded by
        a hash of the last transcript turn XOR the current hour-of-day. The
        rotation stays deterministic per turn-position but varies across a
        day, so a user replaying a captured input on the same evening still
        sees a different prompt than they got that morning.
        """
        # Optional follow-up: try one prose-completion recovery call first.
        recovered = self._try_prose_recovery(user_input, kwargs)
        if recovered is not None:
            return recovered

        first_clause = self._first_clause(user_input) or "No content provided yet."
        entities = self._entities(user_input)
        story_thread = first_clause[:120] if not kwargs.get("current_state") else "Continuation of the current story thread."
        response = self._fallback_response(user_input, entities)
        payload = {
            "response": response,
            "updated_narrative_state": {
                "open_questions": ["What detail matters most here?"],
                "next_step": "Follow the user's lead.",
                "mode_status": "developing",
                "story_thread": story_thread,
                "entities_involved": entities,
                "established": [first_clause],
                "emotional_texture": "unclear",
                "open_threads": [],
                "unresolved": [],
            },
            "questions": ["What detail matters most here?"],
        }
        return json.dumps(payload, indent=2, ensure_ascii=True)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _try_prose_recovery(self, user_input: str, kwargs: dict[str, Any]) -> str | None:
        """Optional follow-up for Finding #9: when the structured call has
        failed, try one tiny prose call and lift a question from its output.

        Returns a complete JSON payload string on success, or ``None`` to fall
        through to the heuristic. Bounded to one attempt per agent instance
        to avoid runaway recursion.
        """
        if self._attempted_prose_recovery:
            return None
        # Be conservative — the structured path already failed, and we don't
        # want to chain provider errors. If anything below raises we silently
        # fall through to the deterministic rotation.
        self._attempted_prose_recovery = True
        try:
            prose_prompt = (
                "In ONE short sentence ending with a question mark, ask the user "
                "a non-leading follow-up that invites them to say more. Do not "
                "summarize; do not name an emotion. Input:\n\n"
                + user_input.strip()
            )
            response = self.llm.complete(
                prose_prompt,
                agent=self.name + ".prose_recovery",
                significance="low",
            )
            sentence = self._extract_question(response.text)
            if not sentence:
                return None
            first_clause = self._first_clause(user_input) or "No content provided yet."
            entities = self._entities(user_input)
            payload = {
                "response": sentence,
                "updated_narrative_state": {
                    "open_questions": [sentence],
                    "next_step": "Follow the user's lead.",
                    "mode_status": "developing",
                    "story_thread": first_clause[:120],
                    "entities_involved": entities,
                    "established": [first_clause],
                    "emotional_texture": "unclear",
                    "open_threads": [],
                    "unresolved": [],
                },
                "questions": [sentence],
            }
            return json.dumps(payload, indent=2, ensure_ascii=True)
        except Exception:
            return None

    @staticmethod
    def _extract_question(text: str) -> str | None:
        """Pull the first sentence that ends in '?' from a prose response."""
        if not text:
            return None
        # Look for question-shaped sentences. Cap at 200 chars to keep the
        # response tight.
        match = re.search(r"([^\n.!?]*\?)", text.strip())
        if not match:
            return None
        candidate = match.group(1).strip()
        if not candidate or len(candidate) > 200:
            return None
        return candidate

    def _fallback_response(self, text: str, entities: list[str]) -> str:
        """Anchor on a primer-known person when one is present; otherwise
        anchor on the first clause. Rotates through a small pool of openers
        seeded by ``hash(last transcript turn) XOR hour-of-day``.
        """
        style = load_operating_style(self.vault)
        emotion_naming = style.get("emotion-naming")  # False = forbid
        directness = style.get("directness") is True
        opener_style = style.get("opener-style")

        if entities:
            anchor = entities[0]
            return _rotate_opener(self.vault, anchor, kind="person",
                                  emotion_naming=emotion_naming,
                                  directness=directness,
                                  opener_style=opener_style)
        first = self._first_clause(text)
        if first and len(first) > 8:
            anchor = first.rstrip(".!?").lower()
            return _rotate_opener(self.vault, anchor, kind="clause",
                                  emotion_naming=emotion_naming,
                                  directness=directness,
                                  opener_style=opener_style)
        return "Say more."

    def _first_clause(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        for separator in [".", "!", "?", "\n"]:
            if separator in text:
                return text.split(separator, 1)[0].strip()
        return text[:160].strip()

    def _entities(self, text: str) -> list[str]:
        """Finding #9: restrict entity candidates to primer-known names plus
        full-name shapes that survive the stopword check. The previous
        regex-only implementation grabbed any capitalized word and reported
        days, adverbs, and interrogatives as entities.
        """
        primer_cast = _primer_known_names(self.vault)
        entities: list[str] = []
        # Prefer primer-known names first — those are the high-confidence
        # anchors and define the rotation pool's primary subject.
        for known in primer_cast:
            if name_in_text(known, text) and known not in entities:
                entities.append(known)
        # Fall back to multi-word capitalized phrases that aren't stopwords.
        for match in re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}\b", text):
            value = match.group(0)
            if value in entities:
                continue
            tokens = value.split()
            if any(t in SENTENCE_INITIAL_OR_TOOL_STOPWORDS for t in tokens):
                continue
            if any(t in DAY_STOPWORDS for t in tokens):
                continue
            # Months excluded only if not in primer cast.
            if any(t in MONTH_STOPWORDS and t not in primer_cast for t in tokens):
                continue
            entities.append(value)
        return entities[:6]


# ── Opener rotation (Finding #9) ──────────────────────────────────────────────

_PERSON_OPENERS_DEFAULT = [
    "What's on your mind with {anchor}?",
    "Say more about {anchor}.",
    "What's the part that's sticking with you about {anchor}?",
    "Walk me through what happened with {anchor}.",
]

_PERSON_OPENERS_DIRECT = [
    "{anchor} — what's the situation?",
    "Tell me what's going on with {anchor}.",
    "What's the action you're considering on {anchor}?",
]

_PERSON_OPENERS_MINIMAL = [
    "{anchor}?",
    "Tell me.",
    "Say more.",
]

_CLAUSE_OPENERS_DEFAULT = [
    "What changed when {anchor}?",
    "What was that like?",
    "Walk me through it.",
    "What's the part you want to think out loud about?",
]

_CLAUSE_OPENERS_DIRECT = [
    "Walk me through it.",
    "What's the next move?",
    "Tell me what matters most.",
]

_CLAUSE_OPENERS_MINIMAL = [
    "Tell me.",
    "Say more.",
    "Go on.",
]


def _rotate_opener(
    vault,
    anchor: str,
    kind: str,
    emotion_naming: bool | None = None,
    directness: bool = False,
    opener_style: str | None = None,
) -> str:
    """Pick an opener from the appropriate pool. Seeded by the hash of the
    last transcript turn XOR the current hour-of-day, so consecutive turns
    rarely get the same template but the choice stays reproducible for a
    given (turn, hour) pair.
    """
    if opener_style == "minimal":
        pool = _PERSON_OPENERS_MINIMAL if kind == "person" else _CLAUSE_OPENERS_MINIMAL
    elif directness:
        pool = _PERSON_OPENERS_DIRECT if kind == "person" else _CLAUSE_OPENERS_DIRECT
    else:
        pool = _PERSON_OPENERS_DEFAULT if kind == "person" else _CLAUSE_OPENERS_DEFAULT

    seed = _rotation_seed(vault)
    idx = seed % len(pool)
    template = pool[idx]
    # Some minimal openers don't reference the anchor.
    return template.format(anchor=anchor) if "{anchor}" in template else template


def _rotation_seed(vault) -> int:
    """Hash(last transcript turn) XOR hour-of-day. Stable per-turn within an
    hour, varies across the day if the user replays the same input later.
    """
    hour = datetime.now().hour
    last_turn = _read_last_transcript_line(vault)
    digest = hashlib.sha1(last_turn.encode("utf-8")).digest()
    # First 4 bytes as big-endian int.
    last_hash = int.from_bytes(digest[:4], "big")
    return last_hash ^ (hour * 2654435761)  # multiply by Knuth's golden ratio constant for spread


def _read_last_transcript_line(vault) -> str:
    """Best-effort: read the last non-empty line of today's transcript."""
    try:
        path = vault / "transcripts" / f"{datetime.now().strftime('%Y-%m-%d')}.md"
        if not path.exists():
            return ""
        # Read just the tail for performance on long transcripts.
        with path.open("r", encoding="utf-8") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            tail = fh.read()
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        return lines[-1] if lines else ""
    except Exception:
        return ""
