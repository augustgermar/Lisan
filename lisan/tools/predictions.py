"""Ship 2 of WO-PSYCHE: the prediction ledger.

A framework or pattern is only as good as its predictions, and the only
honest way to know is to write the expectation down BEFORE the outcome and
score it against what the vault actually recorded afterwards. Three parts:

- ``record_prediction`` — one ledger entry: concrete expectation, trigger
  condition, the source it derives from (a framework or pattern id —
  attribution is mandatory), and a review date. Owner- or agent-initiated,
  over chat or CLI.
- ``run_prediction_reconcile`` — the periodic scoring pass (job type
  ``prediction.reconcile``, daily off the post-turn seam). Due predictions
  are judged hit / miss / unclear against records written after them, with
  the evidence cited. Deterministic gates: cited ids must exist in the
  evidence pool; hit and miss require at least one; a verdict that fails a
  gate degrades to an unclear attempt. Scoring is idempotent — a scored
  prediction is never touched again, and unclear defers with a bounded
  number of retries before it finalizes.
- ``rollup_calibration`` — scores roll up to the source as a derived,
  recomputed view (``prediction_calibration`` on the source's frontmatter),
  rendered wherever the source is retrieved. Calibration is the honest
  form of "the system knows things the owner doesn't": a source that
  predicts well earns standing, one that keeps being surprised loses it,
  and the agent says so plainly when asked.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..paths import sqlite_path, vault_root
from ..utils import today_iso
from .db import connect as _db_connect
from .log import get_logger
from .rebuild_index import reindex_record

# An unclear verdict defers the review a week, this many times, before it
# finalizes as unclear. Without a bound, a vague prediction would be
# re-judged forever; with one, "the record never settled it" becomes the
# recorded outcome, which is itself calibration data.
MAX_SCORE_ATTEMPTS = 3
DEFER_DAYS = 7

_SOURCE_TYPES = {"pattern", "knowledge"}
_POOL_LIMIT = 20


# ── Source resolution ────────────────────────────────────────────────────────

def _resolve_source(vault: Path, source_id: str, db_path: Path | None = None) -> tuple[Path, str] | None:
    """Resolve a source id to (path, type). Only frameworks (knowledge) and
    patterns may source predictions — that is the WO's provenance model."""
    source_id = str(source_id or "").strip()
    if not source_id:
        return None
    try:
        conn = _db_connect(db_path or sqlite_path(), readonly=True)
        try:
            row = conn.execute(
                "SELECT path, type FROM files WHERE id = ? LIMIT 1", (source_id,)
            ).fetchone()
            if row and str(row["type"]) in _SOURCE_TYPES:
                path = vault / str(row["path"])
                if path.exists():
                    return path, str(row["type"])
        finally:
            conn.close()
    except Exception:
        pass
    # Fallback: scan the two source directories (fresh vaults, unindexed records).
    for root in (vault / "patterns", vault / "knowledge"):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                fm = load_markdown(path).frontmatter
            except Exception:
                continue
            if str(fm.get("id") or "") == source_id and str(fm.get("type") or "") in _SOURCE_TYPES:
                return path, str(fm.get("type"))
    return None


def _parse_review_date(value: str) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    try:
        from .scheduler import parse_when

        return parse_when(value).astimezone().date().isoformat()
    except Exception:
        return None


# ── Creation ─────────────────────────────────────────────────────────────────

