"""Life ingestion: a personal notes vault becomes memory, not just reference.

The reference pipeline stores notes as searchable knowledge chunks. This
module goes further for the notes that are *about the owner's life*: a note
about a person becomes (or feeds) that person's entity narrative; a
date-titled note becomes an episode; everything else falls back to the
knowledge path. The classified routing is the whole point — and it is built
for disorganized vaults, because real ones are. Grounded against a real
vault with: no frontmatter anywhere, no wikilinks, lowercase-slug filenames
(``adrienne-mcgraw.md``), username filenames, a folder tree as the only
reliable signal, root-level junk (``tmp.md``, ``scratchpad.md``), empty
notes, and one 100k-word log-file of a note.

Classification is deterministic and conservative, in priority order:
1. an empty note is skipped;
2. a date-titled note (``2026-04-11.md``) is an episode;
3. a note under a kind-named folder (People/, Places/, Projects/, Pets/,
   Organizations/ — any case, anywhere in the relative path) is an entity
   note of that kind, named by its de-slugged filename;
4. everything else is knowledge (the safe default: still searchable,
   entity-linked by mention, never a wrongly-minted entity).

Entity notes are assimilated through the same organs conversation uses:
the note body is tokenized and appended to the entity's durable
``source_log`` (idempotent by source path + content hash), and one
``entity.rewrite_story`` compaction job is enqueued so the narrative is
composed in the background by the writer, under its no-shrink guardrail.
The full note text ALSO flows through the knowledge path, linked to the
entity, so nothing is lost to condensation. Source files are read-only.

The owner's own note about themself is never made a third-party entity —
a name matching the principal's aliases routes to knowledge.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso
from .deixis import tokenize_principal
from .log import get_logger, log_error

_FOLDER_KIND = {
    "people": "person", "person": "person", "friends": "person",
    "family": "person", "contacts": "person",
    "places": "place", "place": "place",
    "organizations": "organization", "orgs": "organization",
    "companies": "organization",
    "projects": "project", "project": "project",
    "pets": "pet", "animals": "pet",
}
_DATE_TITLE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Meta/organizational notes that live inside kind folders but are not a
# member of the kind — the real reference vault had "people-index" and
# "people-template" sitting right next to the actual people, and they
# became person entities on the first rehearsal.
_META_TOKENS = {
    "index", "indexes", "template", "templates", "list", "lists", "notes",
    "note", "misc", "overview", "moc", "map", "archive", "todo", "tmp",
    "scratch", "scratchpad", "inbox", "birthdays", "contacts", "people",
    "person", "family", "friends", "readme",
}
_LOG_SEED_MAX_CHARS = 2400


def ingest_life_sources(
    sources: list[Path],
    *,
    vault: Path,
    db_path: Path | None = None,
    replace: bool = False,
    plan_only: bool = False,
) -> dict[str, Any]:
    """Classify and assimilate. Returns a summary with per-route counts."""
    from .ingest import _reference_files_in_directory

    files: list[Path] = []
    roots: dict[Path, Path] = {}
    for source in sources:
        source = Path(source).resolve()
        if source.is_dir():
            for f in _reference_files_in_directory(source):
                files.append(f)
                roots[f] = source
        elif source.is_file():
            files.append(source)
            roots[source] = source.parent

    routed = _classify(files, roots, vault)
    summary: dict[str, Any] = {
        "classified": {k: len(v) for k, v in routed.items()},
        "entities_created": [],
        "entities_enriched": [],
        "episodes_created": 0,
        "knowledge_documents": 0,
        "knowledge_records": 0,
        "already_ingested": 0,
        "rewrite_jobs": 0,
        "warnings": [],
        "plan_only": plan_only,
    }

    if plan_only:
        summary["would_create_entities"] = [
            {"name": item["name"], "kind": item["kind"]}
            for item in routed["entity"]
            if _find_existing_entity(item["name"], vault) is None
        ]
        return summary

    for item in routed["entity"]:
        try:
            _assimilate_entity_note(item, vault=vault, db_path=db_path, replace=replace, summary=summary)
        except Exception as exc:
            log_error(vault, f"life_ingest entity note failed: {item['path']}", exc)
            summary["warnings"].append(f"entity note failed: {item['path'].name}: {exc}")

    for item in routed["episode"]:
        try:
            _assimilate_episode_note(item, vault=vault, summary=summary)
        except FileExistsError:
            summary["already_ingested"] += 1
        except Exception as exc:
            log_error(vault, f"life_ingest episode note failed: {item['path']}", exc)
            summary["warnings"].append(f"episode note failed: {item['path'].name}: {exc}")

    knowledge_files = [item["path"] for item in routed["knowledge"]]
    if knowledge_files:
        _ingest_knowledge(knowledge_files, vault=vault, db_path=db_path, replace=replace, summary=summary)

    _reindex_all(vault, db_path)
    return summary


# ------------------------------------------------------------- classification

def _classify(files: list[Path], roots: dict[Path, Path], vault: Path) -> dict[str, list[dict[str, Any]]]:
    from .primer_index import principal_aliases

    principal = {a.lower() for a in principal_aliases(vault)}
    routed: dict[str, list[dict[str, Any]]] = {"entity": [], "episode": [], "knowledge": [], "skipped_empty": []}
    for path in files:
        if path.suffix.lower() not in {".md", ".markdown"}:
            routed["knowledge"].append({"path": path})
            continue
        try:
            body_words = len(path.read_text(encoding="utf-8", errors="ignore").split())
        except Exception:
            body_words = 0
        if body_words == 0:
            routed["skipped_empty"].append({"path": path})
            continue

        stem = path.stem.strip()
        if _DATE_TITLE.match(stem):
            routed["episode"].append({"path": path, "date": stem})
            continue

        kind = _kind_from_folders(path, roots.get(path))
        if kind:
            name = _deslug(stem)
            if name.lower() in principal or stem.lower() in principal:
                routed["knowledge"].append({"path": path})  # never a third-party entity
                continue
            if any(tok in _META_TOKENS for tok in name.lower().split()):
                routed["knowledge"].append({"path": path})  # index/template/meta note
                continue
            routed["entity"].append({"path": path, "name": name, "kind": kind})
            continue

        routed["knowledge"].append({"path": path})
    return routed


def _kind_from_folders(path: Path, root: Path | None) -> str | None:
    try:
        parts = path.relative_to(root).parts[:-1] if root else path.parts[:-1]
    except ValueError:
        parts = path.parts[:-1]
    for part in reversed([p.lower() for p in parts]):
        if part in _FOLDER_KIND:
            return _FOLDER_KIND[part]
    return None


def _deslug(stem: str) -> str:
    """``adrienne-mcgraw`` → ``Adrienne Mcgraw``; already-cased names pass
    through. Usernames (``babylulu99``) stay single tokens, capitalized —
    they are still someone the owner knows by that handle."""
    name = re.sub(r"[-_]+", " ", stem).strip()
    name = re.sub(r"\s+", " ", name)
    tokens = [t if (len(t) > 1 and not t.islower()) else t.capitalize() for t in name.split()]
    return " ".join(tokens)


# ------------------------------------------------------------- entity notes

def _find_existing_entity(name: str, vault: Path) -> dict[str, Any] | None:
    from .ingest import _find_entity_by_label, _load_entity_catalog

    return _find_entity_by_label(name, _load_entity_catalog(vault))


def _assimilate_entity_note(
    item: dict[str, Any],
    *,
    vault: Path,
    db_path: Path | None,
    replace: bool,
    summary: dict[str, Any],
) -> None:
    from .record_factory import new_entity

    path: Path = item["path"]
    name: str = item["name"]
    kind: str = item["kind"]

    existing = _find_existing_entity(name, vault)
    if existing is not None:
        entity_id = str(existing.get("id") or "")
        entity_path = _entity_path_for_id(entity_id, vault)
        if entity_path is None:
            summary["warnings"].append(f"entity {entity_id} matched but file not found for {path.name}")
            return
        summary["entities_enriched"].append(name)
    else:
        record = new_entity(
            vault,
            name,
            subtype=kind,
            summary=f"{name}, from {{{{principal}}}}'s notes.",
            aliases=[path.stem] if path.stem.lower() != name.lower() else None,
            confidence="medium",
            confidence_basis="Life ingestion from the owner's notes",
        )
        entity_path = record.path
        entity_id = str(load_markdown(entity_path).frontmatter.get("id") or "")
        summary["entities_created"].append({"name": name, "kind": kind})

    appended = _append_note_to_entity_log(entity_path, path, vault)
    if appended:
        _enqueue_compaction(entity_path, db_path)
        summary["rewrite_jobs"] += 1
    else:
        summary["already_ingested"] += 1

    # the full note text also becomes knowledge, linked to the entity —
    # the log seed is condensed; nothing may be lost to condensation
    _ingest_knowledge([path], vault=vault, db_path=db_path, replace=replace,
                      summary=summary, link_entity_ids=[entity_id] if entity_id else None)


def _entity_path_for_id(entity_id: str, vault: Path) -> Path | None:
    if not entity_id:
        return None
    for path in (vault / "entities").rglob("*.md"):
        try:
            if str(load_markdown(path).frontmatter.get("id") or "") == entity_id:
                return path
        except Exception:
            continue
    return None


def _append_note_to_entity_log(entity_path: Path, note_path: Path, vault: Path) -> bool:
    """Append the note into the entity's durable source_log — the same organ
    conversation captures feed — idempotent by source path + content hash."""
    raw = note_path.read_text(encoding="utf-8", errors="ignore")
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    source_marker = note_path.name
    # the note's own modification date, not the ingestion date — a note
    # written years ago must not read as if it were written today
    try:
        note_date = date.fromtimestamp(note_path.stat().st_mtime).isoformat()
    except Exception:
        note_date = today_iso()

    doc = load_markdown(entity_path)
    fm = dict(doc.frontmatter)
    log = [dict(e) for e in (fm.get("source_log") or []) if isinstance(e, dict)]
    for entry in log:
        if entry.get("source") == source_marker and entry.get("source_hash") == content_hash:
            return False  # this exact note content is already assimilated

    from .ingest import _resolve_wikilinks

    text, _ = _resolve_wikilinks(raw)
    text = re.sub(r"\s+", " ", text).strip()[:_LOG_SEED_MAX_CHARS]
    text = tokenize_principal(text, vault)
    log.append({
        "date": note_date,
        "text": f"(from {{{{principal}}}}'s note '{note_path.stem}', {note_date}) {text}",
        "folded": False,
        "source": source_marker,
        "source_hash": content_hash,
    })
    fm["source_log"] = log
    fm["updated"] = today_iso()
    write_markdown(entity_path, fm, doc.body)
    return True


def _enqueue_compaction(entity_path: Path, db_path: Path | None) -> None:
    from .jobs import enqueue_job

    enqueue_job(
        "entity.rewrite_story",
        {"entity_path": str(entity_path), "force_compact": True},
        db_path=db_path,
    )


# ------------------------------------------------------------- episode notes

def _assimilate_episode_note(item: dict[str, Any], *, vault: Path, summary: dict[str, Any]) -> None:
    from .ingest import _resolve_wikilinks
    from .record_factory import new_episode

    path: Path = item["path"]
    day: str = item["date"]
    raw = path.read_text(encoding="utf-8", errors="ignore")
    text, _ = _resolve_wikilinks(raw)
    text = tokenize_principal(text.strip(), vault)
    first_line = next((l.strip().lstrip("# ") for l in text.splitlines() if l.strip()), "")

    new_episode(
        vault,
        f"note {day}",
        summary=f"From {{{{principal}}}}'s note dated {day}: {first_line[:160]}",
        source="ingestion",
        confidence="medium",
        confidence_basis="The owner's own dated note",
        last_confirmed=day,
    )
    summary["episodes_created"] += 1


# ------------------------------------------------------------- knowledge

def _ingest_knowledge(
    files: list[Path],
    *,
    vault: Path,
    db_path: Path | None,
    replace: bool,
    summary: dict[str, Any],
    link_entity_ids: list[str] | None = None,
) -> None:
    from .ingest import ingest_reference_sources

    for path in files:
        try:
            result = ingest_reference_sources(
                [path], vault=vault, db_path=db_path,
                on_exists="replace" if replace else "abort",
                link_entities=link_entity_ids,
                # entity birth is the classifier's job (or conversation's) —
                # phrase heuristics minted 141 junk orgs on the first real
                # run. Match-only here; one reindex at the end of the run.
                create_entities=False,
                reindex=False,
            )
        except FileExistsError:
            summary["already_ingested"] += 1
            continue
        except Exception as exc:
            log_error(vault, f"life_ingest knowledge failed: {path}", exc)
            summary["warnings"].append(f"knowledge failed: {path.name}: {exc}")
            continue
        summary["knowledge_documents"] += len(result.get("documents") or [])
        summary["knowledge_records"] += len(result.get("created_records") or [])
        summary["warnings"].extend(str(w) for w in (result.get("warnings") or []))


def _reindex_all(vault: Path, db_path: Path | None) -> None:
    try:
        from .rebuild_index import rebuild_index

        rebuild_index(vault=vault, db_path=db_path)
    except Exception as exc:
        log_error(vault, "life_ingest reindex failed", exc)
