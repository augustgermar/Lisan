"""Tell the owner, in a line or two, when the agent learns something on its own.

Learning that the owner cannot see is learning they cannot trust or correct.
Four things count: a skill the loop wrote or changed, a skill it promoted or
flagged, a belief the dreamer revised on evidence, and a new ache (a deviation
the scanner raised about the agent itself). Each message is deliberately short;
the full record is in the learning artifacts and `lisan skills history`.

Best effort by construction: a failed notification must never undo or block the
learning it reports. Only the resident vault can page the owner (the check lives
in escalation._notify_owner, which also honours LISAN_NO_OUTBOUND), so tests and
scratch vaults stay silent. Turn it off with ``learning.notify: false``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_LINE = 150
MAX_LINES = 4


def enabled(config: dict[str, Any] | None) -> bool:
    return (config or {}).get("learning", {}).get("notify", True) is not False


def _line(text: Any) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= MAX_LINE else one[: MAX_LINE - 1].rstrip() + "…"


def send(vault: Path, title: str, lines: list[str], *, config: dict[str, Any] | None = None) -> bool:
    """One message: a title and up to MAX_LINES bullets (the rest counted)."""
    lines = [_line(x) for x in lines if x]
    if not lines:
        return False
    try:
        if config is None:
            from ..config import load_config

            config = load_config()
        if not enabled(config):
            return False
        shown = [f"• {x}" for x in lines[:MAX_LINES]]
        if len(lines) > MAX_LINES:
            shown.append(f"…and {len(lines) - MAX_LINES} more")
        from .escalation import _notify_owner

        return bool(_notify_owner(f"{title}\n" + "\n".join(shown), chat_id=None, vault=vault))
    except Exception as exc:  # reporting must never break learning
        try:
            from .log import log_error

            log_error(vault, "learning_notice", exc)
        except Exception:
            pass
        return False


def skills_learned(vault: Path, review_id: str, verdicts: list[Any], lifecycle: list[dict[str, Any]], *, config=None) -> bool:
    lines: list[str] = []
    for v in verdicts:
        applied = getattr(v, "applied", None)
        if applied is None:
            continue
        verb = {"create": "new skill", "patch": "updated skill", "edit": "updated skill"}.get(applied.op, f"{applied.op} skill")
        why = getattr(getattr(v, "op", None), "rationale", "") or ""
        lines.append(f"{verb} {applied.skill}: {why}" if why else f"{verb} {applied.skill}")
    for change in lifecycle or []:
        verb = "now trusts" if change.get("to") == "established" else "flagged"
        lines.append(f"{verb} its skill {change.get('skill')}: {change.get('reason', '')}")
    return send(vault, "🧠 I learned something", lines, config=config)


def beliefs_revised(vault: Path, statements: list[str], *, config=None) -> bool:
    return send(vault, "🪞 I revised what I believe about myself", statements, config=config)


def aches(vault: Path, found: list[tuple[str, str]], *, config=None) -> bool:
    return send(vault, "🩺 I noticed something off", [f"{klass}: {summary}" for klass, summary in found], config=config)
