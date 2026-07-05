"""Belief formation (WO-10 — docs/belief_formation.md, owner-approved).

Dreamer-proposes-owner-approves, mirroring the voice ceremony — except the
proposer here is not even a model: candidates are rule-derived
generalizations over first-person episode *outcomes*, so the whole
extraction pass is deterministic. Formation is the last confabulation
surface in the self-knowledge loop (a belief formed from thin evidence
becomes the agent's self-story), so creation is gated harder than
revision:

- >= 3 supporting self-episodes spanning >= 2 distinct days
- every cited episode must exist and carry source_refs
- counterexamples are listed on the candidate, never hidden; a candidate
  with contradiction ratio > 1/3 is dropped by the extractor
- eval-tagged episodes (conversation ids or source refs matching the eval
  namespaces) do not count toward the gate — beliefs come from real use,
  not rehearsals
- <= 7 candidates per artifact; confidence at most "medium" at birth

No provisional path: beliefs enter the ledger owner-ratified or not at
all (an empty belief ledger is a safe default).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..paths import vault_root
from ..utils import today_iso

EVAL_NAMESPACES = ("eval-", "scale-", "cap-", "grow-", "hermes-", "baseline-", "wo5-")
MIN_SUPPORT = 3
MIN_DAYS = 2
MAX_CONTRADICTION_RATIO = 1 / 3
MAX_CANDIDATES = 7
BIRTH_CONFIDENCE = "medium"  # never higher at formation; earned through reconcile


@dataclass(slots=True)
class BeliefCandidate:
    statement: str
    supporting: list[str] = field(default_factory=list)
    counterexamples: list[str] = field(default_factory=list)
    days: set[str] = field(default_factory=set)

    @property
    def contradiction_ratio(self) -> float:
        total = len(self.supporting) + len(self.counterexamples)
        return len(self.counterexamples) / total if total else 0.0


def _is_eval_tagged(fm: dict[str, Any]) -> bool:
    haystack = " ".join(
        [str(fm.get("conversation_id") or "")]
        + [str(r) for r in (fm.get("source_refs") or [])]
        + [str(fm.get("summary") or "")]
    ).lower()
    return any(ns in haystack for ns in EVAL_NAMESPACES)


def _load_self_episodes(vault: Path) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    root = vault / "self" / "episodes"
    if not root.exists():
        return episodes
    for path in sorted(root.glob("*.md")):
        try:
            fm = dict(load_markdown(path).frontmatter)
        except Exception:
            continue
        if str(fm.get("type") or "") != "self_episode":
            continue
        if not fm.get("source_refs"):
            continue  # a self-episode without sources cannot support a belief
        if _is_eval_tagged(fm):
            continue
        episodes.append(fm)
    return episodes


# (event_kind, outcome-class) → belief statement templates. Support and
# counterexample outcomes per class; the statement is a generalization
# over what actually happened, phrased first-person, measured.
_CLASSES = [
    {
        "kind": "plan",
        "support": "succeeded",
        "counter": "failed",
        "statement": "I complete multi-step plans reliably once they are underway.",
    },
    {
        "kind": "task",
        "support": "succeeded",
        "counter": "failed",
        "statement": "I deliver scheduled tasks and reminders dependably.",
    },
    {
        "kind": "plan",
        "support": "failed",
        "counter": "succeeded",
        "statement": "My multi-step plans have a recurring failure mode worth watching.",
    },
    {
        "kind": "task",
        "support": "failed",
        "counter": "succeeded",
        "statement": "My scheduled tasks fail often enough to double-check delivery.",
    },
]


def extract_belief_candidates(vault: Path) -> list[BeliefCandidate]:
    """Deterministic: same episodes → same candidates, in template order."""
    episodes = _load_self_episodes(vault)
    candidates: list[BeliefCandidate] = []
    for cls in _CLASSES:
        cand = BeliefCandidate(statement=cls["statement"])
        for fm in episodes:
            if str(fm.get("event_kind")) != cls["kind"]:
                continue
            outcome = str(fm.get("outcome") or "")
            episode_id = str(fm.get("id") or "")
            if outcome == cls["support"]:
                cand.supporting.append(episode_id)
                cand.days.add(str(fm.get("created") or "")[:10])
            elif outcome == cls["counter"]:
                cand.counterexamples.append(episode_id)
        if len(cand.supporting) < MIN_SUPPORT:
            continue
        if len(cand.days) < MIN_DAYS:
            continue
        if cand.contradiction_ratio > MAX_CONTRADICTION_RATIO:
            continue
        candidates.append(cand)
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates


def run_belief_extraction(vault: Path | None = None, *, out: Path | None = None) -> dict[str, Any]:
    vault = vault or vault_root()
    candidates = extract_belief_candidates(vault)
    now = datetime.now().astimezone()
    stamp = now.strftime("%Y%m%d%H%M%S")
    day = now.date().isoformat()
    path = out or (vault / "reports" / f"belief-extraction-{stamp}.md")
    payload = [
        {
            "statement": c.statement,
            "supporting": c.supporting,
            "counterexamples": c.counterexamples,
            "days": sorted(c.days),
            "contradiction_ratio": round(c.contradiction_ratio, 3),
        }
        for c in candidates
    ]
    frontmatter = {
        "id": f"report.belief-extraction.{stamp}",
        "type": "report",
        "created": day,
        "updated": day,
        "status": "active",
        "significance": "high",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "private",
        "summary": "Belief formation — ratification artifact",
        "links": [],
        "confidence": "medium",
        "confidence_basis": "Deterministic generalization over first-person episode outcomes",
        "last_confirmed": day,
        "review_after": day,
        "artifact_kind": "beliefs",
        "belief_extraction": {"candidates": payload},
    }
    lines = ["# Belief formation — ratification artifact", ""]
    if payload:
        for cand in payload:
            lines.append(f"- **{cand['statement']}**")
            lines.append(f"  - supporting: {len(cand['supporting'])} episodes over {len(cand['days'])} days")
            if cand["counterexamples"]:
                lines.append(f"  - counterexamples (listed, not hidden): {', '.join(cand['counterexamples'])}")
    else:
        lines.append("No candidate cleared the gate — the honest common case.")
    lines.append("")
    lines.append("To ratify (owner only — beliefs have no provisional path): "
                 "`lisan self ratify --from <this file>`. Prune lines you reject first.")
    write_markdown(path, frontmatter, "\n".join(lines))
    return {"artifact": str(path), "candidates": len(payload)}


def ratify_beliefs(vault: Path | None = None, *, artifact_path: Path) -> list[Path]:
    """Owner ratification: every candidate's evidence is RE-verified against
    the vault (exists, has source_refs, not eval-tagged) — the artifact is a
    proposal, never an authority. Confidence is capped at birth."""
    from .self_beliefs import new_self_belief

    vault = vault or vault_root()
    doc = load_markdown(Path(artifact_path))
    data = doc.frontmatter.get("belief_extraction") if isinstance(doc.frontmatter, dict) else None
    candidates = (data or {}).get("candidates") or []
    if not candidates:
        raise ValueError(f"No belief candidates in artifact {artifact_path}; nothing to ratify.")
    valid_ids = {str(fm.get("id")): fm for fm in _load_self_episodes(vault)}
    created: list[Path] = []
    for cand in candidates[:MAX_CANDIDATES]:
        statement = str(cand.get("statement") or "").strip()
        refs = [r for r in (cand.get("supporting") or []) if str(r) in valid_ids]
        days = {str(valid_ids[str(r)].get("created") or "")[:10] for r in refs}
        if not statement or len(refs) < MIN_SUPPORT or len(days) < MIN_DAYS:
            continue
        try:
            created.append(
                new_self_belief(
                    vault,
                    statement,
                    confidence=BIRTH_CONFIDENCE,
                    evidence_refs=sorted(refs),
                    basis=f"Owner-ratified from {Path(artifact_path).name} ({today_iso()})",
                )
            )
        except FileExistsError:
            continue  # already formed: ratification is idempotent
    return created
