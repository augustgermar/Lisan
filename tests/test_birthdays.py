"""Birthdays: the reminder, and the fact the entity keeps. Invented cast only."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from lisan.frontmatter import dump_markdown, load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools import birthdays, scheduler
from lisan.tools.jobs import get_job
from lisan.tools.scheduler import next_occurrence


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    monkeypatch.setattr(birthdays, "_local_tz", lambda: timezone.utc)
    monkeypatch.setattr(scheduler, "_local_tz", lambda: timezone.utc)


def _at(y, m, d, h=8):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


def _person(vault: Path, stem: str, name: str, **fm_extra) -> Path:
    folder = vault / "entities" / "people"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.md"
    fm = {
        "id": f"entity.{stem}", "type": "entity", "canonical_name": name,
        "kind": "entity", "subtype": "person", "significance": "medium",
        "created": "2026-01-01", "updated": "2026-01-01", "links": [],
    }
    fm.update(fm_extra)
    path.write_text(dump_markdown(fm, f"# {name}\n\nA person.\n"), encoding="utf-8")
    return path


# ── scheduling ────────────────────────────────────────────────────────────────

def test_schedules_the_day_before_with_an_annual_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(birthdays, "_now_utc", lambda: _at(2026, 8, 12))
    db = tmp_path / "jobs.sqlite"

    result = birthdays.schedule_birthday_reminders(
        [{"person": "Ada", "date": "2090-08-20"}, {"name": "Lin", "date": "2090-01-01"}],
        db_path=db,
    )

    assert [r["person"] for r in result] == ["Ada", "Lin"]
    ada = get_job(result[0]["job_id"], db_path=db)
    assert ada["scheduled_for"] == "2026-08-19T09:00:00Z"
    assert ada["recurrence"] == "annual@08-19@09:00"
    # A January 1 birthday reminds on December 31 — the previous year.
    assert get_job(result[1]["job_id"], db_path=db)["recurrence"] == "annual@12-31@09:00"


def test_dates_are_validated_before_any_job_is_created(tmp_path):
    db = tmp_path / "jobs.sqlite"
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        birthdays.schedule_birthday_reminders(
            [{"person": "Ada", "date": "2090-08-20"}, {"person": "Bad", "date": "2090-02-30"}],
            db_path=db,
        )
    # The valid first entry must not have been scheduled before the bad one raised.
    assert not db.exists() or not [
        j for j in __import__("lisan.tools.jobs", fromlist=["x"]).list_jobs(db_path=db)
        if j.get("job_type") == "task.reminder"
    ]


@pytest.mark.parametrize("bad", ["2090-8-20", "20900820", "2090-W12-1", "not-a-date", ""])
def test_non_iso_shapes_are_rejected(bad):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        birthdays._birthday_date(bad)


# ── the leap-day case the old implementation got wrong ────────────────────────

def test_february_29_reminds_on_the_28th_in_a_leap_year():
    """The bug: Feb 29 was forced to Feb 28 in *every* year and then a day was
    subtracted, so a leap-day birthday reminded on the 27th — two days early in
    the one year the birthday actually falls, and identical to a Feb 28
    birthday's reminder."""
    when, rule, _ = birthdays._reminder_plan(date(2000, 2, 29), now=_at(2028, 1, 1))
    assert when.date() == date(2028, 2, 28)      # 2028 is a leap year: the eve
    assert rule == "annual@02-28@09:00"


@pytest.mark.parametrize("scheduled_in", [2026, 2027, 2028, 2029, 2030])
def test_the_leap_day_rule_does_not_depend_on_the_year_it_was_created(scheduled_in):
    """Caught by running it, not by the tests above.

    "The day before February 29" is not a fixed calendar date, so deriving the
    recurrence from whichever year the command happened to run in produced
    annual@02-27 from a common year and annual@02-28 from a leap year — and the
    first is then two days early for the rest of the person's life. The stored
    rule has to be the same whenever it is written.
    """
    _, rule, _ = birthdays._reminder_plan(date(2000, 2, 29), now=_at(scheduled_in, 1, 1))
    assert rule == "annual@02-28@09:00"


