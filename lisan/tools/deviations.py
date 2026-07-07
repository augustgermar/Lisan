"""Deviation-sourced drive: the agent's own aches about its own model.

Scans the vault for deviations in the agent's model of the owner's world and
of itself, and emits ``origin: self`` open loops — the same first-class type
as owner-sourced loops, scored by the same ``drive.loop_score``. The subject
of a self-loop is the state of the model ("two entities share one name",
"this link points nowhere", "this person is thin but keeps coming up"),
never a task handed down and never a third party as a standalone subject.

Everything here is deterministic — pure code over the vault and the index,
no LLM, no network. Interior-only by construction: detection reads the
vault; closure (satiation) happens when the deviation stops being true.

The governing metaphor is the mosquito: the ache is real, bounded to a
specific deficit, and satiable — when a scan finds a previously-reported
deviation gone, it resolves the loop itself and the ache quiesces. The
bounded-appetite dial is mechanical: a hard daily cap on new self-loops
(shipped low) and per-class significance so only genuine deficits surface.

Deviation classes (WO-ENRICH §1.2), all inward-pointing:
- ``cross_kind``  — one canonical name, multiple kinds (a fragmented model)
- ``dangling``    — a link that resolves to nothing (an unresolved reference)
- ``thin``        — a person the owner keeps mentioning but the model barely
                    knows (detected only when mentions outpace the story)
- ``stale``       — a high-significance model nobody has updated in weeks
- ``interocept``  — the machine's own condition: failed-job clusters,
                    an embedding backlog (zero privacy surface)
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any
from .db import connect as _db_connect

from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso
from .deixis import tokenize_principal
from .log import get_logger, log_error

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # punishingly conservative on purpose: a curious mind asks one good
    # question a day; a grinder files forty tickets a night (WO §1.7)
    "daily_cap": 2,
    "thin_person_max_words": 25,
    "thin_person_min_mentions": 3,
    "stale_after_days": 30,
    "failed_jobs_threshold": 5,
    "embedding_backlog_threshold": 25,
}

_SIGNIFICANCE = {
    "cross_kind": "high",
    "near_dup": "medium",
    "dangling": "medium",
    "thin": "medium",
    "interocept": "medium",
    "stale": "low",
}
# emission order under the cap: model contradictions first, housekeeping last
_CLASS_ORDER = ["cross_kind", "near_dup", "interocept", "thin", "dangling", "stale"]


def deviations_config(config: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update((config or {}).get("deviations") or {})
    return out


def scan_deviations(
    vault: Path,
    *,
    db_path: Path | None = None,
    config: dict[str, Any] | None = None,
    now: date | None = None,
) -> dict[str, Any]:
    """One full pass: detect, satiate, emit (capped). Returns a summary."""
    cfg = deviations_config(config)
    if not cfg.get("enabled", True):
        return {"enabled": False, "detected": 0, "emitted": 0, "satiated": 0}
    now = now or date.today()

    current = detect(vault, db_path=db_path, config=cfg)
    satiated = _satiate(vault, {d["fingerprint"] for d in current}, db_path=db_path)
    emitted = _emit(vault, current, cfg, now, db_path=db_path)
    return {
        "enabled": True,
        "detected": len(current),
        "emitted": len(emitted),
        "satiated": satiated,
        "emitted_ids": emitted,
    }


def detect(vault: Path, *, db_path: Path | None = None, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """All currently-true deviations, deterministically. Each carries a
    stable fingerprint so re-scans are idempotent."""
    cfg = deviations_config(config)
    found: list[dict[str, Any]] = []
    entities = _load_entities(vault)
    found.extend(_cross_kind_duplicates(entities))
    found.extend(_near_duplicates(vault))
    found.extend(_dangling_links(vault))
    found.extend(_thin_persons(entities, db_path=db_path, cfg=cfg))
    found.extend(_stale_entities(entities, cfg=cfg))
    found.extend(_interoception(vault, db_path=db_path, cfg=cfg))
    order = {name: i for i, name in enumerate(_CLASS_ORDER)}
    # within a class, the strongest signal aches first — under a daily cap,
    # ordering IS the appetite
    found.sort(key=lambda d: (order.get(d["klass"], 99), -float(d.get("weight") or 0), d["fingerprint"]))
    return found


# ---------------------------------------------------------------- detectors

def _load_entities(vault: Path) -> list[dict[str, Any]]:
    out = []
    root = vault / "entities"
    if not root.exists():
        return out
    for path in sorted(root.rglob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        fm = dict(doc.frontmatter)
        body = re.sub(r"^#\s+.*$", "", doc.body, count=1, flags=re.M).strip()
        out.append({
            "path": path,
            "name": str(fm.get("canonical_name") or "").strip(),
            "kind": str(fm.get("kind") or fm.get("subtype") or "").strip(),
            "significance": str(fm.get("significance") or "low"),
            "updated": str(fm.get("updated") or fm.get("created") or ""),
            "words": len(body.split()),
            "rel": str(path.relative_to(vault)),
        })
    return out


def _cross_kind_duplicates(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entities:
        if e["name"]:
            by_name[e["name"].lower()].append(e)
    out = []
    for name, group in sorted(by_name.items()):
        kinds = sorted({e["kind"] for e in group if e["kind"]})
        if len(group) > 1 and len(kinds) > 1:
            out.append({
                "klass": "cross_kind",
                "fingerprint": f"cross-kind-{_slug(name)}",
                "summary": (
                    f"my model holds {len(group)} different entities all named '{name}' "
                    f"({', '.join(kinds)}) — probably one thing fragmented"
                ),
                "links": [e["rel"] for e in group],
            })
    return out


def _near_duplicates(vault: Path) -> list[dict[str, Any]]:
    """Same-kind name near-duplicates ('deck rebuild' vs 'deck rebuild
    project (summer 2026)'). Never merged automatically — each becomes a
    question the drive can ask, per the surface-with-curiosity policy."""
    try:
        from .entity_merge import dedup_candidates
    except Exception:
        return []
    out = []
    for c in dedup_candidates(vault):
        pair = "-".join(sorted([_slug(c["keep"]), _slug(c["absorb"])]))[:80]
        out.append({
            "klass": "near_dup",
            "fingerprint": f"near-dup-{pair}",
            "summary": (
                f"'{c['absorb']}' and '{c['keep']}' look like the same {c['kind']} "
                f"({c['why']}) — worth merging into one"
            ),
            "links": [],
        })
    return out


def _dangling_links(vault: Path) -> list[dict[str, Any]]:
    """Links that resolve to nothing — references the model never bound.

    open_loops are deliberately not scanned: a loop's links point at the
    history that raised it, and that history going away is normal lifecycle
    — including for our own emitted self-loops, whose subject records get
    fixed. Scanning them made satiation spawn a follow-up ache about the
    very loop it had just resolved (a self-loop chain reaction, found by
    the satiation test)."""
    out = []
    seen: set[str] = set()
    for root in ("entities", "claims", "decisions", "knowledge"):
        base = vault / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.md")):
            try:
                fm = dict(load_markdown(path).frontmatter)
            except Exception:
                continue
            for link in fm.get("links") or []:
                link = str(link).strip()
                if not link or "/" not in link:
                    continue  # id-style links are checked against nothing here
                if (vault / link).exists():
                    continue
                fp = f"dangling-{_slug(Path(link).stem)}"
                if fp in seen:
                    continue
                seen.add(fp)
                rel = str(path.relative_to(vault))
                out.append({
                    "klass": "dangling",
                    "fingerprint": fp,
                    "summary": f"a record of mine ({rel}) links to '{link}', which doesn't exist — a reference I never resolved",
                    "links": [rel],
                })
    return out


def _thin_persons(entities: list[dict[str, Any]], *, db_path: Path | None, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """A person the owner keeps bringing up, about whom the model holds
    almost nothing. Requires the index for mention counts; without it,
    detects nothing (never guess)."""
    if db_path is None or not Path(db_path).exists():
        return []
    out = []
    max_words = int(cfg["thin_person_max_words"])
    min_mentions = int(cfg["thin_person_min_mentions"])
    try:
        conn = _db_connect(db_path)
    except Exception:
        return []
    try:
        for e in entities:
            if e["kind"] != "person" or not e["name"] or e["words"] > max_words:
                continue
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM files_fts WHERE files_fts MATCH ?",
                    ('"' + e["name"].replace('"', " ") + '"',),
                ).fetchone()
            except sqlite3.OperationalError:
                continue
            mentions = int(row[0]) if row else 0
            if mentions >= min_mentions:
                out.append({
                    "klass": "thin",
                    "weight": mentions,
                    "fingerprint": f"thin-{_slug(e['name'])}",
                    "summary": (
                        f"{e['name']} keeps coming up ({mentions} records) but my model of them "
                        f"is {e['words']} words — there is clearly more to know"
                    ),
                    "links": [e["rel"]],
                })
    finally:
        conn.close()
    return out


def _stale_entities(entities: list[dict[str, Any]], *, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    stale_days = int(cfg["stale_after_days"])
    today = date.today()
    out = []
    for e in entities:
        if e["significance"] != "high" or not e["updated"]:
            continue
        try:
            updated = date.fromisoformat(e["updated"][:10])
        except ValueError:
            continue
        gap = (today - updated).days
        if gap >= stale_days:
            out.append({
                "klass": "stale",
                "fingerprint": f"stale-{_slug(e['name'] or e['path'].stem)}",
                "summary": (
                    f"{e['name'] or e['path'].stem} matters (high significance) but my model of it "
                    f"hasn't moved in {gap} days — it may have drifted from reality"
                ),
                "links": [e["rel"]],
            })
    return out


def _interoception(vault: Path, *, db_path: Path | None, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Own-state deviations: the machine aching about its own condition."""
    if db_path is None or not Path(db_path).exists():
        return []
    out = []
    try:
        conn = _db_connect(db_path)
    except Exception:
        return []
    try:
        try:
            failed = int(conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'failed'").fetchone()[0])
        except sqlite3.OperationalError:
            failed = 0
        if failed >= int(cfg["failed_jobs_threshold"]):
            out.append({
                "klass": "interocept",
                "fingerprint": "interocept-failed-jobs",
                "summary": f"{failed} of my background jobs sit failed — part of my own machinery isn't working",
                "links": [],
            })
        try:
            backlog = int(conn.execute(
                "SELECT COUNT(*) FROM files WHERE embedding_status = 'pending'"
            ).fetchone()[0])
        except sqlite3.OperationalError:
            backlog = 0
        if backlog >= int(cfg["embedding_backlog_threshold"]):
            out.append({
                "klass": "interocept",
                "fingerprint": "interocept-embedding-backlog",
                "summary": f"{backlog} records await embedding — my semantic recall is running partially blind",
                "links": [],
            })
    finally:
        conn.close()
    return out


# ------------------------------------------------------- satiation + emission

def _self_loops(vault: Path) -> list[tuple[Path, dict[str, Any]]]:
    root = vault / "open_loops"
    if not root.exists():
        return []
    out = []
    for path in sorted(root.glob("*.md")):
        try:
            fm = dict(load_markdown(path).frontmatter)
        except Exception:
            continue
        if str(fm.get("origin") or "") == "self":
            out.append((path, fm))
    return out


def _satiate(vault: Path, current_fingerprints: set[str], *, db_path: Path | None) -> int:
    """A previously-reported deviation that is no longer true closes its own
    loop. This IS the drive being satiable — nobody has to answer a question
    about an ache that healed."""
    logger = get_logger(vault)
    resolved = 0
    for path, fm in _self_loops(vault):
        if str(fm.get("status") or "") != "active":
            continue
        fp = str(fm.get("deviation_fingerprint") or "")
        if not fp or fp in current_fingerprints:
            continue
        doc = load_markdown(path)
        write_markdown(path, {
            **dict(doc.frontmatter),
            "status": "resolved",
            "updated": today_iso(),
            "resolved_at": today_iso(),
            "resolved_by": "deviation.scan",
            "resolved_note": "deviation no longer detected",
        }, doc.body)
        _index_quietly(path, vault, db_path)
        logger.info(f"deviation.satiated fingerprint={fp} loop={fm.get('id')}")
        resolved += 1
    return resolved


def _emit(
    vault: Path,
    current: list[dict[str, Any]],
    cfg: dict[str, Any],
    now: date,
    *,
    db_path: Path | None,
) -> list[str]:
    logger = get_logger(vault)
    existing = {str(fm.get("deviation_fingerprint") or "") for _, fm in _self_loops(vault)}
    today = now.isoformat()
    made_today = sum(
        1 for _, fm in _self_loops(vault) if str(fm.get("created") or "") == today
    )
    cap = int(cfg["daily_cap"])
    emitted: list[str] = []

    root = vault / "open_loops"
    root.mkdir(parents=True, exist_ok=True)
    for dev in current:
        if made_today + len(emitted) >= cap:
            break
        if dev["fingerprint"] in existing:
            continue  # already aching, or already answered — never re-file
        loop_id = f"open_loop.deviation-{dev['fingerprint']}"
        path = root / f"{today}-deviation-{dev['fingerprint']}.md"
        summary = tokenize_principal(str(dev["summary"]), vault)
        fm = {
            "id": loop_id,
            "type": "open_loop",
            "origin": "self",
            "deviation_fingerprint": dev["fingerprint"],
            "deviation_class": dev["klass"],
            "created": today,
            "updated": today,
            "status": "active",
            "significance": _SIGNIFICANCE.get(dev["klass"], "low"),
            "domain_primary": "cross_arena",
            "domain_secondary": [],
            "privacy": "personal",
            "disclosure": "private",
            "summary": summary,
            "links": list(dev.get("links") or []),
            "confidence": "high",
            "confidence_basis": "Deterministic deviation scan",
            "last_confirmed": today,
            "review_after": today,
            "priority": "normal",
            "owner": "agent",
            "next_action": "",
            "blocked_by": [],
        }
        body = f"# Deviation: {dev['fingerprint']}\n\n{summary}\n"
        try:
            write_markdown(path, fm, body)
            _index_quietly(path, vault, db_path)
        except Exception as exc:
            log_error(vault, f"deviation.emit failed for {dev['fingerprint']}", exc)
            continue
        logger.info(f"deviation.emitted class={dev['klass']} fingerprint={dev['fingerprint']}")
        emitted.append(loop_id)
    return emitted


def _index_quietly(path: Path, vault: Path, db_path: Path | None) -> None:
    try:
        from .rebuild_index import index_single_record, open_index_connection

        conn = open_index_connection(db_path)
        try:
            index_single_record(path, vault, conn)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "unnamed"
