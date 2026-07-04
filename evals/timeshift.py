"""Longitudinal compression for the capstone loop (WO-9).

Simulating "two weeks passed" does not require faking the clock: it
requires the records to be two weeks older. This tool shifts the date
fields of vault records backward by N days — deterministically, across
frontmatter date fields and the transcript filenames — so open-loop
aging, salience decay, staleness, and review-after behavior can be
exercised in minutes instead of weeks.

Deliberately vault-destructive, so it carries the same guard as the wipe:
it refuses to run against the live vault unless --allow-live is passed
(this round's vault is owner-declared disposable), and never touches
anything outside the given vault.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisan.frontmatter import load_markdown, write_markdown  # noqa: E402

LIVE_VAULT = Path("/Users/august/.lisan/vault")
DATE_FIELDS = ("created", "updated", "last_confirmed", "review_after", "last_callback",
               "resolved_at", "first_seen", "last_reviewed", "generated", "date")
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

RECORD_DIRS = ("entities", "episodes", "knowledge", "evidence", "claims", "decisions",
               "open_loops", "state", "drafts", "patterns", "reviews", "contradictions", "self")


def _shift_date_string(value: str, days: int) -> str:
    match = _DATE_RE.match(str(value or ""))
    if not match:
        return value
    try:
        shifted = date.fromisoformat(match.group(1)) - timedelta(days=days)
    except ValueError:
        return value
    return shifted.isoformat() + str(value)[10:]


def shift_vault(vault: Path, days: int, *, allow_live: bool = False) -> dict:
    vault = Path(vault)
    if vault.resolve() == LIVE_VAULT.resolve() and not allow_live:
        raise RuntimeError("target is the live vault; pass --allow-live only if the vault is disposable")
    shifted_records = 0
    for dirname in RECORD_DIRS:
        root = vault / dirname
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            try:
                doc = load_markdown(path)
            except Exception:
                continue
            fm = dict(doc.frontmatter)
            changed = False
            for field in DATE_FIELDS:
                if field in fm and isinstance(fm[field], str):
                    new = _shift_date_string(fm[field], days)
                    if new != fm[field]:
                        fm[field] = new
                        changed = True
            if changed:
                write_markdown(path, fm, doc.body)
                shifted_records += 1
    # Transcript day-files: rename and re-stamp their date frontmatter.
    shifted_transcripts = 0
    transcripts = vault / "transcripts"
    if transcripts.exists():
        for path in sorted(transcripts.glob("*.md")):
            match = _DATE_RE.match(path.stem)
            if not match:
                continue
            new_day = (date.fromisoformat(match.group(1)) - timedelta(days=days)).isoformat()
            try:
                doc = load_markdown(path)
                fm = dict(doc.frontmatter)
                if "date" in fm:
                    fm["date"] = new_day
                target = path.with_name(f"{new_day}.md")
                write_markdown(target, fm, doc.body)
                if target != path:
                    path.unlink()
                shifted_transcripts += 1
            except Exception:
                continue
    return {"vault": str(vault), "days": days,
            "records_shifted": shifted_records, "transcripts_shifted": shifted_transcripts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--days", type=int, required=True, help="shift this many days into the past")
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()
    print(json.dumps(shift_vault(args.vault, args.days, allow_live=args.allow_live), indent=2))