def record_prediction(
    vault: Path,
    expectation: str,
    *,
    source: str,
    review_after: str,
    trigger: str = "",
    subject: str | None = None,
    confidence: float = 0.5,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """One ledger entry. Refuses missing/unknown sources rather than minting
    unattributed expectations, and refuses clinical-label language outright
    (WO-PSYCHE §1 rule 2 — the ledger inherits the hypothesis discipline)."""
    from .epistemic import hypothesis_gate_terms
    from .record_factory import new_prediction

    expectation = str(expectation or "").strip()
    if not expectation:
        return {"ok": False, "error": "empty expectation — a prediction states what is expected to happen"}
    lowered = f"{expectation} {trigger or ''}".lower()
    banned = [term for term in hypothesis_gate_terms() if term in lowered]
    if banned:
        return {
            "ok": False,
            "error": (
                f"the hypothesis language gate refused terms: {', '.join(sorted(banned))}. "
                "Rephrase, or edit psyche.banned_hypothesis_terms in config.json "
                "(an empty list disables the gate)"
            ),
        }
    resolved = _resolve_source(vault, source, db_path=db_path)
    if resolved is None:
        return {
            "ok": False,
            "error": (
                f"source {source!r} does not resolve to a framework or pattern record — "
                "a prediction must derive from a named, existing source"
            ),
        }
    review = _parse_review_date(review_after)
    if review is None:
        return {"ok": False, "error": f"could not parse review date {review_after!r}; use YYYY-MM-DD or a relative offset like '+7d'"}
    if review <= today_iso():
        return {"ok": False, "error": f"review date {review} is not in the future — a prediction is judged later, not now"}

    source_path, source_type = resolved
    source_id = str(load_markdown(source_path).frontmatter.get("id") or source)
    try:
        created = new_prediction(
            vault,
            expectation,
            source_id=source_id,
            review_after=review,
            trigger=trigger,
            subject=subject,
            confidence=confidence,
        )
    except FileExistsError as exc:
        return {"ok": False, "error": f"an identical prediction already exists today: {Path(str(exc)).name}"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    reindex_record(created.path, vault, db_path, quiet=True)
    return {
        "ok": True,
        "path": str(created.path),
        "source": source_id,
        "source_type": source_type,
        "review_after": review,
    }


# ── Scoring ──────────────────────────────────────────────────────────────────

def _pending_due(vault: Path, today: str) -> list[Path]:
    root = vault / "predictions"
    if not root.exists():
        return []
    due: list[Path] = []
    for path in sorted(root.glob("*.md")):
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        if str(fm.get("status")) == "pending" and str(fm.get("review_after") or "9999") <= today:
            due.append(path)
    return due


def has_due_predictions(vault: Path) -> bool:
    """Cheap gate for the post-turn job planner."""
    return bool(_pending_due(vault, today_iso()))


def _evidence_pool(vault: Path, fm: dict[str, Any], db_path: Path | None) -> list[dict[str, str]]:
    """Records written AFTER the prediction, relevant to its text. Every
    entry carries the id the model must cite — the deterministic gate
    checks refs against exactly this list."""
    from .retrieval import retrieve_context

    made = str(fm.get("created") or "")
    query = f"{fm.get('expectation') or ''} {fm.get('trigger') or ''} {fm.get('subject') or ''}".strip()
    try:
        result = retrieve_context(query=query, vault=vault, db_path=db_path)
        loaded = list(result.loaded)
    except Exception:
        loaded = []
    pool: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in loaded:
        item_id = str(getattr(item, "id", "") or "")
        if not item_id or item_id in seen or item_id.startswith("prediction."):
            continue
        try:
            item_fm = load_markdown(vault / str(item.path)).frontmatter
        except Exception:
            continue
        stamp = str(item_fm.get("record_date") or item_fm.get("created") or "")
        if not stamp or stamp <= made:
            continue  # evidence must postdate the prediction
        seen.add(item_id)
        pool.append({
            "id": item_id,
            "date": stamp,
            "type": str(item_fm.get("type") or ""),
            "summary": str(item_fm.get("summary") or "")[:300],
        })
        if len(pool) >= _POOL_LIMIT:
            break
    return pool


def _judge(fm: dict[str, Any], pool: list[dict[str, str]], *, vault: Path, provider: str | None, model: str | None) -> dict[str, Any]:
    from ..agents.base import PromptAgent

    class _ReconcileAgent(PromptAgent):
        # Rides the analyst's routing on purpose: prediction scoring is the
        # analyst organ's job (Ship 3 inherits it), and the analyst is
        # never the author of the prediction it scores.
        name = "analyst"
        prompt_file = "prediction_reconcile_v1"
        output_schema_name = "prediction_reconcile_output"

    payload = json.dumps(
        {
            "prediction": {
                "expectation": fm.get("expectation"),
                "trigger": fm.get("trigger"),
                "made_on": fm.get("created"),
                "subject": fm.get("subject") or None,
            },
            "EVIDENCE_POOL": pool,
        },
        indent=2,
        ensure_ascii=True,
    )
    return _ReconcileAgent(vault=vault).run_json(
        payload,
        significance="medium",
        provider=provider,
        model=model,
        provider_error_mode="raise",
        parse_error_mode="raise",
    )


def _apply_unclear(path: Path, fm: dict[str, Any], note: str, today: str) -> str:
    attempts = int(fm.get("score_attempts") or 0) + 1
    fm["score_attempts"] = attempts
    fm["verdict_note"] = note
    fm["updated"] = today
    if attempts >= MAX_SCORE_ATTEMPTS:
        fm["status"] = "scored"
        fm["verdict"] = "unclear"
        fm["scored_at"] = today
        outcome = "unclear_final"
    else:
        next_review = (date.fromisoformat(today) + timedelta(days=DEFER_DAYS)).isoformat()
        fm["review_after"] = next_review
        outcome = "deferred"
    doc = load_markdown(path)
    write_markdown(path, fm, doc.body)
    return outcome


def run_prediction_reconcile(
    *,
    vault: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Score every due pending prediction, once, with the gates applied.
    Idempotent: scored records are never revisited; deferred records are
    not due again until their pushed review date."""
    vault = vault or vault_root()
    logger = get_logger(vault)
    today = today_iso()
    summary = {"due": 0, "hits": 0, "misses": 0, "unclear_final": 0, "deferred": 0, "sources_updated": 0}
    touched_sources: set[str] = set()

    for path in _pending_due(vault, today):
        summary["due"] += 1
        doc = load_markdown(path)
        fm = dict(doc.frontmatter)
        pool = _evidence_pool(vault, fm, db_path)
        if not pool:
            outcome = _apply_unclear(path, fm, "no records postdating the prediction were found at review time", today)
            summary["deferred" if outcome == "deferred" else "unclear_final"] += 1
            reindex_record(path, vault, db_path, quiet=True)
            touched_sources.add(str(fm.get("source_id") or ""))
            logger.info(f"prediction.reconcile id={fm.get('id')} verdict=unclear ({outcome}, empty pool)")
            continue

        result = _judge(fm, pool, vault=vault, provider=provider, model=model)
        verdict = str(result.get("verdict") or "").strip().lower()
        reason = str(result.get("reason") or "").strip()
        pool_ids = {entry["id"] for entry in pool}
        refs = [ref for ref in (result.get("evidence_refs") or []) if str(ref) in pool_ids]

        # Deterministic gates: a hit/miss must cite real, in-pool evidence.
        if verdict in {"hit", "miss"} and refs:
            fm["status"] = "scored"
            fm["verdict"] = verdict
            fm["verdict_evidence"] = refs
            fm["verdict_note"] = reason
            fm["scored_at"] = today
            fm["updated"] = today
            write_markdown(path, fm, doc.body)
            summary["hits" if verdict == "hit" else "misses"] += 1
            logger.info(f"prediction.reconcile id={fm.get('id')} verdict={verdict} refs={len(refs)}")
        else:
            if verdict in {"hit", "miss"}:
                reason = f"{verdict} verdict discarded — cited evidence not in the pool. {reason}".strip()
            outcome = _apply_unclear(path, fm, reason or "reconcile returned unclear", today)
            summary["deferred" if outcome == "deferred" else "unclear_final"] += 1
        reindex_record(path, vault, db_path, quiet=True)
        touched_sources.add(str(fm.get("source_id") or ""))

    for source_id in sorted(s for s in touched_sources if s):
        if rollup_calibration(vault, source_id, db_path=db_path):
            summary["sources_updated"] += 1
    return summary


# ── Rollup ───────────────────────────────────────────────────────────────────

def _ledger_by_source(vault: Path, source_id: str) -> dict[str, int]:
    tally = {"hits": 0, "misses": 0, "unclear": 0, "pending": 0}
    root = vault / "predictions"
    if not root.exists():
        return tally
    for path in sorted(root.glob("*.md")):
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        if str(fm.get("source_id") or "") != source_id:
            continue
        if str(fm.get("status")) == "pending":
            tally["pending"] += 1
        elif str(fm.get("verdict")) == "hit":
            tally["hits"] += 1
        elif str(fm.get("verdict")) == "miss":
            tally["misses"] += 1
        elif str(fm.get("verdict")) == "unclear":
            tally["unclear"] += 1
    return tally


def calibration_standing(tally: dict[str, int]) -> str:
    """Plain words for a source's predictive record. Conservative: no
    standing is claimed in either direction until three settled verdicts."""
    settled = tally["hits"] + tally["misses"]
    if settled == 0:
        return "unproven"
    if settled < 3:
        return "early"
    rate = tally["hits"] / settled
    if rate >= 0.7:
        return "predicting well"
    if rate <= 0.3:
        return "keeps being surprised"
    return "mixed"


def rollup_calibration(vault: Path, source_id: str, *, db_path: Path | None = None) -> bool:
    """Recompute the source's calibration from the full ledger — a derived
    view, idempotent by construction, written to the source frontmatter
    where retrieval renders it alongside the hypothesis itself."""
    resolved = _resolve_source(vault, source_id, db_path=db_path)
    if resolved is None:
        return False
    source_path, _ = resolved
    tally = _ledger_by_source(vault, source_id)
    doc = load_markdown(source_path)
    fm = dict(doc.frontmatter)
    fm["prediction_calibration"] = {
        **tally,
        "standing": calibration_standing(tally),
        "updated": today_iso(),
    }
    write_markdown(source_path, fm, doc.body)
    reindex_record(source_path, vault, db_path, quiet=True)
    return True


# ── Views ────────────────────────────────────────────────────────────────────

def list_predictions(vault: Path, *, include_scored: bool = True) -> list[dict[str, Any]]:
    root = vault / "predictions"
    out: list[dict[str, Any]] = []
    if not root.exists():
        return out
    for path in sorted(root.glob("*.md")):
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        if not include_scored and str(fm.get("status")) != "pending":
            continue
        out.append({
            "id": str(fm.get("id") or path.stem),
            "status": str(fm.get("status") or ""),
            "verdict": str(fm.get("verdict") or ""),
            "expectation": str(fm.get("expectation") or ""),
            "source_id": str(fm.get("source_id") or ""),
            "review_after": str(fm.get("review_after") or ""),
            "scored_at": str(fm.get("scored_at") or ""),
        })
    return out


def format_prediction_list(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No predictions on the ledger."
    lines = []
    for entry in entries:
        state = entry["verdict"] or entry["status"]
        when = entry["scored_at"] or f"review {entry['review_after']}"
        text = entry["expectation"]
        if len(text) > 70:
            text = text[:67] + "..."
        lines.append(f"[{state}] {text}  ({entry['source_id']}, {when})")
    return "\n".join(lines)
