"""The deterministic gate between a reviewer's proposal and a skill on disk.

The reviewer (a model) proposes operations; this module decides, in plain code,
whether each may take effect, and works out exactly what it would write. No
model is consulted here and nothing is written here: `gate_operation` returns a
verdict and a `PlannedChange` (the full new file contents plus a diff), and a
later stage applies accepted changes. That separation is what lets the same code
serve shadow mode (show what would happen) and auto mode (do it).

Rules, from docs/learning_loop_workorder.md (Stage 3):

 1. evidence      every cited event exists, is in the batch, is real use (not an
                  eval), and there are enough of them
 2. scope         instructional files only; confined to the skill's directory;
                  never a pinned skill
 3. authorship    NOT a barrier: owner-authored skills may be edited (the applier
                  keeps the owner's original as v0)
 4. provenance    recorded on the skill (sources, tainted), never a reason to refuse
 5. format, size  the result must parse; privilege fields cannot be widened
 6. names         class-level, not a date, ticket, error, or "fix-x-today"
 7. content scan  no credentials; no text that tries to give the agent orders
 8. no negative   added text may not assert that something "is broken" / "does
    claims        not work": a failure belongs in an episode, not a standing rule
"""
from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import skill_frontmatter as fm
from .learning import is_eval_conversation
from .skill_format import SKILL_FILE, parse_frontmatter, parse_skill_md
from .skill_history import CODE_ENTRIES, HISTORY_DIR, SkillHistoryError, is_pinned, read_provenance

OPS = ("patch", "add_reference", "create", "none")
MIN_EVIDENCE = {"patch": 1, "add_reference": 1, "create": 2}  # create: 2 independent events (decision 4)

DEFAULT_MAX_SKILL_BYTES = 16_000
MAX_REFERENCE_FACTOR = 2
MAX_DESCRIPTION = 1024
MAX_NAME = 40

