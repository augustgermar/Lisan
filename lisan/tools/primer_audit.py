from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from ..frontmatter import load_markdown
from ..agents import DreamerAgent
from ..paths import vault_root
from ..utils import today_iso
from .context_budget import Record, Section, render


def _readable(path: Path) -> bool:
    """A record that will not parse is skipped, as it always has been."""
    try:
        load_markdown(path)
    except Exception:
        return False
    return True


def primer_audit_sections(vault: Path | None = None) -> list[Section]:
    """The audit bundle as packable sections — see context_budget for why
    this is a list of records rather than one string."""
    vault = vault or vault_root()
    sections: list[Section] = []

    for rel in ["primer/operating-style.md"]:
        path = vault / rel
        if path.exists():
            sections.append(
                Section(f"## {rel}", (Record("", path.read_text(encoding="utf-8").strip()),))
            )

    sections.append(
        Section(
            "## State Files",
            tuple(
                Record(f"### {path.name}", path.read_text(encoding="utf-8").strip())
                for path in sorted((vault / "state").glob("*.md"))
                if _readable(path)
            ),
        )
    )
    sections.append(
        Section(
            "## Entities",
            tuple(
                Record(f"### {path.relative_to(vault)}", path.read_text(encoding="utf-8").strip())
                for path in sorted((vault / "entities").rglob("*.md"))
                if _readable(path)
            ),
        )
    )

    cutoff = date.today() - timedelta(days=90)
    episodes: list[Record] = []
    for path in sorted((vault / "episodes").glob("*.md")):
        try:
            created = load_markdown(path).frontmatter.get("created")
        except Exception:
            continue
        if not created:
            continue
        try:
            if date.fromisoformat(str(created)) < cutoff:
                continue
        except ValueError:
            continue
        episodes.append(Record(f"### {path.name}", path.read_text(encoding="utf-8").strip()))
    sections.append(Section("## Recent Episodes", tuple(episodes)))
    return sections


def build_primer_audit_bundle(vault: Path | None = None) -> str:
    return render(primer_audit_sections(vault))


def run_primer_audit(
    vault: Path | None = None,
    dry_run: bool = False,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    """``provider=None`` means "ask the routing table", which is the only
    answer that works on an arbitrary install. This defaulted to the literal
    string "anthropic" — a provider absent from DEFAULT_CONFIG, so
    `lisan primer-audit` failed on any install that had not hand-added it."""
    vault = vault or vault_root()
    bundle = build_primer_audit_bundle(vault)
    if dry_run:
        return bundle

    response = DreamerAgent(vault=vault, prompt_file="dreamer_primer_v1").run_json(
        bundle,
        significance="high",
        provider=provider,
        model=model,
        task="primer",
    )
    out = vault / "reports" / f"primer-audit-draft-{today_iso()}.md"
    if isinstance(response, dict):
        out.write_text(
            "# Primer Audit Draft\n\n```json\n" + json.dumps(response, indent=2, ensure_ascii=True) + "\n```\n",
            encoding="utf-8",
        )
    else:
        out.write_text(str(response), encoding="utf-8")
    return str(out)
