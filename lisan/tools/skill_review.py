"""Reviewing finished work for lessons: stage 2 of the learning loop.

    unreviewed events --> batch --> reviewer (a model) --> gate (code) --> artifact

The reviewer reads a batch of frozen events plus the skills they used and
proposes operations; `skill_gate` decides each one and works out exactly what it
would write. In shadow mode that is all that happens: the result is a readable
artifact for the owner. Nothing here writes a skill — applying accepted changes
is the next stage — so a review can be run, inspected and discarded freely.

Events are marked reviewed only after a review completes, so a reviewer that was
unreachable or returned nonsense leaves its events to be read again; a provider
failure is never mistaken for "nothing to learn".
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import learning
from .skill_apply import Applied, ApplyRefused, apply_change
from .skill_gate import GateVerdict, Operation, gate_batch, is_repairable, parse_operations
from .skill_lifecycle import evaluate_lifecycle

DEFAULT_AUTO_APPLY_CAP = 3  # changes applied per review: a bound on the blast radius of any one review

DEFAULT_CHAR_BUDGET = 160_000
DEFAULT_MAX_EVENTS = 12
_RESULT_CAP = 6000  # per tool result, in the prompt (the frozen event keeps the full text)
_BODY_CAP = 12_000


@dataclass
class ReviewResult:
    review_id: str
    mode: str
    event_ids: list[str]
    summary: str
    verdicts: list[GateVerdict]
    artifact: Path | None = None
    reviewer: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    skipped: str | None = None
    lifecycle: list[dict[str, Any]] = field(default_factory=list)

    @property
    def applied(self) -> list[GateVerdict]:
        return [v for v in self.verdicts if v.applied is not None]

    @property
    def accepted(self) -> list[GateVerdict]:
        return [v for v in self.verdicts if v.accepted]

    @property
    def rejected(self) -> list[GateVerdict]:
        return [v for v in self.verdicts if not v.accepted and v.op.op != "none"]


# ── What the reviewer reads ──────────────────────────────────────────────────

def _clip(text: Any, limit: int) -> str:
    text = str(text if text is not None else "")
    return text if len(text) <= limit else text[:limit] + f"\n[... {len(text) - limit:,} more characters ...]"


def _age_days(occurred_at: str, now: float | None) -> int | None:
    try:
        import calendar

        then = calendar.timegm(time.strptime(str(occurred_at), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None
    return max(0, int(((now if now is not None else time.time()) - then) // 86400))


def render_event(event: dict[str, Any], *, now: float | None = None) -> str:
    """One event as the reviewer sees it, including how old it is: the system
    changes quickly, and a lesson about the agent's own environment drawn from
    old events can be obsolete on arrival."""
    age = _age_days(event.get("occurred_at"), now)
    head = (
        f"### EVENT {event['id']}\n"
        f"kind: {event['kind']}  when: {event['occurred_at']}"
        + (f" ({age} days ago)" if age is not None else "")
        + f"  outcome: {event.get('outcome')}  "
        f"sources: {', '.join(event.get('sources') or []) or '-'}  tainted: {bool(event.get('tainted'))}"
    )
    payload = event.get("payload") or {}
    lines = [head]
    kind = event["kind"]
    if kind == "turn":
        lines.append(f"OWNER SAID: {_clip(payload.get('text'), 3000)}")
        if event.get("skills_used"):
            lines.append("SKILLS USED: " + ", ".join(u["skill"] for u in event["skills_used"]))
        for i, call in enumerate(payload.get("tool_calls") or [], start=1):
            args = json.dumps(call.get("args") or {}, ensure_ascii=False, default=str)
            lines.append(f"TOOL CALL {i}: {call.get('tool')}({_clip(args, 1500)})")
            lines.append(f"  RESULT: {_clip(call.get('result'), _RESULT_CAP)}")
        lines.append(f"AGENT REPLIED: {_clip(payload.get('response'), 3000)}")
    elif kind == "plan":
        lines.append(f"PLAN GOAL: {payload.get('goal')}")
        for i, step in enumerate(payload.get("steps") or [], start=1):
            lines.append(f"STEP {i} [{step.get('kind')}, {step.get('status')}]: {_clip(step.get('description'), 800)}")
            if step.get("children"):
                lines.append("  workers: " + " | ".join(_clip(c, 300) for c in step["children"]))
            lines.append(f"  RESULT: {_clip(step.get('result'), _RESULT_CAP)}")
    elif kind == "group":
        lines.append(f"GOAL: {payload.get('goal')}")
        for i, child in enumerate(payload.get("children") or [], start=1):
            lines.append(f"WORKER {i} [{child.get('profile')}, {child.get('status')}]: {_clip(child.get('brief'), 1500)}")
            lines.append(f"  FOUND: {_clip(child.get('text') or child.get('error'), _RESULT_CAP)}")
    else:  # adjutant
        lines.append(f"TASK: {payload.get('task_id')} ({', '.join(payload.get('kinds') or [])}) attempt {payload.get('attempt')}")
        for action in payload.get("actions") or []:
            lines.append(f"  ACTION: {_clip(action, 600)}")
        for error in payload.get("errors") or []:
            lines.append(f"  ERROR: {_clip(error, 600)}")
    return "\n".join(lines)


def select_batch(events: list[dict[str, Any]], *, char_budget: int, max_events: int) -> list[dict[str, Any]]:
    """Oldest first, as many as fit the context budget. Always at least one, so
    a single huge event cannot wedge the queue."""
    batch: list[dict[str, Any]] = []
    used = 0
    for event in events:
        size = len(render_event(event))
        if batch and (used + size > char_budget or len(batch) >= max_events):
            break
        batch.append(event)
        used += size
    return batch


def skill_index(skills_dir: Path, db_path: Path | None) -> str:
    from .skill_history import read_provenance
    from .skill_loader import load_skills

    usage = {u["skill"]: u for u in learning.skill_usage_summary(db_path, days=None)}
    lines = []
    for skill in load_skills(skills_dir):
        name = skill["name"]
        prov = read_provenance(skills_dir, name)
        bits = [prov["origin"]]
        if prov.get("status"):
            bits.append(str(prov["status"]))
        if prov.get("revised_by"):
            bits.append(f"revised by {prov['revised_by']}")
        if prov["pinned"]:
            bits.append("PINNED: do not propose changes")
        if name in usage:
            u = usage[name]
            bits.append(f"used {u['uses']}x: " + ", ".join(f"{k} {v}" for k, v in sorted(u["outcomes"].items())))
        lines.append(f"- {name} ({'; '.join(bits)}): {skill.get('description')}")
    return "\n".join(lines) or "(no skills exist yet)"


def skill_bodies(events: list[dict[str, Any]], skills_dir: Path) -> str:
    """Full text of every skill the batch used, so the reviewer can quote it."""
    from .skill_format import SKILL_FILE

    names: list[str] = []
    for event in events:
        for used in event.get("skills_used") or []:
            if used["skill"] not in names:
                names.append(used["skill"])
    parts = []
    for name in names:
        path = skills_dir / name / SKILL_FILE
        if path.is_file():
            parts.append(f"=== {name}/SKILL.md ===\n{_clip(path.read_text(encoding='utf-8'), _BODY_CAP)}")
    return "\n\n".join(parts) or "(none of the skills in this batch has a body to show)"


def current_tools() -> str:
    """The tools the agent has today, so a lesson is written in today's words.
    Older events may show retired names; the aliases are listed."""
    from .execution_tools import LEGACY_TOOL_NAMES, TOOLS

    names = ", ".join(sorted(t["name"] for t in TOOLS))
    renamed = "; ".join(f"{old} is now {new}" for old, new in LEGACY_TOOL_NAMES.items())
    return names + (f"\nRenamed (older events show the old names): {renamed}" if renamed else "")


def build_input(batch: list[dict[str, Any]], skills_dir: Path, db_path: Path | None) -> str:
    return "\n\n".join([
        "TODAY: " + time.strftime("%Y-%m-%d", time.gmtime()),
        "CURRENT_TOOLS:\n" + current_tools(),
        "SKILL_INDEX:\n" + skill_index(skills_dir, db_path),
        "SKILL_BODIES:\n" + skill_bodies(batch, skills_dir),
        "EVENTS (" + str(len(batch)) + "):\n" + "\n\n".join(render_event(e) for e in batch),
    ])


# ── Running a review ─────────────────────────────────────────────────────────

Reviewer = Callable[[str], tuple[dict[str, Any], dict[str, Any]]]


def default_reviewer(vault: Path, config: dict[str, Any], *, provider: str | None = None, model: str | None = None) -> Reviewer:
    """The real reviewer: raises on a provider failure or an unusable reply, so a
    review that did not happen is never recorded as one that found nothing."""
    from ..agents.skill_reviewer import SkillReviewerAgent

    agent = SkillReviewerAgent(vault=vault, config=config)

    def _run(prompt_input: str) -> tuple[dict[str, Any], dict[str, Any]]:
        started = time.monotonic()
        result = agent.run(
            prompt_input, significance="high", provider=provider, model=model,
            provider_error_mode="raise", parse_error_mode="raise",
        )
        data = result.data if isinstance(result.data, dict) else {}
        meta = {
            "provider": getattr(result.response, "provider", None),
            "model": getattr(result.response, "model", None),
            "seconds": round(time.monotonic() - started, 1),
            "prompt_chars": len(prompt_input),
        }
        return data, meta

    return _run


def run_review(
    vault: Path,
    db_path: Path | None,
    config: dict[str, Any],
    *,
    event_ids: list[str] | None = None,
    dry_run: bool = False,
    reviewer: Reviewer | None = None,
    skills_dir: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    today: str | None = None,
) -> ReviewResult:
    """Review a batch of events. `event_ids` names them explicitly (otherwise: the
    oldest unreviewed). `dry_run` reads and judges but marks nothing reviewed and
    works in any mode; a real run needs `shadow` or `auto`."""
    from ..paths import skills_root

    mode = learning.learning_mode(config)
    review_id = f"review-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"
    blank = ReviewResult(review_id, mode, [], "", [], dry_run=dry_run)
    if not dry_run and mode not in ("shadow", "auto"):
        blank.skipped = f"learning.mode is {mode!r}; reviews run in shadow or auto"
        return blank

    skills_dir = skills_dir if skills_dir is not None else skills_root()
    settings = config.get("learning") or {}
    by_id = {e["id"]: e for e in learning.iter_events(vault)}
    if event_ids:
        missing = [i for i in event_ids if i not in by_id]
        if missing:
            blank.skipped = f"no such event(s): {', '.join(missing)}"
            return blank
        candidates = [by_id[i] for i in event_ids]
    else:
        candidates = [by_id[i] for i in learning.unreviewed_ids(db_path) if i in by_id]
    if not candidates:
        blank.skipped = "nothing to review"
        return blank

    batch = select_batch(
        candidates,
        char_budget=int(settings.get("review_char_budget") or DEFAULT_CHAR_BUDGET),
        max_events=int(settings.get("max_review_events") or DEFAULT_MAX_EVENTS),
    )
    reviewer = reviewer or default_reviewer(vault, config, provider=provider, model=model)
    prompt_input = build_input(batch, skills_dir, db_path)
    data, meta = reviewer(prompt_input)

    ops = parse_operations(data)
    batch_events = {e["id"]: e for e in batch}
    gate_kw = dict(skills_dir=skills_dir, events=batch_events, batch_ids=set(batch_events), config=config, today=today)
    verdicts = gate_batch(ops, **gate_kw)
    verdicts = _revise_once(verdicts, reviewer, prompt_input, meta, gate_kw)
    result = ReviewResult(
        review_id=review_id, mode=mode, event_ids=[e["id"] for e in batch],
        summary=str(data.get("summary") or "").strip(), verdicts=verdicts, reviewer=meta, dry_run=dry_run,
    )
    if mode == "auto" and not dry_run:
        result.lifecycle = evaluate_lifecycle(skills_dir, db_path)
        _apply_accepted(result, skills_dir, cap=int(settings.get("auto_apply_max_per_review") or DEFAULT_AUTO_APPLY_CAP))
        from .learning_notice import skills_learned

        skills_learned(vault, result.review_id, result.accepted, result.lifecycle, config=config)
    result.artifact = write_artifact(vault, result)
    if not dry_run:
        learning.mark_reviewed(vault, db_path, result.event_ids, review_id)
    return result


def _apply_accepted(result: ReviewResult, skills_dir: Path, *, cap: int) -> None:
    """Auto mode: write the changes the gate passed, up to a per-review cap. A
    change that cannot be applied safely (the file moved on, a pin appeared) is
    recorded as not applied and never forced."""
    done = 0
    for verdict in result.accepted:
        if done >= cap:
            verdict.apply_note = f"deferred: this review already applied {cap} change(s) (learning.auto_apply_max_per_review)"
            continue
        try:
            verdict.applied = apply_change(
                verdict.change, skills_dir=skills_dir, review_id=result.review_id,
                reason=verdict.op.rationale, events=verdict.change.provenance.get("source_events"),
            )
            done += 1
        except ApplyRefused as exc:
            verdict.apply_note = f"not applied: {exc}"


def apply_review(
    vault: Path,
    db_path: Path | None,
    config: dict[str, Any],
    review_id: str,
    *,
    ops: list[int] | None = None,
    skills_dir: Path | None = None,
    today: str | None = None,
) -> list[GateVerdict]:
    """The owner applies proposals from a past review by hand (the way to use
    shadow mode). Each is re-gated against the skills and events as they are NOW
    before anything is written, so a proposal that has gone stale is refused, not
    forced. `ops` are 1-based positions as numbered in the artifact."""
    from ..paths import skills_root

    skills_dir = skills_dir if skills_dir is not None else skills_root()
    found = list(_artifact_dir(vault).glob(f"*/{review_id}.json"))
    if not found:
        raise ValueError(f"no review {review_id!r}")
    record = json.loads(found[0].read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in learning.iter_events(vault) if e["id"] in set(record["events"])}
    chosen = []
    for position, item in enumerate(record["operations"], start=1):
        if ops is not None and position not in ops:
            continue
        proposal = dict(item.get("proposal") or {})
        proposal.update(op=item["op"], skill=item["skill"], evidence=item.get("evidence") or [], rationale=item.get("rationale") or "")
        chosen.append(Operation(**{k: v for k, v in proposal.items() if k in Operation.__dataclass_fields__}))
    verdicts = gate_batch(chosen, skills_dir=skills_dir, events=by_id, batch_ids=set(by_id), config=config, today=today)
    for verdict in verdicts:
        if not verdict.accepted:
            continue
        try:
            verdict.applied = apply_change(
                verdict.change, skills_dir=skills_dir, review_id=review_id, actor="owner",
                reason=f"applied by the owner from {review_id}: {verdict.op.rationale}",
                events=verdict.change.provenance.get("source_events"),
            )
        except ApplyRefused as exc:
            verdict.apply_note = f"not applied: {exc}"
    return verdicts


def _revise_once(
    verdicts: list[GateVerdict],
    reviewer: Reviewer,
    prompt_input: str,
    meta: dict[str, Any],
    gate_kw: dict[str, Any],
) -> list[GateVerdict]:
    """Give proposals refused for a slip of form one chance to be corrected.

    The reviewer is shown exactly why each was discarded and asked for corrected
    versions only. The gate then judges those from scratch, so a revision gets no
    leniency. Safety refusals are never sent back. A failure here costs nothing
    already earned: the first pass's verdicts stand."""
    repairable = [v for v in verdicts if is_repairable(v)]
    if not repairable:
        return verdicts
    lines = [
        "REVISION REQUEST: the automatic gate discarded the operations below. The reasons are exact. "
        "Return a new JSON object (same schema) holding ONLY corrected versions of these operations; omit any "
        "that the reason shows should not be made. Do not repeat operations that were accepted.",
        "",
    ]
    for i, v in enumerate(repairable, start=1):
        proposal = {k: val for k, val in vars(v.op).items() if val}
        lines.append(f"DISCARDED {i}: {v.op.op} {v.op.skill}")
        lines += [f"  reason: {r}" for r in v.reasons]
        lines.append(f"  your operation: {json.dumps(proposal, ensure_ascii=False)}")
    started = time.monotonic()
    try:
        data, _ = reviewer(prompt_input + "\n\n" + "\n".join(lines))
    except Exception as exc:  # the first pass stands
        meta["revision_error"] = f"{exc.__class__.__name__}: {exc}"
        return verdicts
    meta["revised"] = True
    meta["revision_seconds"] = round(time.monotonic() - started, 1)

    kept = [v for v in verdicts if v not in repairable]
    accepted_skills = {v.op.skill for v in kept if v.accepted}
    # A reviewer asked for corrections may helpfully repeat what was already
    # accepted; that is not a new proposal and must not appear as a refusal.
    offered = [op for op in parse_operations(data) if op.skill not in accepted_skills]
    revised = gate_batch(offered, **gate_kw)
    earlier = {v.op.skill: v.reasons for v in repairable}
    for v in revised:
        v.first_attempt_reasons = earlier.get(v.op.skill)
    return kept + revised


