"""Adopt a real (non-template) intent.md in a test vault.

Chat-side execution fails closed without an adopted intent (2026-10-29), so
tests that exercise codex runs, delegation, or plan steps must say, explicitly,
that the owner has adopted standing rules. The template's own delegations are
used unchanged; only the sentinel dates are replaced.
"""
from __future__ import annotations

from pathlib import Path


def adopt_intent(vault: Path, *, today: str = "2026-07-26") -> Path:
    from lisan.tools.intent import default_intent_document, intent_path

    path = intent_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_intent_document(today=today), encoding="utf-8")
    return path