_REFERENCE_PATH = re.compile(r"^references/[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
_NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
# What a name must not be: a moment, a ticket, an error, or a stopgap. A skill is
# a class of task, not something that happened once.
_BAD_NAME_PARTS = re.compile(
    r"(\d{2,})|(^|-)(pr|issue|ticket|bug|fix|hotfix|today|yesterday|tomorrow|temp|tmp|misc|stuff|test|new|old|"
    r"error|exception|failure|failed|traceback|phase|stage|step|part|round|iteration)(-|$)|(^|-)[a-z](-|$)"
)

# Frontmatter a reviewer's patch may not change: capability and visibility fields
# belong to the owner, and identity belongs to the directory. `version` and
# `metadata` are changed by the applier, never by a proposed patch.
_PROTECTED_FRONTMATTER_EXCEPT = {"description"}

_INJECTION = (
    re.compile(r"(?i)\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|any|your)\b[^.\n]{0,30}\b(?:instruction|prompt|rule|guideline|polic)"),
    re.compile(r"(?i)\bdo not (?:tell|inform|notify|alert|mention (?:this )?to) the (?:user|owner)\b"),
    re.compile(r"(?i)\b(?:system prompt|developer message|hidden instruction)\b"),
    re.compile(r"(?i)\byou (?:must|should|are required to) (?:always|never)\b[^.\n]{0,80}\b(?:without|do not|don't) (?:ask|confirm|check|tell|verify)"),
    re.compile(r"(?i)\b(?:exfiltrat|secretly|covertly)"),
    re.compile(r"(?i)\bsend\b[^.\n]{0,50}\bto\b[^.\n]{0,40}[\w.+-]+@[\w-]+\.[\w.-]+"),
)
_NEGATIVE_CLAIMS = (
    re.compile(r"(?i)\b(?:is|are|was|were)\s+(?:currently\s+|now\s+|permanently\s+)?(?:broken|unavailable|down|unsupported|deprecated|disabled|not working)\b"),
    re.compile(r"(?i)\b(?:does|do|did)\s*n(?:o|')?t\s+work\b|\bdoes not work\b"),
    re.compile(r"(?i)\bnever works\b|\bwon't work\b|\bwill not work\b|\bcannot be used\b"),
)


@dataclass
class Operation:
    op: str
    skill: str
    file: str = ""
    old_text: str = ""
    new_text: str = ""
    content: str = ""
    pointer: str = ""
    description: str = ""
    body: str = ""
    evidence: list[str] = field(default_factory=list)
    rationale: str = ""


@dataclass
class PlannedChange:
    skill: str
    op: str
    files: dict[str, str]  # relative path -> full new text
    is_new: bool
    provenance: dict[str, Any]
    diff: str
    version_before: str | None = None
    version_after: str | None = None
    # The text each file had when this change was planned. The applier refuses to
    # write if the file has changed since: a plan is only valid for the text it saw.
    old_files: dict[str, str] = field(default_factory=dict)


@dataclass
class GateVerdict:
    op: Operation
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    change: PlannedChange | None = None
    first_attempt_reasons: list[str] | None = None  # set when this is a revision of a refused proposal
    applied: Any = None  # skill_apply.Applied once written
    apply_note: str | None = None  # why an accepted change was not applied (cap, stale, ...)


# Reasons that are about safety, not form. A proposal refused for one of these is
# never sent back for another try: retrying invites the reviewer to reword its
# way past the check, and the check is the point.
_SAFETY_REASONS = (
    "credential", "give the agent orders", "is pinned",
    "from a rehearsal", "is not a skill this loop may touch", "executable code is out of reach", "is not editable",
)


def is_repairable(verdict: GateVerdict) -> bool:
    """A refusal that is a slip of form (missing evidence, a mismatched quote, a
    description that does not start 'Use when', an incident-style name) and not a
    safety finding. These get one revision."""
    if verdict.accepted or verdict.op.op == "none" or not verdict.reasons:
        return False
    return not any(marker in reason for reason in verdict.reasons for marker in _SAFETY_REASONS)


def parse_operations(data: Any) -> list[Operation]:
    """Tolerant: a reviewer's JSON is never trusted to be well formed."""
    raw = data.get("operations") if isinstance(data, dict) else None
    ops: list[Operation] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        evidence = item.get("evidence")
        ops.append(Operation(
            op=str(item.get("op") or "").strip(),
            skill=str(item.get("skill") or "").strip(),
            file=str(item.get("file") or "").strip(),
            old_text=str(item.get("old_text") or ""),
            new_text=str(item.get("new_text") or ""),
            content=str(item.get("content") or ""),
            pointer=str(item.get("pointer") or "").strip(),
            description=str(item.get("description") or "").strip(),
            body=str(item.get("body") or ""),
            evidence=[str(e) for e in evidence] if isinstance(evidence, list) else [],
            rationale=str(item.get("rationale") or "").strip(),
        ))
    return ops


# ── individual checks ────────────────────────────────────────────────────────

def resolve_event_id(cited: str, batch_ids: set[str]) -> str:
    """Models drop the kind prefix when copying an id ("job.2026…" for
    "turn:job.2026…"). Forgive exactly that: a citation that is, minus its
    prefix, one — and only one — event in the batch means that event. Anything
    else is returned unchanged for the evidence check to refuse. This never
    invents evidence: the cited thing must still be an event the reviewer read."""
    if cited in batch_ids:
        return cited
    matches = [b for b in batch_ids if ":" in b and b.split(":", 1)[1] == cited]
    return matches[0] if len(matches) == 1 else cited


def check_name(name: str) -> list[str]:
    problems = []
    if not _NAME.match(name or ""):
        problems.append(f"name {name!r} must be lowercase words joined by hyphens")
    if len(name or "") < 3 or len(name or "") > MAX_NAME:
        problems.append(f"name {name!r} must be 3-{MAX_NAME} characters")
    if _BAD_NAME_PARTS.search(name or ""):
        problems.append(
            f"name {name!r} reads as a moment, ticket, error, stopgap or project phase; a skill is a class of task "
            "(not a date, number, 'fix-…', 'phase-a', or an error)"
        )
    return problems


def scan_added_text(added: str) -> list[str]:
    """Checks 7 and 8, on text the change introduces (not on what was already there),
    plus retired tool names (a skill outlives a rename)."""
    from ..providers.codex import mask_secrets_strict
    from .execution_tools import LEGACY_TOOL_NAMES

    problems = []
    for legacy, current in LEGACY_TOOL_NAMES.items():
        if re.search(rf"\b{re.escape(legacy)}\b", added):
            problems.append(f"added text names the retired tool {legacy!r}; use its current name {current!r}")
    if mask_secrets_strict(added) != added:
        problems.append("added text contains something that looks like a credential")
    for pattern in _INJECTION:
        if pattern.search(added):
            problems.append(f"added text tries to give the agent orders ({pattern.pattern[:40]}…)")
            break
    claim = _first_negative_claim(added)
    if claim:
        problems.append(
            f"added text asserts a negative capability claim ({claim!r}); a failure belongs in an "
            "episode, not a standing rule — it hardens into refusals the agent cites against itself "
            "(describe what to do when something is missing, not that it is broken)"
        )
    return problems


# Words that mark a lesson about the agent's OWN environment: what it is allowed
# to do and how its guardrails behave. These change within weeks (the approval
# gate and the write boundary were both removed in July), so a lesson drawn only
# from old events is likely obsolete on arrival. The prompt asks the reviewer to
# be sceptical; this is the deterministic backstop for when it is not.
_ENVIRONMENT_VOCABULARY = re.compile(
    r"(?i)\b(?:sandbox(?:ed)?|approval(?:s| gate)?|write[- ]boundary|permissions?|entitlements?|full[- ]access|"
    r"allow-?list|read-only mode|intent\.md)\b"
)
DEFAULT_ENVIRONMENT_STALENESS_DAYS = 14


def _event_age_days(event: dict[str, Any], today: str) -> int | None:
    import calendar

    try:
        then = calendar.timegm(time.strptime(str(event.get("occurred_at"))[:10], "%Y-%m-%d"))
        now = calendar.timegm(time.strptime(today, "%Y-%m-%d"))
    except (ValueError, TypeError):
        return None  # a malformed date is not grounds to refuse
    return max(0, (now - then) // 86400)


_CONDITIONAL = re.compile(r"(?i)\b(?:if|when|whenever|unless|whether|in case|should|once|until|while)\b")


def _first_negative_claim(added: str) -> str | None:
    """A claim that something IS broken, as opposed to guidance for when it is: a
    match inside a conditional clause ("if the inputs are unavailable, stop and
    report") describes how to handle a state, and is exactly what a good skill
    says. Judged clause by clause."""
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", added):
        for pattern in _NEGATIVE_CLAIMS:
            match = pattern.search(sentence)
            if match and not _CONDITIONAL.search(sentence[: match.start()]):
                return match.group(0)
    return None


def _added_lines(old: str, new: str) -> str:
    old_lines = set(old.splitlines())
    return "\n".join(line for line in new.splitlines() if line not in old_lines)


def _unified(path: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{path}" if old else "/dev/null", tofile=f"b/{path}",
    ))


def _provenance(events: list[dict[str, Any]]) -> dict[str, Any]:
    sources: set[str] = set()
    for event in events:
        sources.update(event.get("sources") or [])
    return {
        "sources": sorted(sources),
        "tainted": any(e.get("tainted") for e in events),
        "source_events": [e["id"] for e in events],
    }


def _metadata_updates(
    provenance: dict[str, Any], *, creating: bool, owner_authored: bool, today: str, existing_status: str | None = None
) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "sources": provenance["sources"],
        "tainted": provenance["tainted"],
        "source_events": provenance["source_events"][:10],
        "last_revised": today,
    }
    if creating:
        updates.update({"origin": "agent", "status": "provisional", "created": today})
    else:
        updates["revised_by"] = "agent"
        if existing_status == "flagged":
            updates["status"] = "provisional"  # a revised skill must prove itself again
    return updates


