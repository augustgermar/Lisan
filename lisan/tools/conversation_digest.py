from __future__ import annotations

from datetime import date
from pathlib import Path

from .narrative_state import conversation_history, load_narrative_state, render_narrative_state
from ..paths import vault_root


def generate_conversation_digest(vault: Path | None = None, conversation_id: str | None = None) -> str:
    vault = vault or vault_root()
    state = load_narrative_state(vault, conversation_id)
    history = conversation_history(vault, conversation_id)
    lines = [
        "# Conversation Digest",
        "",
        f"conversation_id: {state.conversation_id}",
        f"generated: {date.today().isoformat()}",
        "",
        "## Narrative State",
        "",
        "```",
        render_narrative_state(state).rstrip(),
        "```",
        "",
        "## Recent Turns",
        "",
    ]
    if history:
        for turn in history[-12:]:
            lines.append(f"- {turn['speaker']}: {turn['text']}")
    else:
        lines.append("- No turns recorded.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_conversation_digest(vault: Path | None = None, conversation_id: str | None = None) -> Path:
    vault = vault or vault_root()
    out = vault / "reports" / f"conversation-{conversation_id or 'default'}-digest.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(generate_conversation_digest(vault, conversation_id), encoding="utf-8")
    return out

