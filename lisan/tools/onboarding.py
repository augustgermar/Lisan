from __future__ import annotations

import sys
from pathlib import Path

from ..paths import write_high_stakes_seed


# ── Helpers ───────────────────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()


def _c(text: str, *codes: str) -> str:
    if not _USE_COLOR:
        return text
    return "".join(codes) + text + "\033[0m"


BOLD  = "\033[1m"
DIM   = "\033[2m"
CYAN  = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"


def _ask(prompt: str, *, allow_blank: bool = False) -> str | None:
    """Prompt the user. Returns None on /skip or KeyboardInterrupt; empty string if blank allowed."""
    try:
        raw = input(_c(f"  {prompt} ", BOLD)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if raw.lower() in ("/skip", "skip"):
        return None
    if not raw and not allow_blank:
        return ""
    return raw


def _has_content(path: Path) -> bool:
    """True if the file contains any substantive (non-header, non-blank) lines."""
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("<!--"):
            return True
    return False


# ── Writer ────────────────────────────────────────────────────────────────────

def _write_identity(path: Path, name: str, background: str, values: str, relationships: str) -> None:
    lines = ["# Identity", ""]
    if name:
        lines += [f"You are {name}.", ""]
    lines += ["## Background", "", background or "_Not yet filled in._", ""]
    lines += ["## Values and Priorities", "", values or "_Not yet filled in._", ""]
    lines += ["## Relationships", "", relationships or "_Not yet filled in._", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_identity_core(path: Path, name: str) -> None:
    """Write the structured principal/assistant/deixis source-of-truth.

    Mirrors the YAML frontmatter parsed by ``primer_index.py`` (principal +
    assistant + deixis_frame). The given-name token becomes the principal alias
    that resolves to "you"; legacy ``identity.md`` remains the fallback when this
    file is absent, so existing vaults keep working.
    """
    safe_name = (name or "").replace('"', "").strip()
    given = safe_name.split()[0] if safe_name else ""
    who = given or "the principal"
    aliases = f'["{given}"]' if given else "[]"
    content = f'''---
principal:
  name: "{safe_name}"
  aliases: {aliases}
assistant:
  name: "Lisan"
  aliases: ["Lisan"]
deixis_frame: |
  I / me / Lisan = the assistant (software; no body, no family of its own).
  you / your     = {who}, the principal. Every stored record describes you.
  all other names = third parties; refer to them by name.
---

# Identity Core (invariant)

> Structured, machine-readable source of truth for **who is who**. The
> frontmatter above is authoritative: `primer_index.py` reads it to tell the
> principal (you) apart from third parties, and the deixis layer resolves
> `{{{{principal}}}}` / `{{{{self}}}}` against it at read time (`{{{{user}}}}` is a
> legacy synonym). Slow-changing; off-limits to automated self-rewrite.

## Principal

{f"You are **{safe_name}**" + (f" (also: {given})" if given else "") + " — the person Lisan serves." if safe_name else "_Principal not yet named. Edit the frontmatter above._"}

## Assistant

**Lisan** — your local personal assistant and memory system. Software; no body,
no family, no history of its own.
'''
    path.write_text(content, encoding="utf-8")


def _write_operating_style(path: Path, communication: str, working: str) -> None:
    # Structured preferences sit in JSON frontmatter so the fallback path can
    # read them deterministically; the free-text body is read by the LLM path.
    direct_lower = (communication or "").lower()
    directness = True if any(p in direct_lower for p in ("direct", "brief", "terse", "concise")) else None
    minimal = True if any(p in direct_lower for p in ("no preamble", "skip small", "minimal")) else False
    frontmatter = {
        "emotion-naming": None,
        "directness": directness,
        "opener-style": "minimal" if minimal else None,
        "summary-length": None,
    }
    body_lines = [
        "# Operating Style",
        "",
        "## Communication Style",
        "",
        communication or "_Not yet filled in._",
        "",
        "## Working Style",
        "",
        working or "_Not yet filled in._",
        "",
    ]
    from ..frontmatter import dump_markdown
    path.write_text(dump_markdown(frontmatter, "\n".join(body_lines)), encoding="utf-8")


def _write_high_stakes(path: Path) -> None:
    write_high_stakes_seed(path)


# ── Main flow ─────────────────────────────────────────────────────────────────

def needs_onboarding(vault: Path) -> bool:
    identity = vault / "primer" / "identity.md"
    operating = vault / "primer" / "operating-style.md"
    return not _has_content(identity) or not _has_content(operating)


def run_onboarding(vault: Path) -> bool:
    """Run the interactive onboarding Q&A. Returns True if completed, False if skipped."""
    identity_path = vault / "primer" / "identity.md"
    identity_core_path = vault / "primer" / "identity-core.md"
    operating_path = vault / "primer" / "operating-style.md"
    high_stakes_path = vault / "primer" / "high-stakes.yaml"

    print()
    print(_c("  Welcome to Lisan.", BOLD))
    print(_c("  A few quick questions to set up your memory vault.", DIM))
    print(_c("  Type /skip at any prompt to finish later and edit the files directly.", DIM))
    print(_c("  Press Enter to leave a field blank for now.", DIM))
    print()

    # ── Identity questions ────────────────────────────────────────────────────

    name = _ask("What's your name?", allow_blank=True)
    if name is None:
        _skip_message(vault)
        return False

    background = _ask(
        "In a sentence or two, describe your current situation\n"
        "  (work, life stage, where you're based — whatever feels relevant):",
        allow_blank=True,
    )
    if background is None:
        _skip_message(vault)
        return False

    values = _ask(
        "What are your top values or priorities right now?\n"
        "  (e.g. 'family, creative work, financial independence'):",
        allow_blank=True,
    )
    if values is None:
        _skip_message(vault)
        return False

    relationships = _ask(
        "Who are the most important people in your life?\n"
        "  (name and role, one per line or comma-separated — e.g. 'Sarah, partner'):",
        allow_blank=True,
    )
    if relationships is None:
        _skip_message(vault)
        return False

    # ── Operating style questions ─────────────────────────────────────────────

    communication = _ask(
        "How do you prefer Lisan to communicate with you?\n"
        "  (e.g. 'direct and brief', 'casual and warm', 'formal and precise'):",
        allow_blank=True,
    )
    if communication is None:
        _skip_message(vault)
        return False

    working = _ask(
        "Anything specific you want Lisan to always keep in mind\n"
        "  when working with you? (or press Enter to skip):",
        allow_blank=True,
    )
    if working is None:
        _skip_message(vault)
        return False

    # ── Write files ───────────────────────────────────────────────────────────

    _write_identity(
        identity_path,
        name=name or "",
        background=background or "",
        values=values or "",
        relationships=relationships or "",
    )
    _write_identity_core(identity_core_path, name=name or "")
    _write_operating_style(
        operating_path,
        communication=communication or "",
        working=working or "",
    )
    _write_high_stakes(high_stakes_path)

    print()
    print(_c("  ✓", GREEN) + _c(" Primer files written.", BOLD))
    print(_c(f"  You can edit them anytime at:", DIM))
    print(_c(f"    {identity_path}", DIM))
    print(_c(f"    {identity_core_path}", DIM))
    print(_c(f"    {operating_path}", DIM))
    print(_c(f"    {high_stakes_path}", DIM))
    print()
    return True


def _skip_message(vault: Path) -> None:
    identity_path = vault / "primer" / "identity.md"
    operating_path = vault / "primer" / "operating-style.md"
    print()
    print(_c("  Onboarding skipped. Edit these files to give Lisan context about you:", DIM))
    print(_c(f"    {identity_path}", DIM))
    print(_c(f"    {operating_path}", DIM))
    print()
