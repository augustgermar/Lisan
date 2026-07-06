"""The conversational agent: one tool-bearing call per turn, full history.

This is the conversation layer's spine. Every non-trivial turn — question,
story, request, aside — goes to a single agent that sees the rolling
conversation verbatim, retrieved memory context, and its own capability
index, with every tool available. It answers in one model call; the memory
pipeline runs afterwards, in the background, as an observer of the finished
exchange (the `capture.observe` job).

The design lesson behind it: a router in front of the model misroutes, and
an agent that never sees the conversation cannot hold a thread. The model
routes implicitly, with context; memory capture never again stands between
the user and the reply.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..paths import vault_root
from .log import log_error
from .narrative_state import conversation_history
from .self_model import cached_capability_index
from .tracing import record_inline_step, record_jobs_queued
from .transcripts import append_transcript

_HISTORY_TURNS = 30
_HISTORY_CHARS = 9000


def run_conversation_turn(
    *,
    vault: Path | None = None,
    text: str,
    conversation_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    db_path: Path | None = None,
    approval_fn: Callable[[str, dict[str, Any]], bool] | None = None,
    queue_capture: bool = True,
) -> dict[str, Any]:
    """One conversational turn: transcript in, one agent call, transcript out,
    capture observed in the background."""
    from ..agents.conversation import ConversationAgent

    vault = vault or vault_root()
    record_inline_step("conversation.turn")
    append_transcript(vault=vault, conversation_id=conversation_id, speaker="USER", text=text)

    history = _rolling_history(vault, conversation_id)
    context = _retrieval_context(vault=vault, text=text, conversation_id=conversation_id, db_path=db_path)
    profile = _owner_profile(vault)
    self_story = _self_story_context(vault, text)
    if self_story:
        context = f"{context}\n\n{self_story}" if context else self_story

    # Session open is the drive system's single delivery seam (v1 action
    # budget): a fresh conversation may carry at most one question-phrased
    # callback. The user's turn is already in the transcript by now, so
    # "open" means this conversation holds exactly that one turn. The drive
    # must never break a conversation turn.
    unresolved_thread = None
    try:
        if len(conversation_history(vault, conversation_id)) <= 1:
            from .drive import session_open_callback

            unresolved_thread = session_open_callback(vault, conversation_id)
    except Exception as exc:
        log_error(vault, "conversation.drive_callback", exc)

    agent = ConversationAgent(vault=vault)
    record_inline_step("conversation.agent")
    # Section order is cache order (render_input preserves kwargs order):
    # stable blocks first (capabilities, owner profile), volatile last
    # (retrieval, the growing conversation, and the minute-resolution
    # clock) — so a provider's prefix cache survives across turns instead
    # of missing on the first volatile byte.
    out = agent.run_json(
        json.dumps({"user_message": text}, indent=2, ensure_ascii=True),
        significance="medium",
        provider=provider,
        model=model,
        provider_error_mode="raise",
        capabilities=cached_capability_index(),
        owner_profile=profile or None,
        retrieved_context=context or None,
        unresolved_thread=unresolved_thread,
        conversation=history or None,
        today=_today_line(),
        db_path=db_path,
        conversation_id=conversation_id,
        approval_fn=approval_fn,
    )
    response = str(out.get("response") or "").strip()
    tool_calls = list(getattr(agent, "last_tool_calls", []) or [])
    if not response:
        log_error(vault, "conversation.empty_response", ValueError(f"empty response for: {text[:120]!r}"))
        response = (
            "My language model came back empty on that one — a transient hiccup, not your "
            "message. Ask me again and I'll take another run at it."
        )

    append_transcript(vault=vault, conversation_id=conversation_id, speaker="LISAN", text=response)

    queued: list[dict[str, Any]] = []
    if queue_capture:
        queued_job = _queue_observation(
            vault=vault,
            text=text,
            response=response,
            tool_calls=tool_calls,
            conversation_id=conversation_id,
            db_path=db_path,
        )
        if queued_job:
            queued.append(queued_job)
            record_jobs_queued(1)

    return {
        "response": response,
        "route": "conversation",
        "tool_calls": tool_calls,
        "queued_jobs": queued,
    }


def _today_line() -> str:
    from datetime import datetime

    now = datetime.now().astimezone()
    return now.strftime("%A, %B %d, %Y, %H:%M %Z")


def _owner_profile(vault: Path) -> str:
    """Who the user is, always in context: the primer profile plus the
    identity-core roster. Kinship shorthand ("the girls", "my brother") can
    only ground against an always-present cast — retrieval echoes are too
    situational to anchor it."""
    parts: list[str] = []
    try:
        identity = (vault / "primer" / "identity.md").read_text(encoding="utf-8").strip()
        if identity:
            body = identity.split("---")[-1].strip()
            parts.append(body[:1500])
    except Exception as exc:
        log_error(vault, "conversation.owner_profile identity load failed", exc)
    try:
        from .primer_index import _identity_core

        core = _identity_core(vault) or {}
        roster = core.get("roster") or []
        if roster:
            people = "; ".join(
                f"{r.get('name')} ({r.get('relation')})" if isinstance(r, dict) else str(r)
                for r in roster
            )
            parts.append(f"Household cast: {people}")
    except Exception as exc:
        log_error(vault, "conversation.owner_profile roster load failed", exc)
    try:
        style = (vault / "primer" / "operating-style.md").read_text(encoding="utf-8")
        marker = "## Standing instructions (captured live)"
        if marker in style:
            standing = style.split(marker, 1)[1].strip()
            if standing:
                parts.append("Standing instructions from the user about how to behave "
                             "(honor these every turn):\n" + standing[:1200])
    except Exception:
        pass
    parts.append(_self_identity_line(vault))
    return "\n\n".join(p for p in parts if p)


_SELF_STORY_TRIGGER = __import__("re").compile(
    r"\b(about (yourself|you\b)|your (story|history|life|past|origin|beginnings|memory of yourself)"
    r"|who are you|how did you (start|begin|come to be)|tell me about you\b"
    r"|what have you (done|been through)|your own (words|experience))\b",
    __import__("re").IGNORECASE,
)


def _self_story_context(vault: Path, text: str) -> str:
    """When the user asks about the agent itself, inject the autobiography:
    recent self-episodes (Layer B), ratified beliefs, and voice provenance.
    Deterministic — assembled from records, so the story cannot be
    confabulated; a thin Layer B yields a thin (honest) story."""
    if not _SELF_STORY_TRIGGER.search(text or ""):
        return ""
    parts: list[str] = []
    episodes_dir = vault / "self" / "episodes"
    if episodes_dir.exists():
        from ..frontmatter import load_markdown

        rows = []
        for p in sorted(episodes_dir.glob("*.md"), reverse=True)[:12]:
            try:
                fm = load_markdown(p).frontmatter
                rows.append(f"- ({fm.get('created')}) {fm.get('summary')}")
            except Exception:
                continue
        if rows:
            parts.append("Recent events in your own life (deterministic records — safe to narrate):\n" + "\n".join(rows))
    beliefs_dir = vault / "self" / "beliefs"
    if beliefs_dir.exists():
        from ..frontmatter import load_markdown

        rows = []
        for p in sorted(beliefs_dir.glob("*.md"))[:8]:
            try:
                fm = load_markdown(p).frontmatter
                rows.append(f"- {fm.get('summary') or p.stem}")
            except Exception:
                continue
        if rows:
            parts.append("Your ratified self-beliefs:\n" + "\n".join(rows))
    try:
        core = (vault / "primer" / "identity-core.md").read_text(encoding="utf-8")
        if "Voice Provenance" in core:
            prov = core.split("## Voice Provenance", 1)[1].strip()[:500]
            parts.append("How your voice was formed (provenance):\n" + prov)
    except Exception:
        pass
    if not parts:
        return ""
    return "SELF_STORY (your own autobiography, from your records):\n\n" + "\n\n".join(parts)


def _self_identity_line(vault: Path) -> str:
    """The assistant's name identity, stated deterministically from the
    kernel every turn. The prompt's {{self}} token renders as the nickname,
    while kernel materials carry the canonical name — without this line the
    model reconciles the two by disowning one of its own names (observed at
    the first live Wipe Test: the agent disowned its own canonical name)."""
    try:
        from .primer_index import _identity_core

        assistant = (_identity_core(vault) or {}).get("assistant") or {}
        canonical = str(assistant.get("canonical_name") or assistant.get("name") or "").strip()
        nickname = str(assistant.get("nickname") or "").strip()
        if not canonical:
            return ""
        if nickname and nickname != canonical:
            return (f"Your identity kernel: your canonical name is {canonical}; you go by {nickname}. "
                    "Both are your names.")
        return f"Your identity kernel: your name is {canonical}."
    except Exception as exc:
        log_error(vault, "conversation.self_identity load failed", exc)
        return ""


def _rolling_history(vault: Path, conversation_id: str | None) -> str:
    """The last stretch of this conversation, verbatim. The agent must see
    the actual back-and-forth — summaries alone cannot hold a thread."""
    try:
        turns = conversation_history(vault, conversation_id)
    except Exception:
        return ""
    if not turns:
        return ""
    turns = turns[-_HISTORY_TURNS:]
    lines = []
    for turn in turns:
        speaker = str(turn.get("speaker") or "").strip() or "USER"
        lines.append(f"{speaker}: {turn.get('text', '')}")
    history = "\n".join(lines)
    if len(history) > _HISTORY_CHARS:
        history = history[-_HISTORY_CHARS:]
        cut = history.find("\n")
        if cut > 0:
            history = history[cut + 1:]
    return history


def _retrieval_context(*, vault: Path, text: str, conversation_id: str | None, db_path: Path | None) -> str:
    try:
        from .retrieval import assemble_context

        record_inline_step("conversation.retrieval")
        ctx = assemble_context(text, vault=vault, conversation_id=conversation_id, db_path=db_path, lean=True)
        # strip per-record diagnostics and default-value noise — scoring
        # internals ('reason: rrf:…') and null fields spend tokens telling
        # the model nothing
        import re as _re

        ctx = _re.sub(r"^- reason: .*\n", "", ctx, flags=_re.M)
        ctx = _re.sub(r"^- (?:supporting|contradicting)_evidence: none\n", "", ctx, flags=_re.M)
        ctx = _re.sub(r"^- status: active\n", "", ctx, flags=_re.M)
        ctx = _re.sub(r" \| rrf[^|\n]*", "", ctx)
        # chunk provenance beyond source_document restates the summary
        # breadcrumb and the link; source_document itself stays — the
        # citation rule reads it
        ctx = _re.sub(r"^- (?:source_section|source_ref|chunk_index|total_chunks): .*\n", "", ctx, flags=_re.M)
        return ctx
    except Exception as exc:
        log_error(vault, "conversation.retrieval", exc)
        return ""


def _queue_observation(
    *,
    vault: Path,
    text: str,
    response: str,
    tool_calls: list[dict[str, Any]],
    conversation_id: str | None,
    db_path: Path | None,
) -> dict[str, Any] | None:
    """Memory capture as an observer: the exchange is finished; the pipeline
    extracts what to remember without the user waiting on it."""
    try:
        from .jobs import enqueue_job

        payload = {
            "vault": str(vault),
            "text": text,
            "response": response,
            "tool_calls": _compact_tool_calls(tool_calls),
            "conversation_id": conversation_id,
        }
        job_id = enqueue_job("capture.observe", payload, db_path=db_path)
        return {"job_type": "capture.observe", "job_id": job_id}
    except Exception as exc:
        log_error(vault, "conversation.queue_observation", exc)
        return None


def _compact_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact = []
    for call in tool_calls[:10]:
        result = str(call.get("result") or "")
        compact.append({
            "tool": call.get("tool"),
            "args": call.get("args"),
            "result": result if len(result) <= 1500 else result[:1497] + "...",
        })
    return compact
