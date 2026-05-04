from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..frontmatter import write_markdown
from ..paths import vault_root


def append_transcript(
    vault: Path | None = None,
    conversation_id: str | None = None,
    speaker: str = "USER",
    text: str = "",
    timestamp: datetime | None = None,
) -> Path:
    vault = vault or vault_root()
    timestamp = timestamp or datetime.now()
    date_str = timestamp.date().isoformat()
    time_str = timestamp.strftime("%H:%M")
    path = vault / "transcripts" / f"{date_str}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        write_markdown(path, {"date": date_str}, "")

    existing = path.read_text(encoding="utf-8")
    heading = f"## Conversation — {time_str}" + (f" [{conversation_id}]" if conversation_id else "")
    entry = f"\n{heading}\n\n{speaker}: {text}\n"
    if existing.strip():
        path.write_text(existing.rstrip() + entry, encoding="utf-8")
    else:
        path.write_text(existing + entry, encoding="utf-8")
    return path

