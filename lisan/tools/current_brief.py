from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown
from ..paths import vault_root
from ..utils import today_iso
from .domain_fields import domain_primary as get_domain_primary, normalize_domain_fields


@dataclass(slots=True)
class BriefState:
    domain: str
    summary: str
    confidence: str
    stale: bool
    ttl_days: int | None
    review_after: str | None
    updated: str | None

    @property
    def category(self) -> str:
        return self.domain

    @property
    def arena(self) -> str:
        return self.domain


def generate_current_brief(vault: Path | None = None) -> str:
    vault = vault or vault_root()
    states = _load_states(vault)
    lines: list[str] = [
        "# Current Brief",
        "",
        f"Generated: {today_iso()}",
        "",
        "You are working from the active state summaries below.",
        "",
    ]
    if not states:
        lines.append("No active state files exist yet.")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    lines.append("## Active Domains")
    for state in states:
        freshness = "stale" if state.stale else "current"
        ttl = f"{state.ttl_days}d" if state.ttl_days is not None else "none"
        review_after = state.review_after or "none"
        updated = state.updated or "unknown"
        lines.append(f"### {state.category}")
        lines.append(f"- summary: {state.summary}")
        lines.append(f"- confidence: {state.confidence}")
        lines.append(f"- freshness: {freshness}")
        lines.append(f"- ttl_days: {ttl}")
        lines.append(f"- updated: {updated}")
        lines.append(f"- review_after: {review_after}")
        lines.append("")

    stale_states = [state for state in states if state.stale]
    if stale_states:
        lines.append("## Stale Domains")
        for state in stale_states:
            lines.append(f"- {state.category}: {state.summary}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_current_brief(vault: Path | None = None) -> Path:
    vault = vault or vault_root()
    out = vault / "primer" / "current-brief.md"
    out.write_text(generate_current_brief(vault), encoding="utf-8")
    return out


def _load_states(vault: Path) -> list[BriefState]:
    states: list[BriefState] = []
    for path in sorted((vault / "state").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        frontmatter = normalize_domain_fields(doc.frontmatter)
        if str(frontmatter.get("type")) != "state":
            continue
        domain = get_domain_primary(frontmatter)
        summary = str(frontmatter.get("summary", ""))
        confidence = str(frontmatter.get("confidence", "low"))
        ttl_days = frontmatter.get("ttl_days")
        try:
            ttl_days_int = int(ttl_days) if ttl_days is not None and ttl_days != "" else None
        except (TypeError, ValueError):
            ttl_days_int = None
        review_after = str(frontmatter.get("review_after", "")) or None
        updated = str(frontmatter.get("updated", "")) or None
        stale = _is_stale(updated, ttl_days_int)
        states.append(
            BriefState(
                domain=domain,
                summary=summary,
                confidence=confidence,
                stale=stale,
                ttl_days=ttl_days_int,
                review_after=review_after,
                updated=updated,
            )
        )
    return states


def _is_stale(updated: str | None, ttl_days: int | None) -> bool:
    if not updated or ttl_days is None:
        return False
    try:
        age = (date.today() - date.fromisoformat(updated)).days
    except ValueError:
        return False
    return age > ttl_days