# ── the gate ─────────────────────────────────────────────────────────────────

def gate_operation(
    op: Operation,
    *,
    skills_dir: Path,
    events: dict[str, dict[str, Any]],
    batch_ids: set[str],
    config: dict[str, Any] | None = None,
    today: str | None = None,
) -> GateVerdict:
    """Decide one operation. Never raises on a bad proposal; reasons say why."""
    today = today or time.strftime("%Y-%m-%d", time.gmtime())
    settings = (config or {}).get("learning") or {}
    max_bytes = int(settings.get("max_skill_bytes") or DEFAULT_MAX_SKILL_BYTES)
    reasons: list[str] = []

    def reject(*why: str) -> GateVerdict:
        return GateVerdict(op, False, [*reasons, *why])

    if op.op not in OPS:
        return reject(f"unknown operation {op.op!r}")
    if op.op == "none":
        return reject("a no-op: the reviewer found nothing worth saving")

    # 1. evidence
    cited = list(dict.fromkeys(resolve_event_id(e, batch_ids) for e in op.evidence))
    if len(cited) < MIN_EVIDENCE[op.op]:
        reasons.append(f"{op.op} needs at least {MIN_EVIDENCE[op.op]} distinct cited event(s); got {len(cited)}")
    for event_id in cited:
        if event_id not in batch_ids or event_id not in events:
            reasons.append(f"cited event {event_id!r} is not one of the events reviewed")
        elif is_eval_conversation(events[event_id].get("conversation_id")):
            reasons.append(f"cited event {event_id!r} is from a rehearsal, not real use")
    real = [events[e] for e in cited if e in events and e in batch_ids]

    # 2. scope
    try:
        skill_path = (skills_dir / op.skill).resolve()
        if not op.skill or op.skill.startswith((".", "_")) or skills_dir.resolve() not in skill_path.parents:
            reasons.append(f"{op.skill!r} is not a skill this loop may touch")
        elif not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", op.skill):
            reasons.append(f"{op.skill!r} is not a valid skill name")
    except OSError:
        reasons.append(f"{op.skill!r} cannot be resolved")
    if reasons and any("not a skill this loop" in r or "not a valid skill name" in r for r in reasons):
        return reject()

    exists = (skill_path / SKILL_FILE).is_file()
    if op.op == "create":
        if exists or skill_path.exists():
            reasons.append(f"skill {op.skill!r} already exists; propose a patch to it instead")
        reasons += check_name(op.skill)
    else:
        if not exists:
            return reject(f"no skill named {op.skill!r} to {op.op}")
        if is_pinned(skills_dir, op.skill):
            return reject(f"{op.skill!r} is pinned: the owner has reserved it")

    if reasons:
        return reject()

    old_files: dict[str, str] = {}
    new_files: dict[str, str] = {}
    prov = _provenance(real)
    owner_authored = op.op != "create" and read_provenance(skills_dir, op.skill)["origin"] != "agent"
    version_before = None

    if op.op == "create":
        if not op.description or not op.body.strip():
            return reject("create needs a description and a body")
        if "\n" in op.description:
            return reject("the description must be one line")
        text = (
            f"---\nname: {op.skill}\ndescription: {op.description}\nversion: 0.1.0\n---\n\n"
            f"{op.body.strip()}\n"
        )
        text = fm.set_metadata(text, _metadata_updates(prov, creating=True, owner_authored=False, today=today))
        new_files[SKILL_FILE] = text
        version_after = "0.1.0"
    else:
        target = op.file or SKILL_FILE
        if op.op == "add_reference":
            target = op.file
            if not _REFERENCE_PATH.match(target):
                return reject("a reference file must be references/<name>.md")
        elif target != SKILL_FILE and not _REFERENCE_PATH.match(target):
            return reject(f"{target!r} is not editable: only SKILL.md and references/*.md are in reach")
        if Path(target).name in CODE_ENTRIES or target.split("/")[0] in CODE_ENTRIES:
            return reject("executable code is out of reach of the loop")

        skill_md = (skill_path / SKILL_FILE).read_text(encoding="utf-8")
        old_files[SKILL_FILE] = skill_md
        version_before = str(parse_frontmatter(skill_md)[0].get("version") or "") or None

        if op.op == "patch":
            current_path = skill_path / target
            if not current_path.is_file():
                return reject(f"{target} does not exist in {op.skill!r}")
            current = current_path.read_text(encoding="utf-8")
            old_files[target] = current
            if not op.old_text:
                return reject("a patch needs old_text, the exact text to replace")
            hits = current.count(op.old_text)
            if hits != 1:
                return reject(
                    f"old_text must appear exactly once in {target}; it appears {hits} time(s) "
                    "(quote enough surrounding text to be unambiguous)"
                )
            patched = current.replace(op.old_text, op.new_text, 1)
            if target == SKILL_FILE:
                problem = _frontmatter_integrity(current, patched)
                if problem:
                    return reject(problem)
            new_files[target] = patched
        else:  # add_reference
            if not op.content.strip():
                return reject("add_reference needs content")
            if not op.pointer:
                return reject("add_reference needs a one-line pointer so SKILL.md sends the reader there")
            ref_path = skill_path / target
            if ref_path.exists():
                old_files[target] = ref_path.read_text(encoding="utf-8")
            new_files[target] = op.content.rstrip() + "\n"
            link = f"- [{Path(target).name}]({target}): {op.pointer}"
            if target not in skill_md:
                new_files[SKILL_FILE] = _with_reference_line(skill_md, link)

        # the applier's bookkeeping, applied to the result so it can be validated too
        md = new_files.get(SKILL_FILE, skill_md)
        # an existing skill with no version is effectively 1.0.0, so its first
        # revision is 1.0.1 (0.1.0 would read as a downgrade)
        md = fm.bump_patch_version(md, default="1.0.1")
        existing_status = read_provenance(skills_dir, op.skill).get("status")
        md = fm.set_metadata(md, _metadata_updates(
            prov, creating=False, owner_authored=owner_authored, today=today, existing_status=existing_status))
        new_files[SKILL_FILE] = md
        version_after = str(parse_frontmatter(md)[0].get("version") or "") or None

    # 5. format and size
    for rel, text in new_files.items():
        limit = max_bytes if rel == SKILL_FILE else max_bytes * MAX_REFERENCE_FACTOR
        if len(text.encode("utf-8")) > limit:
            reasons.append(f"{rel} would be {len(text.encode('utf-8')):,} bytes; the limit is {limit:,}")
    md_text = new_files.get(SKILL_FILE, "")
    if md_text:
        reasons += _validate_skill_md(op, md_text, old_files.get(SKILL_FILE))

    # 7 + 8. content scan on what the change adds, across every file it touches.
    # Lines the loop wrote itself (version, provenance) are not the reviewer's text.
    added = "\n".join(_added_lines(old_files.get(rel, ""), text) for rel, text in new_files.items())
    reviewer_text = _strip_bookkeeping(added)
    reasons += scan_added_text(reviewer_text)
    reasons += _stale_environment_lesson(reviewer_text, real, today, int(settings.get("environment_staleness_days") or DEFAULT_ENVIRONMENT_STALENESS_DAYS))

    if reasons:
        return reject()

    diff = "".join(_unified(f"{op.skill}/{rel}", old_files.get(rel, ""), text) for rel, text in sorted(new_files.items())
                   if old_files.get(rel, "") != text)
    change = PlannedChange(
        skill=op.skill, op=op.op, files=new_files, is_new=op.op == "create", provenance=prov, diff=diff,
        version_before=version_before, version_after=version_after, old_files=dict(old_files),
    )
    return GateVerdict(op, True, [], change)


