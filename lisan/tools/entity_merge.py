"""Safe entity merging: absorb a fragment into its true entity.

The scale test showed entities fragmenting into base + qualified variants
("deck rebuild" / "deck rebuild project (summer 2026)"). Prevention now
happens at birth (entity_resolution binds suffix-qualified proposals to
the base), and this module closes the other half: merging fragments that
already exist.

A merge never destroys data:
- the fragment's narrative and source_log entries are appended to the
  survivor's durable source_log (dated, provenance-marked);
- the fragment's names become the survivor's aliases, so every future
  mention binds to the survivor;
- the fragment file itself moves to archive/entities/ (reversible);
- one compaction job re-tells the survivor's story with the new material.

Ambiguous candidates are never merged automatically — they surface as
question-phrased deviations for the owner ("are these the same thing?"),
consistent with the surface-with-curiosity contradiction policy.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso
from .log import get_logger


_DISTINCTION_PHRASES = (
    "different people",
    "distinct people",
    "separate people",
    "not the same person",
    "not the same entity",
    "not to be confused",
)
_MONTHS = {
    name.lower(): index
    for index, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        1,
    )
}
_BIRTHDAY_TEXT = re.compile(
    r"\b(?:birthday|born)\b\s*(?:is|falls on|on|:)??\s*"
    r"(?:(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})|"
    r"(\d{4})-(\d{2})-(\d{2}))",
    re.IGNORECASE,
)
_TITLE_ALIAS_TOKENS = frozenset({
    "dealing", "helping", "team", "once", "productivity", "tiadynamics",
})


def explicit_owner_distinction(
    vault: Path,
    source_frontmatter: dict[str, Any],
    target_frontmatter: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return transcript evidence in which the owner distinguishes the pair.

    This is intentionally conservative and fail-closed.  It does not decide
    that two records are different from names alone; it recognizes only owner
    turns with explicit distinction language.  A later ambiguous assent cannot
    erase an earlier explicit distinction—the conflict must return to the
    owner rather than becoming an autonomous identity mutation.
    """
    source_names = _identity_names(source_frontmatter)
    target_names = _identity_names(target_frontmatter)
    evidence: list[dict[str, Any]] = []
    transcripts = vault / "transcripts"
    if not transcripts.exists():
        return evidence
    for path in sorted(transcripts.rglob("*.md")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, 1):
            if not line.lstrip().startswith("USER:"):
                continue
            text = line.split("USER:", 1)[1].strip()
            normalized = _identity_text(text)
            source_hit = any(_name_in_text(name, normalized) for name in source_names)
            target_hit = any(_name_in_text(name, normalized) for name in target_names)
            named = source_hit or target_hit
            rule = ""
            if named and "own person" in normalized:
                rule = "owner said one named entity is its own person"
            elif source_hit and target_hit and any(phrase in normalized for phrase in _DISTINCTION_PHRASES):
                rule = "owner explicitly described the named pair as distinct"
            elif source_hit and target_hit and any(word in normalized.split() for word in ("multiple", "several")):
                rule = "owner named both entities while describing multiple people"
            if rule:
                evidence.append(
                    {
                        "path": str(path.relative_to(vault)),
                        "line": line_number,
                        "text": text,
                        "rule": rule,
                    }
                )
    return evidence


