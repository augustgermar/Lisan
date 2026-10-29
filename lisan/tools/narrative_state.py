from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import vault_root
from ..utils import slugify, today_iso


@dataclass(slots=True)
class NarrativeState:
    conversation_id: str
    story_thread: str
    entities_involved: list[str]
    established: list[str]
    emotional_texture: str
    open_threads: list[str]
    unresolved: list[str]
    natural_next: str
    mode_status: str
    turn_count: int
    last_user_text: str
    last_agent_response: str
    updated: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "story_thread": self.story_thread,
            "entities_involved": self.entities_involved,
            "established": self.established,
            "emotional_texture": self.emotional_texture,
            "open_threads": self.open_threads,
            "unresolved": self.unresolved,
            "natural_next": self.natural_next,
            "mode_status": self.mode_status,
            "turn_count": self.turn_count,
            "last_user_text": self.last_user_text,
            "last_agent_response": self.last_agent_response,
            "updated": self.updated,
        }


def narrative_state_path(vault: Path | None, conversation_id: str | None) -> Path:
    vault = vault or vault_root()
    safe = slugify(conversation_id or "default")
    return vault / "transcripts" / "narrative" / f"{safe}.json"


def load_narrative_state(vault: Path | None = None, conversation_id: str | None = None) -> NarrativeState:
    path = narrative_state_path(vault, conversation_id)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = {}
    conv_id = str(payload.get("conversation_id") or conversation_id or "default")
    return NarrativeState(
        conversation_id=conv_id,
        story_thread=str(payload.get("story_thread") or ""),
        entities_involved=[str(item) for item in payload.get("entities_involved", []) if str(item)],
        established=[str(item) for item in payload.get("established", []) if str(item)],
        emotional_texture=str(payload.get("emotional_texture") or ""),
        open_threads=[str(item) for item in payload.get("open_threads", []) if str(item)],
        unresolved=[str(item) for item in payload.get("unresolved", []) if str(item)],
        natural_next=str(payload.get("natural_next") or ""),
        mode_status=str(payload.get("mode_status") or "seed"),
        turn_count=int(payload.get("turn_count") or 0),
        last_user_text=str(payload.get("last_user_text") or ""),
        last_agent_response=str(payload.get("last_agent_response") or ""),
        updated=str(payload.get("updated") or today_iso()),
    )


