"""IIP Phase 2 — the corpus-adversarial register.

The finding this closes: a Skeptic built from the same corpus it checks
will faithfully audit everything except the corpus's own priors. The
narrative store's favorite causal frames — "anomalies around subject X
get attributed to agent Y", "person Z is modeled as never originating
anything" — reproduce themselves in every interpretation drawn from it.

Two halves:

- **The miner** (``corpus.audit_priors``, a daily job): deterministic,
  LLM-free pass over claims and episodes counting recurring causal
  attributions between entities. Regularities above threshold become
  ``attribution_prior`` pattern records — the register — so the corpus's
  habits are themselves retrievable, auditable records with the pattern
  lifecycle and language gates (approved deviation from the brief's "own
  narrative type", PLAN §6.3).
- **The challenge** (wired into the IIP validator): if EVERY hypothesis
  in an interpretation instantiates a registered prior, the response is
  regenerated with a demand for at least one reading from outside the
  register. Composes with Phase 1: locus diversity guarantees shape;
  this guarantees the corpus's favorite frames can't fill every slot.

Mining is deliberately conservative and mechanical: a prior needs
``MIN_SUPPORT`` distinct records before it registers. Two data points is
a coincidence; the register must earn its challenges the same way every
other hypothesis layer earns its standing.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso
from .log import get_logger
from .rebuild_index import reindex_record

MIN_SUPPORT = 3
_WINDOW = 90  # chars around a causal marker searched for entity names

_CAUSAL_RE = re.compile(
    r"\b(because of|due to|caused by|driven by|blamed on|attributed to|"
    r"the result of|at the (?:direction|insistence|behest) of|"
    r"under (?:pressure|the influence) (?:of|from)|controlled by|"
    r"orchestrated by|decided by|relayed by)\b"
)

_NON_ORIGIN_RE = re.compile(
    r"\b(never (?:chose|chooses|decided|decides|initiated|initiates)|"
    r"didn.?t (?:choose|decide|initiate)|had no (?:choice|say)|"
    r"went along with|was (?:made|told|forced|pressured) to|"
    r"only (?:does|did) what|is never the one who)\b"
)


# ── Entity roster ────────────────────────────────────────────────────────────

def _entity_roster(vault: Path) -> dict[str, dict[str, Any]]:
    """entity_id → {names: lowercase name/alias set, canonical}. The
    principal rides along under its substrate token."""
    roster: dict[str, dict[str, Any]] = {
        "principal": {"names": {"{{principal}}"}, "canonical": "{{principal}}"},
    }
    root = vault / "entities"
    if not root.exists():
        return roster
    for path in sorted(root.rglob("*.md")):
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        entity_id = str(fm.get("id") or "").strip()
        canonical = str(fm.get("canonical_name") or "").strip()
        if not entity_id or not canonical:
            continue
        names = {canonical.lower()}
        first = canonical.split()[0]
        if len(first) >= 3:
            names.add(first.lower())
        for alias in fm.get("aliases") or []:
            alias = str(alias).strip().lower()
            if len(alias) >= 3:
                names.add(alias)
        roster[entity_id] = {"names": names, "canonical": canonical}
    return roster


def _entities_near(text: str, start: int, end: int, roster: dict[str, dict[str, Any]]) -> list[str]:
    """Entity ids whose names appear inside text[start:end], nearest first."""
    window = text[max(0, start):end].lower()
    found: list[tuple[int, str]] = []
    for entity_id, info in roster.items():
        for name in info["names"]:
            pos = window.find(name)
            if pos >= 0:
                found.append((pos, entity_id))
                break
    found.sort()
    return [entity_id for _, entity_id in found]


# ── Mining ───────────────────────────────────────────────────────────────────

def _iter_source_records(vault: Path):
    for rel in ("claims", "episodes"):
        root = vault / rel
        if not root.exists():
            continue
        for path in sorted(root.glob("*.md")):
            try:
                fm = load_markdown(path).frontmatter
            except Exception:
                continue
            record_id = str(fm.get("id") or "")
            text = " ".join(
                str(fm.get(key) or "") for key in ("claim_text", "summary")
            ).strip()
            stamp = str(fm.get("record_date") or fm.get("created") or "")
            if record_id and text:
                yield record_id, text, stamp


def mine_attribution_priors(
    vault: Path,
    *,
    db_path: Path | None = None,
    min_support: int = MIN_SUPPORT,
) -> dict[str, Any]:
    """One deterministic pass over the corpus; upserts the register.

    Idempotent: counts are recomputed from scratch each run and written
    onto stable-id register records. No LLM anywhere in this path."""
    roster = _entity_roster(vault)
    pairwise: dict[tuple[str, str], dict[str, Any]] = {}
    non_origin: dict[str, dict[str, Any]] = {}

    for record_id, text, stamp in _iter_source_records(vault):
        lowered = text.lower()
        for match in _CAUSAL_RE.finditer(lowered):
            agents = _entities_near(lowered, match.end(), match.end() + _WINDOW, roster)
            subjects = _entities_near(lowered, match.start() - _WINDOW, match.start(), roster)
            agent = agents[0] if agents else None
            subject = next((s for s in subjects if s != agent), None)
            if not agent or not subject:
                continue
            entry = pairwise.setdefault((subject, agent), {"refs": set(), "first": stamp, "last": stamp})
            entry["refs"].add(record_id)
            entry["first"] = min(entry["first"], stamp) if entry["first"] else stamp
            entry["last"] = max(entry["last"], stamp)
        for match in _NON_ORIGIN_RE.finditer(lowered):
            subjects = _entities_near(lowered, match.start() - _WINDOW, match.start(), roster)
            if not subjects:
                continue
            subject = subjects[0]
            entry = non_origin.setdefault(subject, {"refs": set(), "first": stamp, "last": stamp})
            entry["refs"].add(record_id)
            entry["first"] = min(entry["first"], stamp) if entry["first"] else stamp
            entry["last"] = max(entry["last"], stamp)

    written = 0
    for (subject, agent), entry in sorted(pairwise.items()):
        if len(entry["refs"]) >= min_support:
            written += _upsert_register_record(
                vault, db_path=db_path, roster=roster, kind="pairwise",
                subject=subject, agent=agent, entry=entry,
            )
    for subject, entry in sorted(non_origin.items()):
        if len(entry["refs"]) >= min_support:
            written += _upsert_register_record(
                vault, db_path=db_path, roster=roster, kind="non_originating",
                subject=subject, agent=None, entry=entry,
            )
    summary = {
        "records_scanned": sum(1 for _ in _iter_source_records(vault)),
        "pairwise_candidates": len(pairwise),
        "non_origin_candidates": len(non_origin),
        "register_entries_written": written,
    }
    get_logger(vault).info(f"corpus.audit_priors {summary}")
    return summary


def _find_register_record(vault: Path, kind: str, subject: str, agent: str | None) -> Path | None:
    """Identity of a register entry is its attribution triple, not its
    filename — the hypothesis text (and so the slug) changes as counts grow."""
    root = vault / "patterns"
    if not root.exists():
        return None
    for path in sorted(root.glob("*.md")):
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        if (
            str(fm.get("pattern_type")) == "attribution_prior"
            and str(fm.get("attribution_kind")) == kind
            and str(fm.get("attribution_subject")) == subject
            and str(fm.get("attribution_agent") or "") == (agent or "")
        ):
            return path
    return None


def _upsert_register_record(
    vault: Path,
    *,
    db_path: Path | None,
    roster: dict[str, dict[str, Any]],
    kind: str,
    subject: str,
    agent: str | None,
    entry: dict[str, Any],
) -> int:
    from .record_factory import new_pattern

    subject_name = roster.get(subject, {}).get("canonical", subject)
    agent_name = roster.get(agent, {}).get("canonical", agent) if agent else None
    if kind == "pairwise":
        hypothesis = (
            f"Recurring attribution in the record: events involving {subject_name} "
            f"are attributed to {agent_name} ({len(entry['refs'])} records)"
        )
    else:
        hypothesis = (
            f"Recurring frame in the record: {subject_name} is modeled as "
            f"non-originating ({len(entry['refs'])} records)"
        )
    refs = sorted(entry["refs"])
    path = _find_register_record(vault, kind, subject, agent)
    if path is None:
        created = new_pattern(
            vault,
            pattern_type="attribution_prior",
            hypothesis=hypothesis,
            status="active_hypothesis",
            significance="medium",
            privacy="personal",
            disclosure="private",
            confidence=0.5,
            supporting_records=refs,
            alternative_explanations=[
                "The recurring attribution may reflect real causation, not a narrative habit",
            ],
            evidence_needed=["Interpretations that survive without this frame"],
        )
        path = created.path
    doc = load_markdown(path)
    fm = dict(doc.frontmatter)
    fm["hypothesis"] = hypothesis
    fm["summary"] = hypothesis
    fm["supporting_records"] = refs
    fm["attribution_kind"] = kind
    fm["attribution_subject"] = subject
    fm["attribution_agent"] = agent or ""
    fm["support_count"] = len(refs)
    fm["first_seen"] = entry["first"] or fm.get("first_seen") or today_iso()
    fm["last_seen"] = entry["last"] or today_iso()
    fm["last_reviewed"] = today_iso()
    fm["updated"] = today_iso()
    # Ceiling: a register entry is a fact about the CORPUS, never about the
    # world; it must not outrank the hypotheses it exists to challenge.
    fm["confidence"] = min(float(fm.get("confidence") or 0.5), 0.5)
    write_markdown(path, fm, doc.body)
    reindex_record(path, vault, db_path, quiet=True)
    return 1


# ── The register, as the challenge consumes it ──────────────────────────────

def load_attribution_register(vault: Path) -> list[dict[str, Any]]:
    """Active register entries with the name sets the challenge matches on."""
    roster = _entity_roster(vault)
    out: list[dict[str, Any]] = []
    root = vault / "patterns"
    if not root.exists():
        return out
    for path in sorted(root.glob("*.md")):
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        if str(fm.get("pattern_type")) != "attribution_prior":
            continue
        if str(fm.get("status")) in {"rejected", "retired", "superseded", "stale"}:
            continue
        target = str(fm.get("attribution_agent") or fm.get("attribution_subject") or "")
        names = set(roster.get(target, {}).get("names") or set())
        if not names:
            continue
        out.append({
            "id": str(fm.get("id") or path.stem),
            "kind": str(fm.get("attribution_kind") or ""),
            "target": target,
            "names": names,
        })
    return out


def hypotheses_all_in_register(
    payload: dict[str, Any],
    register: list[dict[str, Any]],
) -> list[str] | None:
    """The challenge condition: EVERY hypothesis names a registered target.

    Returns the matched register ids when the condition holds, else None.
    One hypothesis from outside the register clears it — which is exactly
    what the challenge regeneration demands."""
    interp = payload.get("interpretation")
    if not isinstance(interp, dict) or not register:
        return None
    hypotheses = [h for h in (interp.get("hypotheses") or []) if isinstance(h, dict)]
    if not hypotheses:
        return None
    matched: set[str] = set()
    for h in hypotheses:
        text = str(h.get("text") or "").lower()
        hit = None
        for entry in register:
            if any(re.search(rf"\b{re.escape(name)}\b", text) for name in entry["names"]):
                hit = entry["id"]
                break
        if hit is None:
            return None  # at least one reading stands outside the register
        matched.add(hit)
    return sorted(matched)


def challenge_feedback(matched_ids: list[str]) -> str:
    return (
        "Corpus-adversarial check: every hypothesis you offered instantiates an "
        f"established attribution prior in my own records ({', '.join(matched_ids)}). "
        "The record's favorite explanations must not fill every slot. Regenerate the "
        "full response with at least one hypothesis from outside these established "
        "frames — a different causal locus or a reading that does not route through "
        "the usual people. Do not mention this correction to the user."
    )