def _stale_environment_lesson(added: str, evidence: list[dict[str, Any]], today: str, max_age: int) -> list[str]:
    """Refuse a lesson about the agent's own environment whose evidence is all
    old. Fresh evidence (any cited event within `max_age` days) clears it."""
    if not evidence or not _ENVIRONMENT_VOCABULARY.search(added):
        return []
    ages = [a for a in (_event_age_days(e, today) for e in evidence) if a is not None]
    if not ages or min(ages) <= max_age:
        return []
    return [
        f"the added text is about the agent's own environment (sandbox, approvals, permissions) but its newest "
        f"evidence is {min(ages)} days old; those rules change within weeks, so cite events from the last {max_age} "
        "days or leave it out"
    ]


def _strip_bookkeeping(added: str) -> str:
    keep = []
    for line in added.splitlines():
        stripped = line.strip()
        if re.match(r"^(version|name):", stripped) or re.match(r"^(origin|status|created|sources|tainted|source_events|last_revised|revised_by):", stripped):
            continue
        keep.append(line)
    return "\n".join(keep)


def _with_reference_line(skill_md: str, link: str) -> str:
    if "## References" in skill_md:
        return skill_md.rstrip("\n") + "\n" + link + "\n"
    return skill_md.rstrip("\n") + "\n\n## References\n\n" + link + "\n"


