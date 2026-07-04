"""Layer B, part 2: capability beliefs (Phase 2 WO-4).

Two organs, never merged: the capability *manifest* (self_model.py) answers
"what can I do" and stays deterministic; capability *beliefs* answer "what
am I good at" — competence claims with confidence and evidence pointers,
revisable when episodic evidence contradicts them (WO-6's reconciliation
job). A revision is never a silent overwrite: the old statement moves into
the ``revisions`` chain with the evidence that displaced it, and the record
body grows an explicit self-revision note — the growth-arc mechanic.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import slugify, today_iso
from .domain_fields import with_domain_fields

_CONFIDENCES = ("low", "medium", "high")


def beliefs_dir(vault: Path) -> Path:
    return vault / "self" / "beliefs"


def new_self_belief(
    vault: Path,
    statement: str,
    *,
    confidence: str = "low",
    evidence_refs: list[str] | None = None,
    basis: str = "",
) -> Path:
    statement = str(statement or "").strip()
    if not statement:
        raise ValueError("A belief needs a statement.")
    if confidence not in _CONFIDENCES:
        raise ValueError(f"confidence must be one of {_CONFIDENCES}")
    today = today_iso()
    slug = slugify(statement)[:60]
    path = beliefs_dir(vault) / f"{slug}.md"
    if path.exists():
        raise FileExistsError(path)
    frontmatter = {
        "id": f"self_belief.{slug}",
        "type": "self_belief",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": "low",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "private",
        "summary": statement,
        "links": [],
        "confidence": "medium",
        "confidence_basis": basis or "Self-assessment; open to revision on evidence",
        "last_confirmed": today,
        "review_after": today,
        "belief_confidence": confidence,
        "evidence_refs": list(evidence_refs or []),
        "revisions": [],
    }
    body = f"# Belief\n\n{statement}\n\n## History\n\n- {today} — formed.\n"
    write_markdown(path, with_domain_fields(frontmatter), body)
    return path


def revise_self_belief(
    path: Path,
    *,
    new_statement: str,
    new_confidence: str,
    reason: str,
    evidence_refs: list[str],
) -> Path:
    """The 3PO arc mechanic: 'I believed X about myself; evidence suggests
    otherwise.' The old belief is chained, never erased."""
    new_statement = str(new_statement or "").strip()
    if not new_statement:
        raise ValueError("A revision needs a new statement.")
    if new_confidence not in _CONFIDENCES:
        raise ValueError(f"confidence must be one of {_CONFIDENCES}")
    if not evidence_refs:
        raise ValueError("A self-revision without evidence is just a mood — refs required.")
    doc = load_markdown(path)
    frontmatter = dict(doc.frontmatter)
    if str(frontmatter.get("type") or "") != "self_belief":
        raise ValueError(f"{path} is not a self_belief record")
    today = today_iso()
    revisions = list(frontmatter.get("revisions") or [])
    revisions.append(
        {
            "date": today,
            "previous_statement": str(frontmatter.get("summary") or ""),
            "previous_confidence": str(frontmatter.get("belief_confidence") or ""),
            "reason": str(reason or "").strip(),
            "evidence_refs": list(evidence_refs),
        }
    )
    frontmatter["revisions"] = revisions
    old_statement = str(frontmatter.get("summary") or "")
    frontmatter["summary"] = new_statement
    frontmatter["belief_confidence"] = new_confidence
    frontmatter["evidence_refs"] = sorted(set(frontmatter.get("evidence_refs") or []) | set(evidence_refs))
    frontmatter["updated"] = today

    body = doc.body
    body = re.sub(r"(# Belief\n\n).*?(\n\n## History)", rf"\g<1>{new_statement}\g<2>", body, count=1, flags=re.DOTALL)
    note = (
        f"- {today} — revised. I believed \"{old_statement}\"; "
        f"{reason.strip() or 'the evidence'} suggests otherwise "
        f"(evidence: {', '.join(f'`{r}`' for r in evidence_refs)})."
    )
    body = body.rstrip() + "\n" + note + "\n"
    write_markdown(path, frontmatter, body)
    return path


def list_self_beliefs(vault: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = beliefs_dir(vault)
    if not root.exists():
        return out
    for path in sorted(root.glob("*.md")):
        try:
            fm = dict(load_markdown(path).frontmatter)
        except Exception:
            continue
        if str(fm.get("type") or "") != "self_belief":
            continue
        fm["path"] = str(path)
        out.append(fm)
    return out
