"""The drive system, v1: session-open callbacks (Phase 2 WO-5).

Zeigarnik as architecture: open loops in the narrative ARE the motivation.
The deficit scorer ranks unresolved loops; the single delivery seam is
session open (the v1 action budget — queue-for-next-session only), and the
callback is always phrased as a question, because closure detection can be
wrong and epistemic humility in the phrasing buys fault tolerance in the
detection: a wrong question degrades to checking in; a wrong assertion is
a hard frame drop.

Failure-mode defenses, all mechanical:
- *Nagging*: at most one callback per session open; per-loop cooldown
  (default 7 days), stamped into the loop's ``last_callback``.
- *Resurrection*: only ``status: active`` loops are ever considered, and
  the phrasing is interrogative by construction.
- *Immortal tension*: salience decays linearly to zero unless the loop is
  refreshed (its ``updated`` date advances); a loop nobody has touched in
  ``max_age_days`` goes silent on its own.
- *To-do-app smell*: loops linked to a first-person episode (the agent did
  work, made a commitment, was wrong) carry a stake bonus and outrank pure
  user reminders.

Every delivery and suppression is logged with a ``drive.callback.*``
marker — the WO-3 metrics count exactly these.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso
from .log import get_logger, log_error

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "cooldown_days": 7,
    "min_score": 2.0,
    "max_age_days": 45,
}

_SALIENCE = {"low": 1.0, "medium": 2.0, "high": 3.0}


def drive_config(config: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update((config or {}).get("drive") or {})
    return out


def _parse_day(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def loop_score(fm: dict[str, Any], now: date | None = None, *, max_age_days: int = 45) -> float:
    """Deterministic deficit score for one active loop. Zero means silent."""
    now = now or date.today()
    created = _parse_day(fm.get("created")) or now
    updated = _parse_day(fm.get("updated")) or created
    age_days = max(0, (now - created).days)
    staleness = max(0, (now - updated).days)

    decay = max(0.0, 1.0 - staleness / float(max_age_days))
    if decay == 0.0:
        return 0.0
    salience = _SALIENCE.get(str(fm.get("significance") or "low"), 1.0)
    if str(fm.get("priority") or "") == "high":
        salience += 1.0
    stake = 2.0 if any(str(l).startswith("self_episode.") for l in (fm.get("links") or [])) else 0.0
    age_component = min(2.0, age_days / 7.0)  # tension builds over the first two weeks
    return round((salience + stake + age_component) * decay, 3)


def phrase_question(fm: dict[str, Any]) -> str:
    """Interrogative by construction — never an assertion."""
    subject = str(fm.get("summary") or fm.get("title") or "that open thread").strip().rstrip(".!")
    question = f'Earlier you mentioned "{subject}" — did that ever get anywhere?'
    assert question.endswith("?")
    return question


def scored_loops(vault: Path, now: date | None = None, *, max_age_days: int = 45) -> list[dict[str, Any]]:
    """All active loops with their scores, best first."""
    out: list[dict[str, Any]] = []
    root = vault / "open_loops"
    if not root.exists():
        return out
    for path in sorted(root.glob("*.md")):
        try:
            fm = dict(load_markdown(path).frontmatter)
        except Exception:
            continue
        if str(fm.get("type") or "") != "open_loop":
            continue
        if str(fm.get("status") or "") != "active":
            continue
        out.append({"path": path, "frontmatter": fm, "score": loop_score(fm, now, max_age_days=max_age_days)})
    out.sort(key=lambda item: (-item["score"], str(item["frontmatter"].get("id") or "")))
    return out


def _in_cooldown(fm: dict[str, Any], now: date, cooldown_days: int) -> bool:
    last = _parse_day(fm.get("last_callback"))
    return last is not None and (now - last).days < cooldown_days


def session_open_callback(
    vault: Path,
    conversation_id: str | None = None,
    *,
    config: dict[str, Any] | None = None,
    now: date | None = None,
) -> str | None:
    """Pick, stamp, and return at most ONE question for a fresh session.
    Returns None when nothing has earned a callback — silence is the
    default, not a failure."""
    cfg = drive_config(config)
    if not cfg.get("enabled", True):
        return None
    now = now or date.today()
    logger = get_logger(vault)
    for item in scored_loops(vault, now, max_age_days=int(cfg["max_age_days"])):
        fm, path, score = item["frontmatter"], item["path"], item["score"]
        loop_id = str(fm.get("id") or path.stem)
        if score < float(cfg["min_score"]):
            break  # sorted: nothing below threshold can follow
        if _in_cooldown(fm, now, int(cfg["cooldown_days"])):
            logger.info(f"drive.callback.suppressed loop={loop_id} reason=cooldown")
            continue
        question = phrase_question(fm)
        try:
            fm["last_callback"] = today_iso()
            doc = load_markdown(path)
            write_markdown(path, {**dict(doc.frontmatter), "last_callback": fm["last_callback"]}, doc.body)
        except Exception as exc:
            log_error(vault, f"drive.callback stamp failed for {loop_id}", exc)
            return None  # never deliver what we could not stamp — that way lies nagging
        logger.info(
            f"drive.callback.delivered loop={loop_id} score={score} conversation={conversation_id or '-'}"
        )
        return question
    return None
