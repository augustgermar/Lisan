from __future__ import annotations

import re
import sys
from pathlib import Path

from ..config import load_config
from ..frontmatter import dump_markdown, write_markdown
from ..paths import write_high_stakes_seed
from ..utils import slugify, today_iso
from .agent_namer import AgentIdentity, generate_agent_identity
from .primer_index import assistant_display_name, assistant_hash, assistant_nickname, assistant_name, assistant_seed, principal_name
from .term import color, BOLD, DIM, CYAN, GREEN, YELLOW


# ── Helpers ───────────────────────────────────────────────────────────────────







def _ask(prompt: str, *, allow_blank: bool = False) -> str | None:
    """Prompt the user. Returns None on /skip or KeyboardInterrupt; empty string if blank allowed."""
    try:
        raw = input(color(f"  {prompt} ", BOLD)).strip()
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


def _yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _flow_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{_yaml_escape(value)}"' for value in values) + "]"


def _principal_aliases(name: str) -> list[str]:
    first = (name or "").strip().split()
    if not first:
        return []
    return [first[0]]


def _rewrite_to_third_person(text: str, principal: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    name = principal.strip() or "The principal"
    substitutions = [
        (r"\b[Ii]'m\b", f"{name} is"),
        (r"\b[Ii] am\b", f"{name} is"),
        (r"\b[Ii] was\b", f"{name} was"),
        (r"\b[Ii]'ve\b", f"{name} has"),
        (r"\b[Ii] have\b", f"{name} has"),
        (r"\b[Ii]'d\b", f"{name} would"),
        (r"\bmyself\b", "the principal"),
        (r"\bmy\b", "the principal's"),
        (r"\bme\b", "the principal"),
        (r"\b[Ii]\b", name),
    ]
    for pattern, replacement in substitutions:
        text = re.sub(pattern, replacement, text)
    return text


def _identity_core_text(
    *,
    principal: str,
    assistant: AgentIdentity,
    nickname: str | None,
) -> str:
    principal = principal.strip() or "the principal"
    principal_aliases = _principal_aliases(principal)
    assistant_display = nickname.strip() if nickname and nickname.strip() else assistant.name
    assistant_aliases = [assistant.name]
    if assistant_display and assistant_display != assistant.name:
        assistant_aliases.append(assistant_display)
    nickname_line = f'"{_yaml_escape(nickname.strip())}"' if nickname and nickname.strip() else "null"
    content = f'''---
principal:
  name: "{_yaml_escape(principal)}"
  aliases: {_flow_list(principal_aliases)}
assistant:
  name: "{_yaml_escape(assistant.name)}"
  canonical_name: "{_yaml_escape(assistant.name)}"
  nickname: {nickname_line}
  software: "Lisan"
  hash: "{assistant.sha256}"
  seed: "{assistant.seed}"
  aliases: {_flow_list(assistant_aliases)}
deixis_frame: |
  I / me / {assistant_display} = the assistant (software; no body, no family of its own).
  you / your = {principal}, the principal. Every stored record describes you.
  all other names = third parties; refer to them by name.
roster: []
---

# Identity Core (invariant)

> Structured, machine-readable source of truth for **who is who**. The
> frontmatter above is authoritative. Slow-changing; off-limits to automated self-rewrite.

## Principal

The principal is **{principal}**.

## Assistant

**{assistant_display}** — a Lisan personal assistant and memory system. Software; no body,
no family, no history of its own.
'''
    return content


def _write_identity(path: Path, name: str, background: str, values: str, relationships: str) -> None:
    principal = name.strip() if name else ""
    body_background = _rewrite_to_third_person(background, principal) if background.strip() else "_Not yet shared. Will be learned from conversation over time._"
    body_values = _rewrite_to_third_person(values, principal) if values.strip() else "_Not yet shared. Will be learned from conversation over time._"
    body_relationships = _rewrite_to_third_person(relationships, principal) if relationships.strip() else "_Not yet shared. Will be populated as people are mentioned._"
    lines = [
        "# About the Principal",
        "",
        f"The principal is {principal}." if principal else "The principal has not been named yet.",
        "",
        "## Background",
        "",
        body_background,
        "",
        "## Values and Priorities",
        "",
        body_values,
        "",
        "## Key Relationships",
        "",
        body_relationships,
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_identity_core(
    path: Path,
    name: str,
    *,
    agent_identity: AgentIdentity | None = None,
    nickname: str | None = None,
) -> None:
    """Write the structured principal/assistant/deixis source-of-truth."""
    safe_name = (name or "").replace('"', "").strip()
    if agent_identity is None:
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
> `{{principal}}` / `{{self}}` against it at read time (`{{user}}` is a
> legacy synonym). Slow-changing; off-limits to automated self-rewrite.

## Principal

{f"You are **{safe_name}**" + (f" (also: {given})" if given else "") + " — the person Lisan serves." if safe_name else "_Principal not yet named. Edit the frontmatter above._"}

## Assistant

**Lisan** — your local personal assistant and memory system. Software; no body,
no family, no history of its own.
'''
        _ceremony_write_kernel(path, content)
        return

    content = _identity_core_text(
        principal=safe_name,
        assistant=agent_identity,
        nickname=nickname,
    )
    _ceremony_write_kernel(path, content)


def _ceremony_write_kernel(path: Path, content: str) -> None:
    """Bootstrap is the founding ceremony: the only in-process path allowed
    to create or rewrite the identity kernel, and it stamps the content
    hash so drift detection is armed from birth."""
    from .kernel import ceremony, stamp_kernel_hash

    with ceremony():
        path.write_text(content, encoding="utf-8")
        stamp_kernel_hash(path.parent.parent)


def _write_operating_style(path: Path, communication: str = "", working: str = "") -> None:
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
        communication or "Direct, concise, and factual.",
        "",
        "## Working Style",
        "",
        working or "Prefer precision over recall when uncertain; ask before taking high-consequence actions.",
        "",
    ]
    path.write_text(dump_markdown(frontmatter, "\n".join(body_lines)), encoding="utf-8")


def _write_high_stakes(path: Path) -> None:
    write_high_stakes_seed(path)


def _write_self_entity(
    vault: Path,
    *,
    agent_identity: AgentIdentity,
    principal: str,
    nickname: str | None,
) -> Path:
    display_name = (nickname or agent_identity.name).strip() or agent_identity.name
    slug = slugify(display_name)
    path = vault / "entities" / "agents" / f"{slug}.md"
    today = today_iso()
    aliases = [agent_identity.name]
    if display_name != agent_identity.name:
        aliases.append(display_name)
    frontmatter = {
        "id": f"entity.agent.{slug}",
        "type": "entity",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": "low",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "public",
        "summary": f"{display_name} is a freshly initialized Lisan instance serving {principal}.",
        "links": [],
        "confidence": "low",
        "confidence_basis": "Onboarding identity bootstrap",
        "last_confirmed": today,
        "review_after": today,
        "kind": "agent",
        "subtype": "agent",
        "canonical_name": agent_identity.name,
        "aliases": aliases,
        "nickname": nickname.strip() if nickname and nickname.strip() else None,
        "software": "Lisan",
        "hash": agent_identity.sha256,
        "seed": agent_identity.seed,
        "disambiguation": "Freshly initialized self-entity.",
        "epoch": 1,
        "epoch_started": today,
        "previous_epochs": [],
    }
    body = f"# {display_name}\n\n{display_name} is a freshly initialized Lisan instance serving {principal}.\nNo shared history yet.\n"
    write_markdown(path, frontmatter, body)
    return path


def _restore_existing_setup(vault: Path) -> None:
    principal = principal_name(vault)
    display_name = assistant_display_name(vault)
    digest = assistant_hash(vault)
    seed = assistant_seed(vault)
    nickname = assistant_nickname(vault)
    if not _has_content(vault / "primer" / "identity.md"):
        _write_identity(vault / "primer" / "identity.md", principal, "", "", "")
    if not _has_content(vault / "primer" / "operating-style.md"):
        _write_operating_style(vault / "primer" / "operating-style.md")
    high_stakes = vault / "primer" / "high-stakes.yaml"
    if not _has_content(high_stakes):
        _write_high_stakes(high_stakes)
    if digest:
        agent = AgentIdentity(seed=seed or digest, sha256=digest, konstel_hash="", name=assistant_name(vault))
        self_path = vault / "entities" / "agents" / f"{slugify(display_name)}.md"
        if not self_path.exists():
            _write_self_entity(vault, agent_identity=agent, principal=principal, nickname=nickname)


def _identity_status(vault: Path) -> str:
    digest = assistant_hash(vault)
    if not digest:
        return "missing"
    name = assistant_display_name(vault)
    return f"present ({name})"


# ── Main flow ─────────────────────────────────────────────────────────────────

def needs_onboarding(vault: Path) -> bool:
    identity = vault / "primer" / "identity.md"
    operating = vault / "primer" / "operating-style.md"
    high_stakes = vault / "primer" / "high-stakes.yaml"
    if not assistant_hash(vault):
        return True
    self_path = vault / "entities" / "agents" / f"{slugify(assistant_display_name(vault))}.md"
    return (
        not _has_content(identity)
        or not _has_content(operating)
        or not _has_content(high_stakes)
        or not self_path.exists()
    )


def run_onboarding(vault: Path) -> bool:
    """Run the interactive onboarding flow. Returns True if completed, False if skipped."""
    identity_path = vault / "primer" / "identity.md"
    identity_core_path = vault / "primer" / "identity-core.md"
    operating_path = vault / "primer" / "operating-style.md"
    high_stakes_path = vault / "primer" / "high-stakes.yaml"

    if assistant_hash(vault):
        _restore_existing_setup(vault)
        print()
        print(color("  Existing identity found.", BOLD))
        print(color(f"  Name: {assistant_display_name(vault)}", DIM))
        print(color("  Hash is stored in primer/identity-core.md; identity will not be regenerated.", DIM))
        print()
        return True

    print()
    print(color("  Welcome to Lisan.", BOLD))
    print(color("  A few quick steps to set up your memory vault.", DIM))
    print(color("  Type /skip at any prompt to finish later and edit the files directly.", DIM))
    print(color("  Press Enter to leave the open prompt blank for now.", DIM))
    print()

    from .chat import startup_check

    config = load_config()
    startup_check(vault, config)

    # ── Step 2: agent identity generation ────────────────────────────────────
    choice = _prompt_agent_identity()
    if choice is None:
        _skip_message(vault)
        return False
    agent_identity, custom_nickname = choice

    # ── Step 3: principal name ───────────────────────────────────────────────
    principal = _ask("What's your name?", allow_blank=False)
    while principal == "":
        print(color("  Please enter a name or /skip.", DIM))
        principal = _ask("What's your name?", allow_blank=False)
        if principal is None:
            _skip_message(vault)
            return False
    if principal is None:
        _skip_message(vault)
        return False

    # ── Step 4: open prompt ──────────────────────────────────────────────────
    background = _ask("Tell me something about yourself.", allow_blank=True)
    if background is None:
        _skip_message(vault)
        return False

    _write_identity(
        identity_path,
        name=principal,
        background=background or "",
        values="",
        relationships="",
    )
    _write_identity_core(
        identity_core_path,
        name=principal,
        agent_identity=agent_identity,
        nickname=custom_nickname,
    )
    _write_operating_style(operating_path)
    _write_high_stakes(high_stakes_path)
    _write_self_entity(
        vault,
        agent_identity=agent_identity,
        principal=principal,
        nickname=custom_nickname,
    )

    print()
    display_name = custom_nickname or agent_identity.name

    print(color("  ✓", GREEN) + color(" Primer files written.", BOLD))
    print(color(f"  Name: {display_name}", DIM))
    if custom_nickname and custom_nickname != agent_identity.name:
        print(color(f"  Canonical: {agent_identity.name}", DIM))
    print(color("  You can edit them anytime at:", DIM))
    print(color(f"    {identity_path}", DIM))
    print(color(f"    {identity_core_path}", DIM))
    print(color(f"    {operating_path}", DIM))
    print(color(f"    {high_stakes_path}", DIM))
    print()
    print(color(f"  I'm {display_name}. Your vault is set up and I'm ready to go.", BOLD))
    print()
    return True


def _prompt_agent_identity() -> tuple[AgentIdentity, str | None] | None:
    while True:
        agent_identity = generate_agent_identity()
        print(color("  Your agent's identity has been generated.", BOLD))
        print()
        print(color(f"  Hash:  {agent_identity.sha256}", DIM))
        print(color(f"  Name:  {agent_identity.name}", DIM))
        print()
        print(color("  This hash uniquely identifies this agent instance.", DIM))
        print(color("  The name is a human-readable projection of that hash.", DIM))
        print()
        print(color("  [1] Keep the generated name", DIM))
        print(color("  [2] Generate a new identity", DIM))
        print(color("  [3] Choose a custom name", DIM))
        print()
        choice = _ask("Choose 1, 2, or 3:", allow_blank=False)
        if choice is None:
            return None
        if choice == "1":
            return agent_identity, None
        if choice == "2":
            continue
        if choice == "3":
            custom = _ask("Choose the displayed name:", allow_blank=True)
            if custom is None:
                return None
            custom = custom.strip()
            if custom:
                return agent_identity, custom
            return agent_identity, None
        print(color("  Please choose 1, 2, or 3.", DIM))


def _skip_message(vault: Path) -> None:
    identity_path = vault / "primer" / "identity.md"
    operating_path = vault / "primer" / "operating-style.md"
    print()
    print(color("  Onboarding skipped. Edit these files to give Lisan context about you:", DIM))
    print(color(f"    {identity_path}", DIM))
    print(color(f"    {operating_path}", DIM))
    print()
