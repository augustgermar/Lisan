"""WO-GROUND Seam A — self-referential questions route through ground truth.

The defect class (see docs/ground_truth_workorder.md): the agent answers a
question about ITSELF — status, schedule, auth, capabilities, its own
recent actions — from memory or plausibility while generated ground truth
sits unconsulted. Case history: invented CLI commands, stale gmail-failure
claims narrated as current, the 2026-07-06 "stalled task processor" story
that got a healthy process killed, and the 2026-07-14 "your daily prompt
is scheduled correctly" reassurance delivered over a series with eight
consecutive terminal failures in the ledger.

This module is the deterministic detector (keyword/pattern, never an LLM
call — it runs on every turn) plus the renderer for the GROUND_TRUTH block
the conversation turn injects when it fires. Over-inclusive on purpose:
a false positive costs a few hundred tokens of truth; a false negative
costs a confabulation.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# ── Detection ────────────────────────────────────────────────────────────────

# Direct liveness/status probes, with or without any other marker.
_LIVENESS_RE = re.compile(
    r"\bare you (there|alive|around|awake|up|ok|okay|online|running|working|listening)\b"
)

_STATUS_RE = re.compile(
    r"\b(system |current )?status\b"
    r"|\bhealth check\b"
    r"|\bself[- _]?state\b"
    r"|\bopen loops?\b"
)

# System components whose condition is agent-state whoever the sentence
# addresses ("is the reminder system working properly?" has no "you").
_SYSTEM_NOUN_RE = re.compile(
    r"\b(remind\w*|schedul\w*|task|job|queue|capture|memory|telegram|gmail|email|"
    r"skill|auth\w*|service|processor|pipeline|index|database|vault|log|error|version)\b"
)

_CONDITION_RE = re.compile(
    r"\b(work|works|working|worked|broken|break|stall|stalled|stuck|fail|failed|failing|"
    r"fire|fired|firing|miss|missed|missing|down|up|running|ran|run|alive|healthy|ok|okay|"
    r"online|offline|delayed|late|pending|queued|scheduled|configured|authorized|"
    r"authenticated|connected|set up|enabled|properly|correctly|going on)\b"
)

# The agent's own recent actions ("have you talked to anyone besides me?").
_RECENT_ACTION_RE = re.compile(
    r"\b(did|didn.?t|have|haven.?t|has|hasn.?t) you\b.{0,60}\b"
    r"(send|sent|sends|talk|talked|spoke|speak|message|messaged|text|texted|email|emailed|"
    r"run|ran|do|done|record|recorded|save|saved|write|wrote|written|schedule|scheduled|"
    r"remind|reminded|deliver|delivered|fail|failed|miss|missed|receive|received|get|got|"
    r"hear|heard|read|see|seen|notice|noticed)\b"
)

# Capability and command questions.
_CAPABILITY_RE = re.compile(
    r"\b(can|could|will|would) you\b"
    r"|\bare you (able|capable)\b"
    r"|\bdo you (have|support|know how|handle)\b"
    r"|\bwhat (can|could|do) you do\b"
    r"|\bcapab\w+\b"
    r"|\b(command|commands|cli|subcommand|flag|flags)\b"
    r"|\bhow (do|would|can) i\b.{0,60}\b(you|lisan)\b"
    r"|\byour (skills?|features?|tools?|abilities)\b"
    r"|\b(is|are) (that|this|it) (built|implemented|supported)\b"
)

# Second-person address of the agent, or the system by name. Used to scope
# the broad state terms so "the job market is broken" doesn't fire.
_AGENT_RE = re.compile(r"\b(you|your|yours|yourself|lisan)\b")


def detect_self_question(text: str) -> set[str]:
    """Which ground truths this turn needs. Deterministic, cheap, every turn.

    Returns a subset of {"state", "capabilities"} — empty when the turn is
    not about the agent. Over-inclusive by design (see module docstring).
    """
    lowered = " ".join(str(text or "").lower().split())
    if not lowered:
        return set()
    needs: set[str] = set()

    if _LIVENESS_RE.search(lowered):
        needs.add("state")
    if _STATUS_RE.search(lowered) and _AGENT_RE.search(lowered):
        needs.add("state")
    if _SYSTEM_NOUN_RE.search(lowered) and _CONDITION_RE.search(lowered):
        needs.add("state")
    if _RECENT_ACTION_RE.search(lowered):
        needs.add("state")
    if _CAPABILITY_RE.search(lowered):
        needs.add("capabilities")
        # Auth/skill capability questions are usually really state questions
        # ("can you read my gmail?" → the answer lives in skill_auth).
        if _SYSTEM_NOUN_RE.search(lowered):
            needs.add("state")
    return needs


# ── Rendering ────────────────────────────────────────────────────────────────

_HEADER = (
    "Generated at answer time from the running system. This block is your ONLY "
    "valid source for statements about your own current state, schedule, auth, "
    "and abilities. Anything retrieved from memory about yourself is history — "
    "cite it as history ('on July 5 I reported X'), never as the present. Where "
    "memory and this block disagree, this block wins without discussion."
)


def render_ground_truth(
    needs: set[str],
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
) -> str | None:
    """The GROUND_TRUTH block for a detected self-question. Never raises."""
    sections: list[str] = []
    if "state" in needs:
        try:
            from .self_model import render_self_state, snapshot_self_state

            sections.append(
                "LIVE SELF-STATE:\n" + render_self_state(snapshot_self_state(vault, db_path))
            )
        except Exception:
            sections.append(
                "LIVE SELF-STATE: unavailable (snapshot failed) — say so plainly; "
                "do not substitute memory for it."
            )
    if "capabilities" in needs:
        try:
            from .self_model import cli_reference

            sections.append("COMMAND REFERENCE (introspected from the code):\n" + cli_reference())
        except Exception:
            pass
    if not sections:
        return None
    return _HEADER + "\n\n" + "\n\n".join(sections)