def contradictory_structured_attributes(
    source_frontmatter: dict[str, Any],
    target_frontmatter: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return conflicts in structured identity attributes carried by records.

    Birthday is the first guarded attribute because it is a high-signal,
    owner-supplied identity field and a prior merge showed that a
    contradiction can exist without an explicit transcript distinction.
    Values are collected from frontmatter and source logs, then compared as
    month/day pairs so a missing year does not hide a conflict.
    """
    source = _birthday_values(source_frontmatter)
    target = _birthday_values(target_frontmatter)
    if not source or not target or source & target:
        return []
    return [{
        "attribute": "birthday",
        "source_values": sorted(source),
        "target_values": sorted(target),
        "rule": "source and target carry contradictory structured birthdays",
    }]


def _birthday_values(frontmatter: dict[str, Any]) -> set[tuple[int, int]]:
    values: set[tuple[int, int]] = set()
    direct = frontmatter.get("birthday")
    texts: list[str] = []
    if direct:
        texts.append(str(direct))
    for entry in frontmatter.get("source_log") or []:
        if isinstance(entry, dict) and entry.get("text"):
            texts.append(str(entry["text"]))
    for text in texts:
        for match in _BIRTHDAY_TEXT.finditer(text):
            if match.group(1):
                values.add((_MONTHS[match.group(1).lower()], int(match.group(2))))
            else:
                values.add((int(match.group(4)), int(match.group(5))))
    return values


def _is_title_alias(value: str) -> bool:
    tokens = set(_identity_text(value).split())
    return bool(tokens & _TITLE_ALIAS_TOKENS)


def _identity_names(frontmatter: dict[str, Any]) -> set[str]:
    values = [frontmatter.get("canonical_name"), *(frontmatter.get("aliases") or [])]
    names: set[str] = set()
    for value in values:
        normalized = _identity_text(str(value or ""))
        # Single common-name aliases are too ambiguous to establish which
        # entity an owner turn distinguishes. Canonical names remain eligible.
        if normalized and (
            value == frontmatter.get("canonical_name") or len(normalized.split()) >= 2
        ):
            names.add(normalized)
    return names


def _identity_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def _name_in_text(name: str, text: str) -> bool:
    return bool(name) and f" {name} " in f" {text} "


def merge_entities(
    vault: Path,
    source: str,
    target: str,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Merge entity *source* (name or id) into *target*. Returns a summary.
    Refuses identity merges and missing entities; never guesses."""
    src = _find_entity(vault, source)
    dst = _find_entity(vault, target)
    if src is None:
        return {"merged": False, "reason": f"source entity not found: {source}"}
    if dst is None:
        return {"merged": False, "reason": f"target entity not found: {target}"}
    if src == dst:
        return {"merged": False, "reason": "source and target are the same entity"}

    src_doc = load_markdown(src)
    dst_doc = load_markdown(dst)
    src_fm = dict(src_doc.frontmatter)
    dst_fm = dict(dst_doc.frontmatter)
    src_name = str(src_fm.get("canonical_name") or src.stem)
    dst_name = str(dst_fm.get("canonical_name") or dst.stem)

    distinction = explicit_owner_distinction(vault, src_fm, dst_fm)
    if distinction:
        first = distinction[0]
        return {
            "merged": False,
            "reason": (
                "owner transcript evidence says the entities are distinct: "
                f"{first['path']}:{first['line']} ({first['rule']})"
            ),
            "owner_distinction_evidence": distinction,
        }

    structured_conflicts = contradictory_structured_attributes(src_fm, dst_fm)
    if structured_conflicts:
        first = structured_conflicts[0]
        return {
            "merged": False,
            "reason": (
                "structured identity attributes conflict: "
                f"{first['attribute']} source={first['source_values']} "
                f"target={first['target_values']}"
            ),
            "structured_conflict_evidence": structured_conflicts,
        }

    # 1. absorb content into the survivor's durable log
    log = [dict(e) for e in (dst_fm.get("source_log") or []) if isinstance(e, dict)]
    src_body = re.sub(r"^#\s+.*$", "", src_doc.body, count=1, flags=re.M).strip()
    if src_body:
        log.append({
            "date": today_iso(),
            "text": f"(merged from duplicate entity '{src_name}') " + re.sub(r"\s+", " ", src_body)[:2400],
            "folded": False,
            "source": f"merge:{src.stem}",
        })
    for entry in (src_fm.get("source_log") or []):
        if isinstance(entry, dict) and entry.get("text"):
            carried = dict(entry)
            carried["folded"] = False
            carried.setdefault("source", f"merge:{src.stem}")
            log.append(carried)
    dst_fm["source_log"] = log

    # 2. names: the fragment's identity becomes reachable aliases
    aliases = {str(a).strip() for a in (dst_fm.get("aliases") or []) if str(a).strip()}
    if not _is_title_alias(src_name):
        aliases.add(src_name)
    aliases.update(
        str(a).strip()
        for a in (src_fm.get("aliases") or [])
        if str(a).strip() and not _is_title_alias(str(a))
    )
    aliases.discard(dst_name)
    dst_fm["aliases"] = sorted(aliases)
    dst_fm["updated"] = today_iso()
    write_markdown(dst, dst_fm, dst_doc.body)

    # 3. the fragment file retires to the archive (reversible), carrying a
    #    forwarding address. Without it, every record that referenced the
    #    fragment keeps resolving to the fragment: the id still exists and
    #    archived records stay indexed, so nothing looks broken while
    #    retrieval quietly reaches a stub instead of the person it was
    #    merged into. `merged_into` lets resolve_reference follow the merge,
    #    and `lisan migrate refs` rewrite it.
    archive = vault / "archive" / "entities"
    archive.mkdir(parents=True, exist_ok=True)
    archived_path = archive / f"merged-{src.stem}.md"
    src_fm["merged_into"] = str(dst_fm.get("id") or "")
    src_fm["status"] = "archived"
    write_markdown(src, src_fm, src_doc.body)
    shutil.move(str(src), str(archived_path))

    # 4. reindex + one compaction to weave the absorbed material in
    _reindex(vault, db_path, dst, removed=src)
    try:
        from .jobs import enqueue_job

        enqueue_job("entity.rewrite_story",
                    {"entity_path": str(dst), "force_compact": True}, db_path=db_path)
    except Exception:
        pass

    get_logger(vault).info(f"entity.merged source={src.stem} target={dst.stem}")
    return {
        "merged": True,
        "source": src_name,
        "target": dst_name,
        "archived": str(archived_path),
        "log_entries_carried": len(log),
    }


def dedup_candidates(vault: Path) -> list[dict[str, Any]]:
    """Same-kind near-duplicate pairs worth asking about: one canonical name
    is a token-subset or qualifier-variant of another. Reported, never
    auto-merged — the owner (or the agent in conversation, with the owner's
    yes) decides."""
    from .entity_resolution import _qualifier_base

    ents = []
    root = vault / "entities"
    if not root.exists():
        return []
    for p in sorted(root.rglob("*.md")):
        try:
            fm = load_markdown(p).frontmatter
        except Exception:
            continue
        name = str(fm.get("canonical_name") or "").strip()
        kind = str(fm.get("kind") or fm.get("subtype") or "").strip()
        if name:
            ents.append({"path": p, "name": name, "kind": kind})

    out = []
    for i, a in enumerate(ents):
        at = set(a["name"].lower().split())
        for b in ents[i + 1:]:
            if a["kind"] != b["kind"]:
                continue
            bt = set(b["name"].lower().split())
            subset = (at < bt or bt < at) and bool(at & bt)
            same_base = _qualifier_base(a["name"]) == _qualifier_base(b["name"]) and a["name"].lower() != b["name"].lower()
            exact = a["name"].lower() == b["name"].lower()
            if subset or same_base or exact:
                smaller, larger = (a, b) if len(at) <= len(bt) else (b, a)
                out.append({
                    "keep": smaller["name"], "absorb": larger["name"],
                    "kind": a["kind"],
                    "why": "exact duplicate" if exact else ("same base name" if same_base else "name is a token-subset"),
                })
    return out


def _find_entity(vault: Path, ref: str) -> Path | None:
    ref = str(ref or "").strip()
    if not ref:
        return None
    root = vault / "entities"
    if not root.exists():
        return None
    ref_lower = ref.lower()
    for p in sorted(root.rglob("*.md")):
        try:
            fm = load_markdown(p).frontmatter
        except Exception:
            continue
        names = {str(fm.get("canonical_name") or "").lower(), str(fm.get("id") or "").lower(), p.stem.lower()}
        names.update(str(a).lower() for a in (fm.get("aliases") or []))
        if ref_lower in names:
            return p
    return None


def _reindex(vault: Path, db_path: Path | None, updated: Path, *, removed: Path) -> None:
    from .rebuild_index import reindex_record

    reindex_record(updated, vault, db_path, remove=removed, quiet=True)