def save_narrative_state(vault: Path | None, state: NarrativeState) -> Path:
    path = narrative_state_path(vault, state.conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.as_dict(), indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return path


def reset_narrative_state(vault: Path | None = None, conversation_id: str | None = None) -> Path:
    path = narrative_state_path(vault, conversation_id)
    if path.exists():
        path.unlink()
    return path


def render_narrative_state(state: NarrativeState) -> str:
    lines = [
        f"conversation_id: {state.conversation_id}",
        f"story_thread: {state.story_thread}",
        f"entities_involved: {', '.join(state.entities_involved) if state.entities_involved else 'none'}",
        f"established: {', '.join(state.established) if state.established else 'none'}",
        f"emotional_texture: {state.emotional_texture}",
        f"open_threads: {', '.join(state.open_threads) if state.open_threads else 'none'}",
        f"unresolved: {', '.join(state.unresolved) if state.unresolved else 'none'}",
        f"natural_next: {state.natural_next}",
        f"mode_status: {state.mode_status}",
        f"turn_count: {state.turn_count}",
        f"last_user_text: {state.last_user_text}",
        f"last_agent_response: {state.last_agent_response}",
        f"updated: {state.updated}",
    ]
    return "\n".join(lines) + "\n"


def update_narrative_state(
    previous: NarrativeState,
    user_text: str,
    elicitor_output: dict[str, Any],
) -> NarrativeState:
    updated_state = elicitor_output.get("updated_narrative_state", {})
    if not isinstance(updated_state, dict):
        updated_state = {}
    open_questions = _string_list(updated_state.get("open_questions"))
    established = _merge_lists(previous.established, _string_list(updated_state.get("established")))
    open_threads = _merge_lists(previous.open_threads, _string_list(updated_state.get("open_threads")))
    unresolved = _merge_lists(previous.unresolved, _string_list(updated_state.get("unresolved")))
    entities = _merge_lists(previous.entities_involved, _string_list(updated_state.get("entities_involved")))
    story_thread = str(updated_state.get("story_thread") or previous.story_thread or _infer_story_thread(user_text))
    emotional_texture = str(updated_state.get("emotional_texture") or previous.emotional_texture or _infer_emotional_texture(user_text))
    natural_next = str(updated_state.get("next_step") or previous.natural_next or _infer_next_step(open_questions))
    mode_status = str(updated_state.get("mode_status") or previous.mode_status or _infer_mode_status(user_text, open_questions))
    return NarrativeState(
        conversation_id=previous.conversation_id,
        story_thread=story_thread,
        entities_involved=entities,
        established=established or [_first_clause(user_text)],
        emotional_texture=emotional_texture,
        open_threads=open_threads,
        unresolved=unresolved,
        natural_next=natural_next,
        mode_status=mode_status,
        turn_count=previous.turn_count + 1,
        last_user_text=user_text.strip(),
        last_agent_response=str(elicitor_output.get("response") or previous.last_agent_response),
        updated=today_iso(),
    )


# (path, conversation) -> (mtime_ns, size, parsed turns). A chat turn asked
# for the full history three or more times, and each call read and parsed
# every transcript file the vault has ever written; past days never change,
# so only a file whose stat changed is read again. Cached turns carry their
# timestamp ("at") for session logic; conversation_history strips it.
_TRANSCRIPT_CACHE: dict[tuple[str, str], tuple[int, int, list[dict[str, str]]]] = {}


def _timed_turns(vault: Path, conv: str) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for path in sorted((vault / "transcripts").glob("*.md")):
        try:
            stat = path.stat()
        except OSError:
            continue
        key = (str(path), conv)
        cached = _TRANSCRIPT_CACHE.get(key)
        if cached is None or cached[0] != stat.st_mtime_ns or cached[1] != stat.st_size:
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            parsed = _parse_transcript(text, conv, day=path.stem)
            cached = (stat.st_mtime_ns, stat.st_size, parsed)
            _TRANSCRIPT_CACHE[key] = cached
        turns.extend(cached[2])
    return turns


def conversation_history(vault: Path | None = None, conversation_id: str | None = None) -> list[dict[str, str]]:
    vault = vault or vault_root()
    conv = conversation_id or "default"
    return [{"speaker": t["speaker"], "text": t["text"]} for t in _timed_turns(vault, conv)]


SESSION_GAP_HOURS = 4


def conversation_session(
    vault: Path | None = None,
    conversation_id: str | None = None,
    *,
    gap_hours: float = SESSION_GAP_HOURS,
) -> list[dict[str, str]]:
    """The turns of the current sitting: the trailing run of turns with no
    silence longer than ``gap_hours`` between them, across day boundaries.

    "Recent", "fresh", and "turn N" used to mean "in today's transcript
    file", so a conversation that crossed midnight suddenly looked brand new
    (the recent-activity preamble was injected mid-thread and the turn
    counter reset), while a persistent id like a Telegram chat looked
    continuous all day regardless of gaps."""
    from datetime import datetime, timedelta

    vault = vault or vault_root()
    turns = _timed_turns(vault, conversation_id or "default")
    session: list[dict[str, str]] = []
    later: datetime | None = None
    for turn in reversed(turns):
        try:
            at = datetime.fromisoformat(turn.get("at") or "")
        except ValueError:
            at = None
        if later is not None and at is not None and later - at > timedelta(hours=gap_hours):
            break
        session.append(dict(turn))
        if at is not None:
            later = at
    session.reverse()
    return session


def format_history(history: list[dict[str, str]]) -> str:
    if not history:
        return "No prior turns recorded."
    lines: list[str] = []
    for turn in history[-12:]:
        lines.append(f"{turn['speaker']}: {turn['text']}")
    return "\n".join(lines)


def _parse_transcript(text: str, conversation_id: str, day: str | None = None) -> list[dict[str, str]]:
    """Parse transcript headings into complete conversation turns.

    A turn body is allowed to contain paragraphs, bullets, and colons.  The
    old parser treated every physical line as a new ``SPEAKER: message``
    record, which silently discarded all continuation lines that did not look
    like a speaker label.  That made facts in otherwise healthy transcripts
    unavailable to the rolling conversation context.
    """
    turns: list[dict[str, str]] = []
    blocks = re.split(r"\n## Conversation — ", text)
    for block in blocks:
        if not block.strip():
            continue
        header, _, rest = block.partition("\n\n")
        if f"[{conversation_id}]" not in header and conversation_id != "default":
            continue
        if conversation_id == "default" and "[" in header and "]" in header:
            continue
        body = rest.strip()
        if not body:
            continue

        # append_transcript writes one speaker label at the beginning of the
        # body.  Everything after that label belongs to the same turn until
        # the next conversation heading.
        first_line, separator, remainder = body.partition("\n")
        speaker, sep, msg = first_line.partition(":")
        speaker = speaker.strip()
        if not sep or not speaker or " " in speaker:
            continue
        message_parts = [msg.strip()]
        if separator and remainder.strip():
            message_parts.append(remainder.strip())
        turn = {"speaker": speaker, "text": "\n".join(message_parts).strip()}
        if day:
            time_match = re.match(r"\s*(\d{2}:\d{2})", header)
            if time_match:
                turn["at"] = f"{day}T{time_match.group(1)}"
        turns.append(turn)
    return turns


def _merge_lists(existing: list[str], incoming: list[str]) -> list[str]:
    merged: list[str] = []
    for item in [*existing, *incoming]:
        value = item.strip()
        if value and value not in merged:
            merged.append(value)
    return merged[:12]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _first_clause(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    for separator in [".", "!", "?", "\n"]:
        if separator in text:
            return text.split(separator, 1)[0].strip()
    return text[:160].strip()


def _infer_story_thread(text: str) -> str:
    clause = _first_clause(text)
    return clause[:120] or "New story thread"


def _infer_emotional_texture(text: str) -> str:
    lowered = text.lower()
    if any(term in lowered for term in ["excited", "happy", "relieved", "proud"]):
        return "positive"
    if any(term in lowered for term in ["angry", "frustrated", "sad", "upset", "anxious"]):
        return "tense"
    return "unclear"


def _infer_next_step(open_questions: list[str]) -> str:
    if open_questions:
        return open_questions[0]
    return "Follow the user's lead."


def _infer_mode_status(text: str, open_questions: list[str]) -> str:
    lowered = text.lower()
    if any(term in lowered for term in ["anyway", "moving on", "next topic", "change topic"]):
        return "closed"
    if open_questions:
        return "developing"
    return "seed"
