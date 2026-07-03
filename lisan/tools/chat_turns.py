from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_IDENTITY_PATTERNS = (
    r"\bwhat(?:'s| is)? your name\b",
    r"\bwho are you\b",
    r"\bwhat are you\b(?:\?|$|\.|!|\s*$)",
    r"\bwhat(?:'s| is) this\b",
)

_HELP_PATTERNS = (
    r"\bhelp\b",
    r"\bwhat can you do\b",
    r"\bhow do i use you\b",
    r"\bwhat do you do\b",
)

_STATUS_PATTERNS = (
    r"\bwhat(?:'s| is) up\b",
    r"\bwhat(?:'s| is) going on\b",
    r"\bwhat are you up to\b",
    r"\bwhat are you doing\b",
    r"\bhow are you\b",
)

_GREETING_PATTERNS = (
    r"^(hi|hello|hey|yo|hiya)$",
    r"^(thanks|thank you|thx|ty)$",
    r"^(ok|okay|cool|nice|great|good|yep|yup|sure|alright)$",
)

_MEMORY_REQUEST_PATTERNS = (
    r"\bremember this\b",
    r"\bsave this\b",
    r"\bnote that\b",
    r"\bcorrection to prior memory\b",
    r"\bfact correction\b",
)

_COMMAND_PREFIXES = ("/",)


@dataclass(slots=True)
class TurnClassification:
    label: str
    route: str
    fast_path_used: bool
    deterministic_response: str | None
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "route": self.route,
            "fast_path_used": self.fast_path_used,
            "deterministic_response": self.deterministic_response,
            "reason": self.reason,
        }


def classify_turn(
    text: str,
    vault: Path | None = None,
    conversation_id: str | None = None,
) -> TurnClassification:
    stripped = text.strip()
    lowered = _normalize(stripped)
    # Mid-conversation, short turns are the MOST context-dependent ("you
    # pick", "go ahead", "the first one") — a canned reply there drops the
    # thread on the floor. Canned fast paths are for fresh conversations
    # only; once a conversation is underway, everything goes to the
    # context-bearing pipeline.
    in_conversation = _conversation_underway(vault, conversation_id)

    if not stripped:
        return TurnClassification(
            label="skip",
            route="skip",
            fast_path_used=True,
            deterministic_response=None,
            reason="empty turn",
        )

    if _is_command(stripped):
        if lowered.startswith("/remember") or lowered.startswith("/forget"):
            return TurnClassification(
                label="memory",
                route="memory",
                fast_path_used=False,
                deterministic_response=None,
                reason="memory prefix",
            )
        return TurnClassification(
            label="skip",
            route="skip",
            fast_path_used=True,
            deterministic_response=None,
            reason="command input",
        )

    if _matches_any(lowered, _MEMORY_REQUEST_PATTERNS):
        return TurnClassification(
            label="memory",
            route="memory",
            fast_path_used=False,
            deterministic_response=None,
            reason="explicit memory request",
        )

    if _matches_any(lowered, _IDENTITY_PATTERNS):
        return TurnClassification(
            label="identity",
            route="advice",
            fast_path_used=True,
            deterministic_response=_identity_response(vault),
            reason="assistant identity question",
        )

    if _matches_any(lowered, _HELP_PATTERNS):
        return TurnClassification(
            label="help",
            route="advice",
            fast_path_used=True,
            deterministic_response=_help_response(),
            reason="help request",
        )

    if _matches_any(lowered, _STATUS_PATTERNS) and not in_conversation:
        return TurnClassification(
            label="status",
            route="advice",
            fast_path_used=True,
            deterministic_response=_status_response(),
            reason="casual status check",
        )

    if (_matches_any(lowered, _GREETING_PATTERNS) or _is_short_acknowledgment(lowered)) and not in_conversation:
        return TurnClassification(
            label="ack",
            route="advice",
            fast_path_used=True,
            deterministic_response=_ack_response(lowered),
            reason="short acknowledgment",
        )

    if _looks_like_memory_story(stripped, lowered):
        return TurnClassification(
            label="memory",
            route="memory",
            fast_path_used=False,
            deterministic_response=None,
            reason="personal narrative",
        )

    if _looks_like_practical_question(stripped, lowered):
        return TurnClassification(
            label="advice",
            route="advice",
            fast_path_used=False,
            deterministic_response=None,
            reason="practical question",
        )

    if len(stripped.split()) <= 6 and not in_conversation:
        return TurnClassification(
            label="smalltalk",
            route="advice",
            fast_path_used=True,
            deterministic_response=_smalltalk_response(lowered),
            reason="casual small talk",
        )

    return TurnClassification(
        label="memory",
        route="memory",
        fast_path_used=False,
        deterministic_response=None,
        reason="default to memory for substantive turn",
    )