def test_february_29_and_february_28_are_no_longer_the_same_reminder(monkeypatch):
    now = _at(2028, 1, 1)
    _, leap_rule, _ = birthdays._reminder_plan(date(2000, 2, 29), now=now)
    _, feb28_rule, _ = birthdays._reminder_plan(date(2000, 2, 28), now=now)
    assert leap_rule != feb28_rule
    assert feb28_rule == "annual@02-27@09:00"


def test_observed_day_tracks_the_actual_calendar():
    assert birthdays._observed_day(date(2000, 2, 29), 2028) == date(2028, 2, 29)  # leap
    assert birthdays._observed_day(date(2000, 2, 29), 2027) == date(2027, 2, 28)  # common
    assert birthdays._observed_day(date(2000, 3, 31), 2027) == date(2027, 3, 31)


def test_the_message_never_claims_tomorrow(monkeypatch):
    """A recurrence is a fixed MM-DD chosen once, but "the day before" is not a
    fixed date for every birthday — the day before March 1 moves between Feb 28
    and Feb 29. A stored message saying "tomorrow" would be false one year in
    four, so it states the date instead."""
    now = _at(2026, 1, 1)
    for birthday in (date(2000, 3, 1), date(2000, 2, 29), date(2000, 7, 4)):
        _, _, message = birthdays._reminder_plan(birthday, now=now)
        assert "tomorrow" not in message.lower()
    _, _, march = birthdays._reminder_plan(date(2000, 3, 1), now=now)
    assert "March 1" in march


def test_annual_recurrence_handles_february_29_when_a_rule_names_it(monkeypatch):
    """A rule of annual@02-29 can still be written by hand via schedule_task,
    so next_occurrence must resolve it — but note this feature never emits one,
    which is why the old test asserting only this proved nothing about
    birthdays."""
    assert next_occurrence("annual@02-29", after=_at(2027, 3, 1)) == datetime(2028, 2, 29, tzinfo=timezone.utc)


# ── idempotency ───────────────────────────────────────────────────────────────

def test_rerunning_does_not_duplicate_the_reminder(tmp_path, monkeypatch):
    monkeypatch.setattr(birthdays, "_now_utc", lambda: _at(2026, 8, 12))
    from lisan.tools.jobs import list_jobs
    db = tmp_path / "jobs.sqlite"
    entry = [{"person": "Ada", "date": "2090-08-20"}]

    first = birthdays.schedule_birthday_reminders(entry, db_path=db)
    second = birthdays.schedule_birthday_reminders(entry, db_path=db)
    third = birthdays.schedule_birthday_reminders(entry, db_path=db)

    assert first[0]["created"] is True
    assert second[0]["created"] is False and third[0]["created"] is False
    assert second[0]["job_id"] == first[0]["job_id"]
    reminders = [j for j in list_jobs(db_path=db, limit=100) if j["job_type"] == "task.reminder"]
    assert len(reminders) == 1


def test_two_different_people_are_not_confused_for_duplicates(tmp_path, monkeypatch):
    """Same day, different person — two commitments, not one. task.reminder is
    in the scheduler's NO_COALESCE set for this reason; dedup here must be at
    least as careful."""
    monkeypatch.setattr(birthdays, "_now_utc", lambda: _at(2026, 8, 12))
    from lisan.tools.jobs import list_jobs
    db = tmp_path / "jobs.sqlite"
    birthdays.schedule_birthday_reminders([{"person": "Ada", "date": "2090-08-20"}], db_path=db)
    birthdays.schedule_birthday_reminders([{"person": "Lin", "date": "2091-08-20"}], db_path=db)
    reminders = [j for j in list_jobs(db_path=db, limit=100) if j["job_type"] == "task.reminder"]
    assert len(reminders) == 2


# ── the fact on the entity ────────────────────────────────────────────────────

class _Vault:
    def __init__(self, tmp_path: Path):
        ensure_repo_layout(tmp_path)
        self.vault = vault_root(tmp_path)
        self.db = tmp_path / "lisan.sqlite"