# ── The artifact the owner reads ─────────────────────────────────────────────

def _artifact_dir(vault: Path) -> Path:
    return learning.learning_root(vault) / "reviews"


def write_artifact(vault: Path, result: ReviewResult) -> Path:
    month = result.review_id.split("-")[1][:6]
    folder = _artifact_dir(vault) / f"{month[:4]}-{month[4:6]}"
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "review_id": result.review_id, "mode": result.mode, "dry_run": result.dry_run,
        "events": result.event_ids, "summary": result.summary, "reviewer": result.reviewer,
        "operations": [
            {
                "op": v.op.op, "skill": v.op.skill, "file": v.op.file, "evidence": v.op.evidence,
                "rationale": v.op.rationale, "accepted": v.accepted, "reasons": v.reasons,
                "first_attempt_reasons": v.first_attempt_reasons,
                "applied": ({"snapshot": v.applied.snapshot, "version": [v.applied.version_before, v.applied.version_after],
                             "files": v.applied.files} if v.applied else None),
                "apply_note": v.apply_note,
                "diff": v.change.diff if v.change else "",
                "provenance": v.change.provenance if v.change else None,
                "version": [v.change.version_before, v.change.version_after] if v.change else None,
                "proposal": {k: val for k, val in vars(v.op).items() if val and k not in ("evidence", "rationale")},
            }
            for v in result.verdicts
        ],
    }
    (folder / f"{result.review_id}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    md = folder / f"{result.review_id}.md"
    md.write_text(render_artifact(result), encoding="utf-8")
    return md


def render_artifact(result: ReviewResult) -> str:
    lines = [
        f"# Skill review {result.review_id}",
        "",
        f"- mode: {result.mode}{' (dry run: nothing marked reviewed)' if result.dry_run else ''}",
        f"- events read: {len(result.event_ids)}",
        f"- proposals: {len([v for v in result.verdicts if v.op.op != 'none'])}"
        f" — {len(result.accepted)} pass the gate, {len(result.rejected)} refused"
        + (f", {len(result.applied)} applied" if result.mode == "auto" and not result.dry_run else ""),
    ]
    for change in result.lifecycle:
        lines.append(f"- lifecycle: `{change['skill']}` {change['from'] or 'unset'} -> {change['to']} ({change['reason']})")
    if result.reviewer:
        lines.append(f"- reviewer: {result.reviewer.get('provider')} {result.reviewer.get('model') or ''}, "
                     f"{result.reviewer.get('seconds')}s, {result.reviewer.get('prompt_chars', 0):,} characters read")
    lines += ["", "## What the reviewer found", "", result.summary or "(no summary)", ""]
    for i, v in enumerate(result.verdicts, start=1):
        if v.op.op == "none":
            continue
        status = "APPLIED" if v.applied else ("NOT APPLIED" if v.accepted and v.apply_note else ("WOULD APPLY" if v.accepted else "REFUSED"))
        lines += [f"## {i}. {v.op.op} `{v.op.skill}` — {status}", "", f"Why: {v.op.rationale}", f"Evidence: {', '.join(v.op.evidence) or '-'}"]
        if v.first_attempt_reasons:
            lines.append("Revised once: the first attempt was discarded because " + "; ".join(v.first_attempt_reasons))
        if v.change and v.change.provenance.get("tainted"):
            lines.append(f"Provenance: drew on external sources ({', '.join(v.change.provenance['sources'])})")
        if v.applied:
            lines.append(f"Applied: previous version saved as {v.applied.snapshot or '(new skill)'}; "
                         f"undo with `lisan skills rollback {v.op.skill} {v.applied.snapshot}`" if v.applied.snapshot
                         else "Applied: a new skill, provisional until it has proven itself.")
        if v.apply_note:
            lines.append(v.apply_note)
        if v.accepted and v.change:
            lines += ["", "```diff", v.change.diff.rstrip(), "```"]
        else:
            lines += ["", "Refused because:"] + [f"- {r}" for r in v.reasons]
        lines.append("")
    return "\n".join(lines)


def digest(result: ReviewResult) -> str | None:
    """One message for the owner, or None when there is nothing to tell."""
    proposals = [v for v in result.verdicts if v.op.op != "none"]
    if not proposals and not result.lifecycle:
        return None
    head = f"Learning review: {len(proposals)} proposal(s) from {len(result.event_ids)} event(s) — "
    head += f"{len(result.accepted)} pass the gate, {len(result.rejected)} refused."
    if result.applied:
        head += f" {len(result.applied)} applied."
    lines = [head]
    for v in proposals[:6]:
        mark = "✓✓" if v.applied else ("✓" if v.accepted else "✗")
        lines.append(f"{mark} {v.op.op} {v.op.skill}: {v.op.rationale[:140]}")
    for change in result.lifecycle[:4]:
        lines.append(f"• {change['skill']} is now {change['to']} ({change['reason']})")
    lines.append(f"Read it: lisan learning review-show {result.review_id}")
    return "\n".join(lines)


def list_reviews(vault: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted(_artifact_dir(vault).glob("*/review-*.json"), reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ops = [o for o in record.get("operations", []) if o.get("op") != "none"]
        out.append({
            "review_id": record["review_id"], "mode": record.get("mode"), "dry_run": record.get("dry_run"),
            "events": len(record.get("events", [])), "proposals": len(ops),
            "accepted": sum(1 for o in ops if o.get("accepted")), "summary": record.get("summary", ""),
        })
    return out


def show_review(vault: Path, review_id: str) -> str | None:
    for path in _artifact_dir(vault).glob(f"*/{review_id}.md"):
        return path.read_text(encoding="utf-8")
    return None


# ── The queued job ───────────────────────────────────────────────────────────

def run_review_job(
    job: dict[str, Any],
    *,
    vault: Path,
    db_path: Path | None,
    send_fn: Any = None,
    config: dict[str, Any] | None = None,
    reviewer: Reviewer | None = None,
    skills_dir: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """`skill.review`: read the oldest unreviewed events once enough have piled
    up and the system has been quiet. Waits (without spending an attempt) rather
    than competing with work in progress; raises on a provider failure so the
    queue retries it and the events stay unreviewed."""
    from ..config import load_config
    from .jobs import JobDeferred

    config = config if config is not None else load_config()
    mode = learning.learning_mode(config)
    if mode not in ("shadow", "auto"):
        return {"skipped": f"learning.mode is {mode!r}"}
    settings = config.get("learning") or {}
    if len(learning.unreviewed_ids(db_path)) < learning.review_every(config):
        return {"skipped": "not enough unreviewed events yet"}

    last = learning.last_recorded_at(db_path)
    quiet = float(settings.get("min_idle_minutes", 5)) * 60
    if last:
        age = (now if now is not None else time.time()) - _epoch(last)
        if age < quiet:
            raise JobDeferred(f"events are still arriving (last {int(age)}s ago); waiting for quiet", retry_after_seconds=int(quiet - age) + 5)

    result = run_review(vault, db_path, config, reviewer=reviewer, skills_dir=skills_dir)
    message = digest(result) if bool(settings.get("digest", True)) else None
    delivered = False
    if message:
        try:
            if send_fn is not None:
                send_fn(message, None)
            else:
                from .scheduler import _deliver_owner_message

                _deliver_owner_message(message, config=config)
            delivered = True
        except Exception as exc:
            try:
                from .log import log_error

                log_error(vault, "skill.review: digest delivery failed", exc)
            except Exception:
                pass
    learning.maybe_enqueue_review(vault, db_path, config)  # a backlog drains one batch at a time
    return {
        "review_id": result.review_id, "events": len(result.event_ids), "proposals": len(result.verdicts),
        "accepted": len(result.accepted), "refused": len(result.rejected), "applied": len(result.applied),
        "delivered": delivered,
        "artifact": str(result.artifact) if result.artifact else None, "skipped": result.skipped,
    }


def _epoch(stamp: str) -> float:
    import calendar

    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