def _conversation_underway(vault: Path | None, conversation_id: str | None) -> bool:
    """True once this conversation has any prior USER turn in today's
    transcript. Non-fatal: an unreadable transcript means fresh."""
    if not conversation_id or vault is None:
        return False
    try:
        from .memory_pipeline import _conversation_turn_count

        return _conversation_turn_count(vault, conversation_id) > 0
    except Exception:
        return False


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = text.replace("whats", "what's")
    return text


def _is_command(text: str) -> bool:
    return text.startswith(_COMMAND_PREFIXES)


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _is_short_acknowledgment(text: str) -> bool:
    tokens = [token for token in re.findall(r"[a-z']+", text) if token]
    if not tokens:
        return False
    return len(tokens) <= 3 and tokens[0] in {"ok", "okay", "cool", "nice", "great", "good", "thanks", "thank", "thx", "ty", "yep", "yup", "sure", "alright"}


def _looks_like_memory_story(text: str, lowered: str) -> bool:
    personal_markers = [
        "i ",
        "i'm",
        "i was",
        "i had",
        "i decided",
        "i feel",
        "i felt",
        "my ",
        "me ",
        "we ",
        "family",
        "work",
        "relationship",
        "promotion",
        "breakup",
        "doctor",
        "boss",
        "manager",
    ]
    if any(marker in lowered for marker in personal_markers):
        if any(term in lowered for term in ["remember this", "save this", "note that"]):
            return False
        if "?" not in lowered or len(text.split()) > 8:
            return True
    return False


def _looks_like_practical_question(text: str, lowered: str) -> bool:
    if "?" not in text and not lowered.startswith(("should ", "can ", "could ", "would ", "is ", "are ", "do ", "does ", "what ", "how ", "when ", "where ", "why ")):
        return False
    practical_markers = [
        "recommend",
        "review",
        "edit",
        "recipe",
        "help",
        "what if",
        "can i",
        "should i",
        "could i",
        "what do you think",
        "best way",
        "how do i",
    ]
    return any(marker in lowered for marker in practical_markers) or lowered.endswith("?")


def _identity_response(vault: Path | None = None) -> str:
    from .primer_index import assistant_name as _asst_name
    name = _asst_name(vault) if vault else "Lisan"
    return f"My name is {name}. I am your local personal assistant and memory system."


def _help_response() -> str:
    return "I can chat, remember things, show logs, and run maintenance. Use /help for the command list."


def _status_response() -> str:
    return "I am here and ready to help."


def _ack_response(lowered: str) -> str:
    if lowered in {"hi", "hello", "hey", "yo", "hiya"}:
        return "Hi. I'm Lisan."
    if "thank" in lowered or "thx" in lowered or "ty" in lowered:
        return "You're welcome."
    if lowered in {"ok", "okay", "cool", "nice", "great", "good", "yep", "yup", "sure", "alright"}:
        return "Yep."
    return "Got it."


def _smalltalk_response(lowered: str) -> str:
    if "what are you up to" in lowered or "what are you doing" in lowered or "what's up" in lowered:
        return "I am here and keeping things moving."
    if "how are you" in lowered:
        return "Doing fine. Ready when you are."
    return "I am here and ready to help."
