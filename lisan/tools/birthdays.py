"""Birthdays: an annual reminder, and a fact the entity actually keeps.

Two halves, deliberately separable. The reminder is scheduling — it rides the
ordinary durable task scheduler, no parallel machinery. The birthday itself is
*memory*: a person Lisan already knows gains a durable, structured fact about
who they are, which is the difference between an alarm clock and an assistant
that knows when your daughter's birthday is.

Where the fact lands matters. An entity's prose narrative is a **regenerable
view** — ``entity_story`` compacts it from an append-only ``source_log``, so
anything written straight into the body is dropped at the next compaction. So a
birthday is written twice, to two seams that keep it:

- ``birthday`` in frontmatter: structured, queryable without an LLM, and the
  thing a future "whose birthday is next?" reads. Deterministic-first.
- one ``source_log`` entry: the durable ground truth compaction reads, so the
  fact reaches the narrative prose the next time the story is retold, and is
  searchable immediately either way.

A conflicting birthday is never silently overwritten. Two different dates for
one person is a contradiction the owner should see, not a race the last writer
wins.
"""
from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso
from .scheduler import _local_tz, _now_utc, schedule_task

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

# Feb 29 exists one year in four. Everything here that could silently pick the
# wrong day funnels through _observed_day.
_LEAP_DAY = (2, 29)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _birthday_date(value: Any) -> date:
    """Parse an ISO birthday, rejecting anything that is not exactly YYYY-MM-DD."""
    text = str(value or "").strip()
    # An explicit digit pattern, not a length check: "2090-W12-1" is ten
    # characters with two hyphens, and date.fromisoformat accepts ISO week
    # dates on modern Pythons, so a length test lets it through as March 23.
    if not _ISO_DATE_RE.match(text):
        raise ValueError(f"invalid birthday date {value!r}; use YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"invalid birthday date {value!r}; use YYYY-MM-DD") from None


def _observed_day(birthday: date, year: int) -> date:
    """The date a birthday falls on in ``year``.

    February 29 is observed on February 28 in common years — and on February 29
    itself in leap years, which the previous implementation got wrong: it forced
    the 28th in *every* year and then subtracted a day for the reminder, so a
    leap-day child was reminded about on the 27th, two days early, and
    indistinguishably from a February 28 birthday.
    """
    if (birthday.month, birthday.day) == _LEAP_DAY:
        return date(year, 2, 29 if monthrange(year, 2)[1] == 29 else 28)
    return date(year, birthday.month, min(birthday.day, monthrange(year, birthday.month)[1]))


def _reminder_plan(birthday: date, *, now: datetime, reminder_hour: int = 9) -> tuple[datetime, str, str]:
    """Next reminder, its annual rule, and the message. All three, together.

    The message never claims "tomorrow". A recurrence is a fixed ``annual@MM-DD``
    string chosen once, but "the day before" is not a fixed calendar date for
    every birthday: the day before March 1 is February 29 in leap years and
    February 28 otherwise, and February 29 has the same problem from the other
    side. Baking "tomorrow" into a message stored once would make it a lie in
    one year out of four. Stating the date is true in every year, and is the
    information the reader actually wants.

    (The general fix is an offset-aware recurrence — ``annual@03-01@09:00-1d``,
    resolved against the year at fire time. That changes the recurrence grammar
    for every recurring task in the system, so it is deliberately not attempted
    here alongside a feature that does not need it.)
    """
    local_now = now.astimezone(_local_tz())
    label = f"{_MONTHS[birthday.month - 1]} {birthday.day}"
    leap_day = (birthday.month, birthday.day) == _LEAP_DAY
    if leap_day:
        message = f"{{person}}'s birthday is {label} — observed February 28 in common years."
    else:
        message = f"{{person}}'s birthday is {label}."

    year = local_now.year
    while True:
        if leap_day:
            # Pinned to February 28 in EVERY year, not derived from whichever
            # year this happened to be scheduled in. "The day before February
            # 29" is not a fixed calendar date, so deriving it per-year made the
            # stored rule depend on the run date: scheduling in a common year
            # produced annual@02-27, scheduling in a leap year produced
            # annual@02-28, and the first was then two days early forever after.
            # February 28 is the eve in leap years and the observed day itself
            # in common ones — the message states the real date either way, so
            # neither reading misleads.
            reminder_day = date(year, 2, 28)
        else:
            reminder_day = _observed_day(birthday, year) - timedelta(days=1)
        candidate = datetime.combine(reminder_day, time(reminder_hour), tzinfo=_local_tz())
        if candidate > local_now:
            rule = f"annual@{reminder_day.month:02d}-{reminder_day.day:02d}@{reminder_hour:02d}:00"
            return candidate, rule, message
        year += 1


# ── the fact, on the entity ───────────────────────────────────────────────────

def record_birthday_on_entity(
    vault: Path,
    person: str,
    birthday: date,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Write the birthday to the person's entity: frontmatter + durable log.

    Returns a summary dict — never raises for a missing person. A birthday
    worth remembering should not fail to schedule because the vault has no
    record of them yet; the reminder still lands and the caller is told the
    entity was not touched.

    Deliberately does NOT create an entity. Entity birth runs through the
    capture pipeline's kind/subtype/tokenization gates, and minting a person
    record from a bare name typed at a CLI flag would route around all of them.
    """
    from .checkin import resolve_checkin_subject

    entity_path, candidates = resolve_checkin_subject(vault, person, db_path=db_path)
    if entity_path is None:
        return {
            "entity_updated": False,
            "reason": "ambiguous" if candidates else "no_entity",
            "candidates": candidates,
        }

    doc = load_markdown(entity_path)
    fm = dict(doc.frontmatter)
    iso = birthday.isoformat()
    existing = str(fm.get("birthday") or "").strip()

    if existing == iso:
        return {"entity_updated": False, "reason": "already_recorded",
                "entity_id": fm.get("id"), "entity_path": str(entity_path)}
    if existing:
        # Two dates for one person is a contradiction, not a race. Refuse and
        # report; the owner decides which is true.
        return {"entity_updated": False, "reason": "conflict", "existing": existing,
                "proposed": iso, "entity_id": fm.get("id"), "entity_path": str(entity_path)}

    fm["birthday"] = iso
    fm["updated"] = today_iso()

    # The durable seam. Prose written into the body would be dropped by the
    # next compaction; a source_log entry is ground truth, is indexed
    # immediately, and is what compaction re-tells the narrative from.
    label = f"{_MONTHS[birthday.month - 1]} {birthday.day}, {birthday.year}"
    entry_text = f"Birthday recorded: {label}."
    log = [dict(e) for e in (fm.get("source_log") or []) if isinstance(e, dict)]
    if not any(str(e.get("text") or "") == entry_text for e in log):
        log.append({"date": today_iso(), "text": entry_text, "folded": False})
    fm["source_log"] = log

    write_markdown(entity_path, fm, doc.body)
    try:
        from .rebuild_index import reindex_record

        reindex_record(entity_path, vault=vault, db_path=db_path)
    except Exception:
        pass  # an index that lags is recoverable; a lost fact is not

    return {"entity_updated": True, "entity_id": fm.get("id"),
            "entity_path": str(entity_path), "birthday": iso}


# ── the reminder ──────────────────────────────────────────────────────────────

def _existing_reminder(rule: str, message: str, db_path: Path | None) -> str | None:
    """The id of a queued reminder that already says this, or None.

    ``task.reminder`` is in the scheduler's NO_COALESCE set on purpose — "every
    scheduled task is a distinct commitment; two reminders must never merge into
    one" — so the generic coalescing machinery must not be borrowed here.
    Dedup belongs to this feature, which knows that two annual reminders naming
    the same person on the same day are the same commitment, not two.
    """
    from .db import connect as db_connect

    try:
        conn = db_connect(db_path)
    except Exception:
        return None
    try:
        rows = conn.execute(
            "SELECT id, payload_json FROM jobs "
            "WHERE job_type = 'task.reminder' AND status IN ('queued','running') "
            "AND recurrence = ?",
            (rule,),
        ).fetchall()
    except Exception:
        return None
    finally:
        conn.close()

    for row in rows:
        try:
            payload = json.loads(row[1] or "{}")
        except Exception:
            continue
        body = payload.get("message") or payload.get("text") or ""
        if str(body).strip() == message:
            return str(row[0])
    return None


def schedule_birthday_reminders(
    birthdays: list[dict[str, Any]],
    *,
    db_path: Path | None = None,
    vault: Path | None = None,
    chat_id: int | None = None,
    reminder_hour: int = 9,
) -> list[dict[str, Any]]:
    """Schedule one annual reminder per ``{"person", "date"}``, and record the
    birthday on each person's entity.

    Every entry is validated before anything is created, so a bad date in the
    third entry does not leave the first two half-scheduled. Re-running is safe:
    an identical annual reminder is reported rather than duplicated, and an
    entity that already carries the birthday is left alone.
    """
    if not isinstance(birthdays, list) or not birthdays:
        raise ValueError("birthdays must be a non-empty list")
    if not 0 <= reminder_hour <= 23:
        raise ValueError("reminder_hour must be between 0 and 23")

    now = _now_utc()
    prepared: list[tuple[str, date, datetime, str, str]] = []
    for entry in birthdays:
        if not isinstance(entry, dict):
            raise ValueError("each birthday must be an object with person and date")
        person = str(entry.get("person") or entry.get("name") or "").strip()
        if not person:
            raise ValueError("each birthday requires a non-empty person")
        birthday = _birthday_date(entry.get("date"))
        when, rule, template = _reminder_plan(birthday, now=now, reminder_hour=reminder_hour)
        prepared.append((person, birthday, when, rule, template.format(person=person)))

    summaries: list[dict[str, Any]] = []
    for person, birthday, when, rule, message in prepared:
        duplicate = _existing_reminder(rule, message, db_path)
        if duplicate:
            summary: dict[str, Any] = {"job_id": duplicate, "recurrence": rule,
                                       "scheduled_for_local": when.isoformat(),
                                       "created": False, "reason": "already_scheduled"}
        else:
            summary = dict(schedule_task(
                kind="reminder", text=message, when=when, recurrence=rule,
                chat_id=chat_id, db_path=db_path,
            ))
            summary["created"] = True
        summary.update({"person": person, "birthday": birthday.isoformat(), "message": message})
        if vault is not None:
            summary["entity"] = record_birthday_on_entity(
                vault, person, birthday, db_path=db_path
            )
        summaries.append(summary)
    return summaries