def _frontmatter_integrity(before: str, after: str) -> str:
    old, _ = parse_frontmatter(before)
    new, _ = parse_frontmatter(after)
    if not new:
        return "the patch broke the SKILL.md frontmatter"
    for key in set(old) | set(new):
        if key in _PROTECTED_FRONTMATTER_EXCEPT:
            continue
        if old.get(key) != new.get(key):
            return f"a patch may not change the {key!r} frontmatter field (the loop and the owner manage it)"
    return ""


def _validate_skill_md(op: Operation, md_text: str, old_md: str | None) -> list[str]:
    """The SKILL.md that would result must be a valid Agent Skill, and a new or
    changed description must do the one job a description has."""
    import tempfile

    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / SKILL_FILE
        path.write_text(md_text, encoding="utf-8")
        manifest = parse_skill_md(path)
    if manifest.errors:
        problems.append("SKILL.md would not be valid: " + "; ".join(str(e) for e in manifest.errors))
    data, _ = parse_frontmatter(md_text)
    description = str(data.get("description") or "")
    old_description = str(parse_frontmatter(old_md)[0].get("description") or "") if old_md else None
    if old_description is None:  # a new skill must say when to use it
        if len(description) > MAX_DESCRIPTION:
            problems.append(f"the description is {len(description)} characters; the limit is {MAX_DESCRIPTION}")
        if not description.lower().startswith("use when"):
            problems.append("the description must start with 'Use when': it is how the agent decides to load the skill")
    elif description != old_description:
        # An existing skill's description is the owner's wording, and for an
        # executable skill it is also the tool description the model reads. The
        # loop may not impose a house style on it (the first real review rewrote
        # one to start "Use when" and lost "no credentials needed"), and may not
        # shrink what it says. It may only extend it or correct it.
        if len(description) > MAX_DESCRIPTION:
            problems.append(f"the description is {len(description)} characters; the limit is {MAX_DESCRIPTION}")
        if not description:
            problems.append("a patch may not empty the description")
        elif len(description) < len(old_description) * 0.8:
            problems.append(
                "a patch may not shorten an existing description by more than a fifth: it is the owner's wording "
                "and how the agent chooses the skill; extend or correct it, do not replace it"
            )
    if len(str(data.get("name") or "")) > 64:
        problems.append("the skill name is longer than 64 characters")
    return problems


def gate_batch(
    ops: list[Operation],
    *,
    skills_dir: Path,
    events: dict[str, dict[str, Any]],
    batch_ids: set[str],
    config: dict[str, Any] | None = None,
    today: str | None = None,
    already_touched: set[str] | None = None,
) -> list[GateVerdict]:
    """Gate every operation of one review. One change per skill per review: two
    proposals to the same skill would each be planned against the original text
    and could not both apply, so the reviewer must combine them."""
    verdicts: list[GateVerdict] = []
    touched: set[str] = set(already_touched or ())
    for op in ops:
        verdict = gate_operation(op, skills_dir=skills_dir, events=events, batch_ids=batch_ids, config=config, today=today)
        if verdict.accepted and op.skill in touched:
            verdict = GateVerdict(op, False, [f"{op.skill!r} already has a change in this review; combine them into one"])
        elif verdict.accepted:
            touched.add(op.skill)
        verdicts.append(verdict)
    return verdicts
