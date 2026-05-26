from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from ..frontmatter import write_markdown
from ..paths import vault_root


# Finding 4: the capture pipeline writes the user's turn to the transcript
# BEFORE the writer runs. A timeout / crash leaves an orphaned entry, and the
# retry then appends a second copy. We guard with a content-equality check on
# the most recent transcript turns: same speaker, same text, same conversation
# id, and within a short look-back window.
_DEDUP_WINDOW_MINUTES = 60


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
    if _is_recent_duplicate(
        existing=existing,
        conversation_id=conversation_id,
        speaker=speaker,
        text=text,
        now=timestamp,
    ):
        return path
    heading = f"## Conversation — {time_str}" + (f" [{conversation_id}]" if conversation_id else "")
    entry = f"\n{heading}\n\n{speaker}: {text}\n"
    if existing.strip():
        path.write_text(existing.rstrip() + entry, encoding="utf-8")
    else:
        path.write_text(existing + entry, encoding="utf-8")
    return path


# ── Dedup helpers ────────────────────────────────────────────────────────────

_BLOCK_RE = re.compile(
    r"\n## Conversation — (?P<time>\d{2}:\d{2})(?: \[(?P<conv>[^\]]+)\])?\n\n(?P<body>.*?)(?=\n## Conversation — |\Z)",
    re.DOTALL,
)


def _is_recent_duplicate(
    *,
    existing: str,
    conversation_id: str | None,
    speaker: str,
    text: str,
    now: datetime,
) -> bool:
    """Return True if the most recent matching turn is a duplicate of this one.

    "Matching" means: same conversation id, same speaker, identical text after
    whitespace normalization, written within the dedup window. We only look at
    the *last* turn of the same speaker in the same conversation — if the user
    legitimately repeats themselves later in a long session it should still
    capture; we're only catching back-to-back retries from a crash.
    """
    if not text.strip():
        return False
    normalized_new = " ".join(text.split())
    cutoff = now - timedelta(minutes=_DEDUP_WINDOW_MINUTES)
    today_date = now.date().isoformat()

    most_recent: tuple[datetime, str, str] | None = None
    for match in _BLOCK_RE.finditer("\n" + existing):
        block_conv = match.group("conv") or ""
        if (conversation_id or "") != block_conv:
            continue
        try:
            block_time = datetime.strptime(
                f"{today_date} {match.group('time')}", "%Y-%m-%d %H:%M"
            )
        except ValueError:
            continue
        for line in match.group("body").strip().splitlines():
            line = line.strip()
            if not line:
                continue
            head, sep, msg = line.partition(":")
            if not sep:
                continue
            block_speaker = head.strip()
            if block_speaker != speaker:
                continue
            most_recent = (block_time, block_speaker, " ".join(msg.split()))
    if most_recent is None:
        return False
    block_time, _, block_text = most_recent
    if block_time < cutoff:
        return False
    return block_text == normalized_new