def test_birthday_lands_in_frontmatter_and_the_durable_log(tmp_path):
    env = _Vault(tmp_path)
    path = _person(env.vault, "ada-lovelace", "Ada Lovelace")

    result = birthdays.record_birthday_on_entity(env.vault, "Ada", date(1990, 8, 20), db_path=env.db)

    assert result["entity_updated"] is True
    fm = load_markdown(path).frontmatter
    assert fm["birthday"] == "1990-08-20"
    # The durable seam: prose in the body would be dropped at the next
    # compaction, so the fact goes to source_log, which compaction re-tells the
    # narrative from and which is indexed immediately either way.
    log = fm["source_log"]
    assert len(log) == 1
    assert "August 20, 1990" in log[0]["text"]
    assert log[0]["folded"] is False


def test_recording_twice_does_not_append_twice(tmp_path):
    env = _Vault(tmp_path)
    path = _person(env.vault, "ada-lovelace", "Ada Lovelace")
    birthdays.record_birthday_on_entity(env.vault, "Ada", date(1990, 8, 20), db_path=env.db)
    again = birthdays.record_birthday_on_entity(env.vault, "Ada", date(1990, 8, 20), db_path=env.db)
    assert again["reason"] == "already_recorded"
    assert len(load_markdown(path).frontmatter["source_log"]) == 1


def test_a_conflicting_birthday_is_refused_not_overwritten(tmp_path):
    """Two dates for one person is a contradiction the owner should see."""
    env = _Vault(tmp_path)
    path = _person(env.vault, "ada-lovelace", "Ada Lovelace", birthday="1990-08-20")

    result = birthdays.record_birthday_on_entity(env.vault, "Ada", date(1991, 3, 3), db_path=env.db)

    assert result["reason"] == "conflict"
    assert result["existing"] == "1990-08-20" and result["proposed"] == "1991-03-03"
    assert load_markdown(path).frontmatter["birthday"] == "1990-08-20"   # untouched
    assert "source_log" not in load_markdown(path).frontmatter


def test_an_unknown_person_still_gets_a_reminder(tmp_path, monkeypatch):
    """A birthday worth remembering must not fail to schedule because the vault
    has never heard of them. No entity is invented either — entity birth runs
    through the capture pipeline's gates, and a bare name typed at a CLI flag
    would route around all of them."""
    monkeypatch.setattr(birthdays, "_now_utc", lambda: _at(2026, 8, 12))
    env = _Vault(tmp_path)
    result = birthdays.schedule_birthday_reminders(
        [{"person": "Nobody", "date": "2090-08-20"}], db_path=env.db, vault=env.vault
    )
    assert result[0]["created"] is True
    assert result[0]["entity"]["reason"] == "no_entity"
    assert not list((env.vault / "entities" / "people").glob("*.md"))


def test_an_ambiguous_first_name_refuses_rather_than_guessing(tmp_path):
    env = _Vault(tmp_path)
    _person(env.vault, "ada-lovelace", "Ada Lovelace")
    _person(env.vault, "ada-byron", "Ada Byron")

    result = birthdays.record_birthday_on_entity(env.vault, "Ada", date(1990, 8, 20), db_path=env.db)

    assert result["reason"] == "ambiguous"
    assert sorted(result["candidates"]) == ["Ada Byron", "Ada Lovelace"]
    for stem in ("ada-lovelace", "ada-byron"):
        fm = load_markdown(env.vault / "entities" / "people" / f"{stem}.md").frontmatter
        assert "birthday" not in fm


def test_scheduling_records_the_fact_when_a_vault_is_given(tmp_path, monkeypatch):
    monkeypatch.setattr(birthdays, "_now_utc", lambda: _at(2026, 8, 12))
    env = _Vault(tmp_path)
    path = _person(env.vault, "ada-lovelace", "Ada Lovelace")

    result = birthdays.schedule_birthday_reminders(
        [{"person": "Ada", "date": "1990-08-20"}], db_path=env.db, vault=env.vault
    )

    assert result[0]["entity"]["entity_updated"] is True
    assert load_markdown(path).frontmatter["birthday"] == "1990-08-20"


def test_without_a_vault_it_only_schedules(tmp_path, monkeypatch):
    monkeypatch.setattr(birthdays, "_now_utc", lambda: _at(2026, 8, 12))
    env = _Vault(tmp_path)
    _person(env.vault, "ada-lovelace", "Ada Lovelace")
    result = birthdays.schedule_birthday_reminders(
        [{"person": "Ada", "date": "1990-08-20"}], db_path=env.db
    )
    assert "entity" not in result[0]
