"""IIP Phase 1 — the Interpersonal Interpretation Protocol.

The finding this closes (owner + external review, 2026-07-15): asked why
someone kept repeating a question, the agent produced three readings that
all lived inside one person's psychology — no hypothesis where the user
was a causal factor, no boring base-rate reading, no discriminating
observations, no convergent action. The system's blind spots correlate
with the owner's, because the narrative store is its training data; a
hypothesis space drawn only from the corpus faithfully reproduces the
corpus's own priors.

Three pieces, WO-GROUND's proven shape (deterministic detector → injected
directive → the model still writes the reply → deterministic validation):

- ``is_interpretation_query`` — regex detector for turns asking to
  interpret another person's behavior, motives, or mental state. Runs
  every turn; over-inclusive by design; self-questions and system
  questions are excluded (they have their own machinery).
- ``INTERPRETATION_DIRECTIVE`` — the injected protocol block requiring a
  structured hypothesis space alongside the prose.
- ``validate_interpretation`` — deterministic post-hoc check: locus
  diversity (≥1 user_causal, ≥1 situational_baserate), non-empty
  discriminators, a convergent action (or the explicit word "none"),
  and provenance refs that resolve when present. Empty provenance is
  VALID by owner decree — an out-of-register hypothesis (Phase 2) has,
  by definition, nothing in the corpus to cite.

Every detector fire is logged to ``vault/logs/iip-challenges.jsonl``
(owner decree: detector precision must be visible, not only challenges).
The log carries a query digest, never the query text.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..paths import sqlite_path, vault_root

LOCI = {"other_person", "user_causal", "situational_baserate", "relationship_system"}

REQUIRED_LOCI = {"user_causal", "situational_baserate"}

LOG_NAME = "iip-challenges.jsonl"


# ── Detection ────────────────────────────────────────────────────────────────

# A person is in play: third-person pronouns, kin/relation words, or a
# capitalized mid-sentence token (a name). Deliberately loose.
_PERSON_RE = re.compile(
    r"\b(she|he|they|her|him|them|hers|his|theirs)\b"
    r"|\bmy (wife|husband|ex|co[- ]?parent|mother|father|mom|dad|daughters?|sons?|kids?|"
    r"girls?|boys?|sisters?|brothers?|friends?|boss|in[- ]laws?|mother[- ]in[- ]law|neighbor\w*)\b"
)

_NAME_RE = re.compile(r"(?<!^)(?<![.!?] )\b[A-Z][a-z]{2,}\b")  # capitalized, not sentence-start

_ASK_RE = re.compile(
    r"\bwhy (does|do|did|is|are|was|were|would|won.?t|can.?t|keeps?|keep)\b"
    r"|\bwhat (do you|should i|do we) make of\b"
    r"|\bhow (should|do|would) i (read|take|interpret|respond to|handle|hear)\b"
    r"|\bhelp me (read|understand|interpret|decode|make sense of)\b"
    r"|\bdecode\b"
    r"|\bwhat (did|does|do) (she|he|they|\w+) (mean|want|really)\b"
    r"|\bkeeps? (asking|saying|doing|texting|sending|bringing up)\b"
    r"|\bwhat.?s (going on|the deal|up) with (her|him|them)\b"
    r"|\b(is|are) (she|he|they) (trying|hoping|angling|hinting)\b"
)


def is_interpretation_query(text: str) -> bool:
    """Deterministic, every turn, no LLM. True when the turn asks for a
    reading of another person's behavior/motives/state.

    Requires a THIRD-PERSON signal: bare "you"/"your" never counts, so
    asks aimed at the agent itself ("why do you keep failing?") fall
    through to the self-question machinery instead. A turn naming both a
    person and system plumbing ("why does Ruth keep asking about my
    reminder system?") is an interpersonal read — and may legitimately
    carry the self-state block too; the two injections are independent."""
    raw = str(text or "").strip()
    if not raw:
        return False
    lowered = " ".join(raw.lower().split())
    if not _ASK_RE.search(lowered):
        return False
    return bool(_PERSON_RE.search(lowered) or _NAME_RE.search(raw))


# ── The injected protocol ────────────────────────────────────────────────────

INTERPRETATION_DIRECTIVE = """\
This turn asks you to interpret another person's behavior. Alongside your
prose response, your JSON output MUST include an "interpretation" object:

  "interpretation": {
    "hypotheses": [{"text": "...", "locus": "...", "provenance": ["record ids"]}],
    "discriminators": ["observable data that would tell the candidates apart"],
    "convergent_action": "an action sound under all leading hypotheses, or the word none with why"
  }

Rules, all hard:
- locus is one of: other_person, user_causal, situational_baserate,
  relationship_system. Include AT LEAST ONE user_causal hypothesis (the
  user's own words, actions, or ambivalence as a causal factor) and AT
  LEAST ONE situational_baserate hypothesis — boring by design:
  developmental stage, disability-related pattern, logistics, ordinary
  planning behavior; explanations requiring no psychological modeling of
  anyone.
- Order hypotheses by prior probability, boring first unless the record
  says otherwise.
- provenance lists the stored record ids that informed a hypothesis.
  An empty list is legitimate — some hypotheses SHOULD come from outside
  the record. Never invent ids.
- discriminators: what to look for, ask, or check that would distinguish
  the top candidates. At least one.
- convergent_action: if one action works under all leading readings, say
  it prominently in the prose too. If none exists, write "none" and say
  what decision actually forks.
- The prose stays natural and readable and must agree with the structure —
  the object is scaffolding, not the reply."""


# ── Validation ───────────────────────────────────────────────────────────────

def _ref_resolves(ref: str, db_path: Path | None) -> bool:
    """True when a provenance ref names a real indexed record. On any
    infrastructure trouble, err permissive — the validator must never
    fail a turn because the index was busy."""
    try:
        from .db import connect as _db_connect

        conn = _db_connect(db_path or sqlite_path(), readonly=True)
        try:
            row = conn.execute("SELECT 1 FROM files WHERE id = ? LIMIT 1", (str(ref),)).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return True


def validate_interpretation(payload: dict[str, Any], *, db_path: Path | None = None) -> list[str]:
    """Deterministic structural check. Returns complaints; empty = pass."""
    complaints: list[str] = []
    interp = payload.get("interpretation")
    if not isinstance(interp, dict):
        return ["missing the required interpretation object"]
    hypotheses = [h for h in (interp.get("hypotheses") or []) if isinstance(h, dict)]
    if not hypotheses:
        return ["interpretation.hypotheses is empty"]
    loci = [str(h.get("locus") or "") for h in hypotheses]
    for locus in set(loci) - LOCI:
        complaints.append(f"invalid locus {locus!r} (allowed: {', '.join(sorted(LOCI))})")
    for required in sorted(REQUIRED_LOCI - set(loci)):
        complaints.append(f"no {required} hypothesis present")
    if not [d for d in (interp.get("discriminators") or []) if str(d).strip()]:
        complaints.append("no discriminators — name observable data that would distinguish the candidates")
    if not str(interp.get("convergent_action") or "").strip():
        complaints.append('convergent_action missing — state one, or the word "none" with what forks')
    # Provenance: [] is valid (owner decree); refs must resolve when given.
    for h in hypotheses:
        for ref in (h.get("provenance") or []):
            if not _ref_resolves(str(ref), db_path):
                complaints.append(f"provenance ref {str(ref)!r} does not resolve to a stored record")
    return complaints


def incompleteness_notice(complaints: list[str]) -> str:
    """The user-facing note when regeneration is exhausted. Honest and
    specific: says which part of the hypothesis space is missing."""
    missing = []
    joined = " ".join(complaints)
    if "user_causal" in joined:
        missing.append("a reading where you are a causal factor")
    if "situational_baserate" in joined:
        missing.append("a boring base-rate reading")
    if "discriminators" in joined:
        missing.append("what to watch for that would settle it")
    if "convergent_action" in joined:
        missing.append("an action that holds under all readings")
    detail = "; ".join(missing) or "part of the required hypothesis space"
    return f"(A note on my own reasoning: this read may be incomplete — I couldn't produce {detail}.)"


# ── Logging ──────────────────────────────────────────────────────────────────

def log_iip_event(vault: Path | None, event: dict[str, Any]) -> None:
    """One JSONL line per detector fire. Best-effort: telemetry never
    fails the turn it describes. Query text never lands here — digest only."""
    try:
        vault = vault or vault_root()
        path = vault / "logs" / LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        pass


def query_digest(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:12]


def summarize_iip_log(vault: Path | None = None, *, weeks: int = 4) -> str:
    """Weekly counts from the fire/challenge log — the instrument for
    'is the system inheriting the owner's blind spots' and for judging
    detector precision and the regeneration cap. Plain text, per week."""
    from collections import defaultdict
    from datetime import timedelta

    vault = vault or vault_root()
    path = vault / "logs" / LOG_NAME
    if not path.exists():
        return "No IIP events logged yet."
    cutoff = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).isoformat()
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = str(event.get("ts") or "")
        if ts < cutoff:
            continue
        week = datetime.fromisoformat(ts).strftime("%G-W%V") if ts else "unknown"
        b = buckets[week]
        b["fires"] += 1
        b[f"validated_{event.get('validated', 'unknown')}"] += 1
        if event.get("regenerations"):
            b["regenerated"] += int(event["regenerations"])
        challenge = event.get("challenge")
        if isinstance(challenge, dict):
            b["challenges"] += 1
            b[f"challenge_{challenge.get('outcome', 'unknown')}"] += 1
    if not buckets:
        return f"No IIP events in the last {weeks} week(s)."
    lines = []
    for week in sorted(buckets):
        b = buckets[week]
        parts = [f"{key}={count}" for key, count in sorted(b.items())]
        lines.append(f"{week}: " + "  ".join(parts))
    return "\n".join(lines)
