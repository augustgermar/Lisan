"""Learned edges: retrieval that learns from its own usage, deterministically.

Every retrieval call already logs which records were loaded together
(``retrieval_log.files_loaded``). This module mines that co-selection
history into an association graph: records that keep appearing in the same
retrieval results are behaviorally related even when no authored link or
lexical/semantic overlap records the relationship (borrowed from the
vellum-assistant review, item 2 — their co-selection NPMI graph).

Scoring is normalized pointwise mutual information (NPMI in [-1, 1]):

    pmi(a,b)  = log( p(a,b) / (p(a) p(b)) )
    npmi(a,b) = pmi(a,b) / -log p(a,b)

NPMI discounts ubiquitous records naturally — the primer-adjacent items
that co-occur with everything score near zero. Pairs already present in
the authored ``links`` table are excluded: the learned graph records only
what the authored graph does not.

Everything here is deterministic given the log contents: mining is a
counting pass (no model), rebuilt by ``lisan sync``, and the retrieval
lane it feeds only ever ADDS candidates to RRF fusion.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import sqlite_path

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "window": 2000,       # most recent retrieval events mined
    "min_co": 3,          # pair must co-occur at least this often
    "min_npmi": 0.30,
    "max_partners": 8,    # per record, keep only the strongest edges
    "seed_count": 3,      # retrieval: top-k user-lane candidates used as seeds
    "lane_limit": 5,      # retrieval: max learned-edge candidates per turn
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS learned_edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    npmi REAL NOT NULL,
    co_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_learned_edges_source ON learned_edges(source_id, npmi);
"""


def learned_edges_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update(((config or {}).get("retrieval") or {}).get("learned_edges") or {})
    return out


def ensure_learned_edges_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def mine_learned_edges(
    db_path: Path | None = None,
    *,
    window: int | None = None,
    min_co: int | None = None,
    min_npmi: float | None = None,
    max_partners: int | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Counting pass over the recent retrieval log → replace learned_edges.

    Idempotent and deterministic for a given log state.
    """
    settings = learned_edges_settings(config)
    window = window if window is not None else int(settings["window"])
    min_co = min_co if min_co is not None else int(settings["min_co"])
    min_npmi = min_npmi if min_npmi is not None else float(settings["min_npmi"])
    max_partners = max_partners if max_partners is not None else int(settings["max_partners"])

    db = db_path or sqlite_path()
    conn = sqlite3.connect(db)
    try:
        ensure_learned_edges_table(conn)
        try:
            rows = conn.execute(
                "SELECT files_loaded FROM retrieval_log WHERE files_loaded IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (window,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        events: list[set[str]] = []
        for (raw,) in rows:
            try:
                loaded = json.loads(raw or "[]")
            except json.JSONDecodeError:
                continue
            ids = {str(i) for i in loaded if str(i).strip()}
            if len(ids) >= 2:
                events.append(ids)

        n_events = len(events)
        occurrences: dict[str, int] = {}
        co: dict[tuple[str, str], int] = {}
        for ids in events:
            ordered = sorted(ids)
            for record_id in ordered:
                occurrences[record_id] = occurrences.get(record_id, 0) + 1
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    co[(a, b)] = co.get((a, b), 0) + 1

        authored: set[tuple[str, str]] = set()
        try:
            for source, target in conn.execute("SELECT source_id, target_id FROM links"):
                pair = tuple(sorted((str(source), str(target))))
                authored.add(pair)  # type: ignore[arg-type]
        except sqlite3.OperationalError:
            pass

        edges: dict[str, list[tuple[str, float, int]]] = {}
        kept_pairs = 0
        if n_events:
            for (a, b), count in co.items():
                if count < min_co or (a, b) in authored:
                    continue
                p_ab = count / n_events
                p_a = occurrences[a] / n_events
                p_b = occurrences[b] / n_events
                if p_ab >= 1.0:  # co-occur in every event: no information
                    continue
                npmi = math.log(p_ab / (p_a * p_b)) / -math.log(p_ab)
                if npmi < min_npmi:
                    continue
                kept_pairs += 1
                edges.setdefault(a, []).append((b, npmi, count))
                edges.setdefault(b, []).append((a, npmi, count))

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        conn.execute("DELETE FROM learned_edges")
        inserted = 0
        for source_id, partners in edges.items():
            partners.sort(key=lambda item: (-item[1], item[0]))
            for target_id, npmi, count in partners[:max_partners]:
                conn.execute(
                    "INSERT OR REPLACE INTO learned_edges "
                    "(source_id, target_id, npmi, co_count, updated_at) VALUES (?,?,?,?,?)",
                    (source_id, target_id, round(npmi, 4), count, now),
                )
                inserted += 1
        conn.commit()
        return {"events": n_events, "pairs_kept": kept_pairs, "edges_written": inserted}
    finally:
        conn.close()


def learned_partners(
    conn: sqlite3.Connection,
    seed_ids: list[str],
    *,
    limit: int,
    exclude: set[str] | None = None,
) -> list[tuple[str, float]]:
    """Strongest learned partners of the seed set, deduped, best-first.
    Deterministic: ties break on id."""
    if not seed_ids or limit <= 0:
        return []
    exclude = exclude or set()
    best: dict[str, float] = {}
    try:
        placeholders = ",".join("?" * len(seed_ids))
        for target_id, npmi in conn.execute(
            f"SELECT target_id, npmi FROM learned_edges WHERE source_id IN ({placeholders})",
            [str(s) for s in seed_ids],
        ):
            target = str(target_id)
            if target in exclude or target in seed_ids:
                continue
            if npmi > best.get(target, float("-inf")):
                best[target] = float(npmi)
    except sqlite3.OperationalError:
        return []
    ranked = sorted(best.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:limit]
