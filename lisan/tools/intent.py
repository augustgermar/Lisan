"""Commander's intent: primer/intent.md — the authority document.

Every Adjutant execution decision is checked against this file. It is a
primer document (excluded from the general record validator), so it
carries its own dedicated validation, its own version history, and a
pure delegation-resolution function the Adjutant gate builds on.

Deterministic throughout: parsing, validation, resolution, and
versioning are code. No LLM is ever consulted about authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..frontmatter import FrontmatterError, MarkdownDocument, load_markdown, parse_markdown

# ---------------------------------------------------------------------------
# Vocabulary (spec: Adjutant SPEC §1.3)

CAPABILITIES = {
    "read_files",
    "write_files",
    "run_local_scripts",
    "web_research",
    "send_outbound_message",
    "spend_money",
    "git_push",
    "publish",
    "delete_files",
}

MODES = {"report_only", "execute", "disabled"}

REQUIRED_SECTIONS = [
    "Mission",
    "Priorities",
    "Standing Delegations",
    "Escalation Rules",
    "Never",
]

# Verdicts, least to most restrictive. Most restrictive wins on conflict.
EXECUTE = "execute"
CONFIRM = "confirm"
REPORT_ONLY = "report_only"
DENY = "deny"
_RESTRICTIVENESS = {EXECUTE: 0, CONFIRM: 1, REPORT_ONLY: 2, DENY: 3}

# Global rules that take integer values, not authority keywords.
_GLOBAL_LIMITS = {"max_task_wall_seconds", "max_tasks_per_cycle"}
_GLOBAL_AUTHORITY_VALUES = {"confirm_always", "never", "allow"}


class IntentError(ValueError):
    """intent.md is missing or invalid. Callers that act on authority
    (the Adjutant) must treat this as fail-closed: refuse to start."""


@dataclass(slots=True)
class Intent:
    frontmatter: dict[str, Any]
    sections: dict[str, str]
    delegations: dict[str, Any]
    content_hash: str
    path: Path | None = None

    @property
    def version(self) -> int:
        return int(self.frontmatter.get("version", 0))


@dataclass(slots=True)
class Verdict:
    decision: str  # execute | confirm | report_only | deny
    rule: str      # the intent rule that produced the decision
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Paths

def intent_path(vault: Path) -> Path:
    return vault / "primer" / "intent.md"


def intent_history_dir(vault: Path) -> Path:
    return vault / "primer" / "intent-history"


def _known_hash_path(vault: Path) -> Path:
    return intent_history_dir(vault) / ".known-hash"


# ---------------------------------------------------------------------------
# Template

INTENT_TEMPLATE_BODY = """\
# Mission

_One paragraph. What the whole system is for._

# Priorities

1. _Highest priority first; ties are broken top-down._
2. _Second priority._

> The Adjutant ranks pollable tasks by word overlap between a task's
> summary and these lines (first match wins; unmatched tasks rank last,
> then by due date). Write priorities in the same vocabulary your tasks
> use — "backups", "invoices", "the garden" — not abstractions.

# Standing Delegations

Every arena starts report-only. Widen authority per-arena, per-capability,
explicitly, here. Resolution order: Never-rules -> global -> arena ->
defaults; most restrictive wins.

```json
{
  "defaults": { "mode": "report_only" },
  "arenas": {
    "legal": {
      "mode": "report_only",
      "capabilities": ["read_files"],
      "confirm_required": ["*"],
      "outbound_comms": "never"
    }
  },
  "global": {
    "spend_money": "confirm_always",
    "send_outbound_message": "confirm_always",
    "delete_files": "confirm_always",
    "max_task_wall_seconds": 600,
    "max_tasks_per_cycle": 5
  }
}
```

# Escalation Rules

- _When to stop and ask, regardless of delegations._

# Never

- _Absolute prohibitions. These override everything, including direct
  task instructions captured in open loops._
"""


def default_intent_document(today: str | None = None) -> str:
    from ..frontmatter import dump_markdown

    today = today or date.today().isoformat()
    frontmatter = {
        "id": "intent-current",
        "type": "intent",
        "created": today,
        "updated": today,
        "status": "active",
        "version": 1,
        "review_after": _next_review_date(today),
    }
    return dump_markdown(frontmatter, INTENT_TEMPLATE_BODY)


def _next_review_date(today: str) -> str:
    d = date.fromisoformat(today)
    # Quarterly review cadence; the owner can set any date they like.
    month = d.month + 3
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(d.day, 28)
    return date(year, month, day).isoformat()


# ---------------------------------------------------------------------------
# Parsing

_SECTION_RE = re.compile(r"^# +(.+?)\s*$", re.MULTILINE)
_FENCED_JSON_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


def _split_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(body))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[match.group(1).strip()] = body[start:end].strip()
    return sections


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_intent(text: str, *, path: Path | None = None) -> Intent:
    """Parse without judging. Raises IntentError only on structural failure
    (bad frontmatter, delegations block absent or not JSON); semantic
    problems are the validator's job so they can all be reported at once."""
    try:
        doc: MarkdownDocument = parse_markdown(text)
    except FrontmatterError as exc:
        raise IntentError(f"intent.md frontmatter invalid: {exc}") from exc
    sections = _split_sections(doc.body)
    delegations: dict[str, Any] = {}
    delegations_text = sections.get("Standing Delegations", "")
    match = _FENCED_JSON_RE.search(delegations_text)
    if match:
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise IntentError(f"Standing Delegations JSON invalid: {exc}") from exc
        if not isinstance(parsed, dict):
            raise IntentError("Standing Delegations JSON must be an object")
        delegations = parsed
    return Intent(
        frontmatter=doc.frontmatter,
        sections=sections,
        delegations=delegations,
        content_hash=_content_hash(text),
        path=path,
    )


# ---------------------------------------------------------------------------
# Validation

def validate_intent_text(text: str) -> list[str]:
    issues: list[str] = []
    try:
        intent = parse_intent(text)
    except IntentError as exc:
        return [str(exc)]

    fm = intent.frontmatter
    if fm.get("id") != "intent-current":
        issues.append(f"id must be 'intent-current', got {fm.get('id')!r}")
    if fm.get("type") != "intent":
        issues.append(f"type must be 'intent', got {fm.get('type')!r}")
    if fm.get("status") not in {"active", "draft"}:
        issues.append(f"status must be 'active' or 'draft', got {fm.get('status')!r}")
    version = fm.get("version")
    if not isinstance(version, int) or version < 1:
        issues.append(f"version must be a positive integer, got {version!r}")
    for field_name in ("created", "updated", "review_after"):
        value = fm.get(field_name)
        if not value:
            issues.append(f"missing frontmatter field: {field_name}")
            continue
        try:
            date.fromisoformat(str(value))
        except ValueError:
            issues.append(f"invalid ISO date in {field_name}: {value!r}")

    for section in REQUIRED_SECTIONS:
        if section not in intent.sections:
            issues.append(f"missing required section: # {section}")
        elif not intent.sections[section].strip():
            issues.append(f"required section is empty: # {section}")

    if "Standing Delegations" in intent.sections and not intent.delegations:
        issues.append("Standing Delegations must contain a fenced ```json block")
    if intent.delegations:
        issues.extend(_validate_delegations(intent.delegations))
    return issues


def _validate_delegations(delegations: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    defaults = delegations.get("defaults")
    if not isinstance(defaults, dict):
        issues.append("delegations.defaults must be an object")
    elif defaults.get("mode") not in MODES:
        issues.append(f"delegations.defaults.mode must be one of {sorted(MODES)}")

    arenas = delegations.get("arenas", {})
    if not isinstance(arenas, dict):
        issues.append("delegations.arenas must be an object")
        arenas = {}
    for name, arena in arenas.items():
        prefix = f"delegations.arenas.{name}"
        if not isinstance(arena, dict):
            issues.append(f"{prefix} must be an object")
            continue
        if arena.get("mode") not in MODES:
            issues.append(f"{prefix}.mode must be one of {sorted(MODES)}")
        for cap in arena.get("capabilities", []) or []:
            if cap not in CAPABILITIES:
                issues.append(f"{prefix}.capabilities: unknown capability {cap!r}")
        for cap in arena.get("confirm_required", []) or []:
            if cap != "*" and cap not in CAPABILITIES:
                issues.append(f"{prefix}.confirm_required: unknown capability {cap!r}")
        outbound = arena.get("outbound_comms")
        if outbound is not None and outbound != "never":
            issues.append(f"{prefix}.outbound_comms must be 'never' or absent")

    global_rules = delegations.get("global", {})
    if not isinstance(global_rules, dict):
        issues.append("delegations.global must be an object")
        global_rules = {}
    for key, value in global_rules.items():
        if key in _GLOBAL_LIMITS:
            if not isinstance(value, int) or value < 1:
                issues.append(f"delegations.global.{key} must be a positive integer")
        elif key in CAPABILITIES:
            if value not in _GLOBAL_AUTHORITY_VALUES:
                issues.append(
                    f"delegations.global.{key} must be one of {sorted(_GLOBAL_AUTHORITY_VALUES)}"
                )
        else:
            issues.append(f"delegations.global: unknown rule {key!r}")
    return issues


def validate_intent_file(vault: Path) -> list[str]:
    path = intent_path(vault)
    if not path.exists():
        return [f"intent.md not found at {path}"]
    return validate_intent_text(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Loading (fail closed)

def load_intent(vault: Path) -> Intent:
    """Load and fully validate. Any issue raises IntentError — the Adjutant
    must not run against an intent it cannot trust."""
    path = intent_path(vault)
    if not path.exists():
        raise IntentError(f"intent.md not found at {path} — run `lisan intent init`")
    text = path.read_text(encoding="utf-8")
    issues = validate_intent_text(text)
    if issues:
        raise IntentError("intent.md invalid: " + "; ".join(issues))
    return parse_intent(text, path=path)


# ---------------------------------------------------------------------------
# Delegation resolution (pure; the Adjutant gate wraps this with the
# task-kind -> capabilities mapping and audit logging)

def resolve_delegation(delegations: dict[str, Any], arena: str, capability: str) -> Verdict:
    """(arena, capability) -> verdict, per spec resolution order:
    never-rules -> global rules -> arena rules -> defaults.
    Most restrictive wins on conflict."""
    candidates: list[Verdict] = []
    arenas = delegations.get("arenas", {}) or {}
    arena_rules = arenas.get(arena)
    defaults = delegations.get("defaults", {}) or {}
    global_rules = delegations.get("global", {}) or {}

    # Never-rules (machine-readable ones).
    if isinstance(arena_rules, dict):
        if arena_rules.get("mode") == "disabled":
            return Verdict(DENY, f"arenas.{arena}.mode=disabled", [f"arena {arena!r} is disabled"])
        if arena_rules.get("outbound_comms") == "never" and capability == "send_outbound_message":
            return Verdict(
                DENY,
                f"arenas.{arena}.outbound_comms=never",
                [f"outbound communication is never permitted in arena {arena!r}"],
            )
    if global_rules.get(capability) == "never":
        return Verdict(DENY, f"global.{capability}=never", [f"{capability} is never permitted"])

    # Global rules.
    if global_rules.get(capability) == "confirm_always":
        candidates.append(
            Verdict(CONFIRM, f"global.{capability}=confirm_always", [f"{capability} always requires confirmation"])
        )

    # Arena rules, or defaults when the arena is unlisted.
    if isinstance(arena_rules, dict):
        mode = arena_rules.get("mode", defaults.get("mode", "report_only"))
        if mode == "report_only":
            candidates.append(Verdict(REPORT_ONLY, f"arenas.{arena}.mode=report_only"))
        else:  # execute
            granted = arena_rules.get("capabilities", []) or []
            confirm_required = arena_rules.get("confirm_required", []) or []
            # A capability named explicitly in confirm_required is a
            # grant-with-confirmation (the spec's own example lists git_push
            # only there). "*" merely tightens the granted list — it never
            # widens it, so default deny holds.
            if capability in confirm_required:
                candidates.append(
                    Verdict(
                        CONFIRM,
                        f"arenas.{arena}.confirm_required",
                        [f"{capability} requires confirmation in arena {arena!r}"],
                    )
                )
            elif capability not in granted:
                candidates.append(
                    Verdict(
                        REPORT_ONLY,
                        f"arenas.{arena}.capabilities",
                        [f"{capability} not granted in arena {arena!r}"],
                    )
                )
            elif "*" in confirm_required:
                candidates.append(
                    Verdict(
                        CONFIRM,
                        f"arenas.{arena}.confirm_required",
                        [f"all capabilities require confirmation in arena {arena!r}"],
                    )
                )
            else:
                candidates.append(Verdict(EXECUTE, f"arenas.{arena}.mode=execute"))
    else:
        default_mode = defaults.get("mode", "report_only")
        if default_mode == "disabled":
            return Verdict(DENY, "defaults.mode=disabled", ["unlisted arenas are disabled"])
        if default_mode == "execute":
            # Defaults grant no capability list; execute-by-default still
            # means report-only per capability. Default deny holds.
            candidates.append(
                Verdict(REPORT_ONLY, "defaults.mode", [f"arena {arena!r} not listed; no capabilities granted"])
            )
        else:
            candidates.append(Verdict(REPORT_ONLY, "defaults.mode=report_only", [f"arena {arena!r} not listed"]))

    return max(candidates, key=lambda v: _RESTRICTIVENESS[v.decision])


def resolve_capabilities(delegations: dict[str, Any], arena: str, capabilities: list[str]) -> Verdict:
    """Most restrictive verdict across a capability set; reasons accumulate
    from every capability that tightened the outcome."""
    if not capabilities:
        return Verdict(REPORT_ONLY, "no capabilities required")
    verdicts = [resolve_delegation(delegations, arena, cap) for cap in capabilities]
    worst = max(verdicts, key=lambda v: _RESTRICTIVENESS[v.decision])
    reasons: list[str] = []
    for v in verdicts:
        if v.decision != worst.decision:
            continue
        for reason in v.reasons:
            if reason not in reasons:
                reasons.append(reason)
    return Verdict(worst.decision, worst.rule, reasons)


# ---------------------------------------------------------------------------
# Lifecycle: init, snapshot, edit, history, out-of-band detection

def init_intent(vault: Path, *, force: bool = False) -> Path:
    path = intent_path(vault)
    if path.exists() and not force:
        raise IntentError(f"intent.md already exists at {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_intent_document(), encoding="utf-8")
    _record_known_hash(vault)
    return path


def snapshot_intent(vault: Path, *, timestamp: str | None = None) -> Path:
    """Copy the current intent.md into intent-history/ before any change."""
    path = intent_path(vault)
    if not path.exists():
        raise IntentError(f"intent.md not found at {path}")
    history = intent_history_dir(vault)
    history.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = history / f"intent-{stamp}.md"
    counter = 1
    while target.exists():
        target = history / f"intent-{stamp}-{counter}.md"
        counter += 1
    shutil.copy2(path, target)
    return target


def list_intent_history(vault: Path) -> list[Path]:
    history = intent_history_dir(vault)
    if not history.exists():
        return []
    return sorted(p for p in history.glob("intent-*.md"))


def _record_known_hash(vault: Path) -> None:
    path = intent_path(vault)
    hash_path = _known_hash_path(vault)
    hash_path.parent.mkdir(parents=True, exist_ok=True)
    hash_path.write_text(_content_hash(path.read_text(encoding="utf-8")) + "\n", encoding="utf-8")


def detect_out_of_band_edit(vault: Path) -> bool:
    """Compare intent.md against the last hash recorded by the CLI. On
    mismatch: snapshot the edited file, bump version, re-record the hash.
    Called at Adjutant startup so manual edits still enter history.
    Returns True when an out-of-band edit was detected and absorbed."""
    path = intent_path(vault)
    if not path.exists():
        return False
    hash_path = _known_hash_path(vault)
    current = _content_hash(path.read_text(encoding="utf-8"))
    if not hash_path.exists():
        _record_known_hash(vault)
        return False
    known = hash_path.read_text(encoding="utf-8").strip()
    if current == known:
        return False
    snapshot_intent(vault)
    _bump_version(vault)
    _record_known_hash(vault)
    return True


def _bump_version(vault: Path) -> None:
    from ..frontmatter import dump_markdown

    path = intent_path(vault)
    doc = load_markdown(path)
    frontmatter = dict(doc.frontmatter)
    frontmatter["version"] = int(frontmatter.get("version", 0)) + 1
    frontmatter["updated"] = date.today().isoformat()
    path.write_text(dump_markdown(frontmatter, doc.body), encoding="utf-8")


def edit_intent(vault: Path, *, editor: str | None = None) -> dict[str, Any]:
    """Open intent.md in $EDITOR. On change: snapshot the prior version,
    bump version + updated, validate, re-record the hash. An edit that
    leaves the file invalid is reported but never discarded — the owner's
    words outrank the validator; the Adjutant simply refuses to start
    until it is fixed."""
    path = intent_path(vault)
    if not path.exists():
        raise IntentError(f"intent.md not found at {path} — run `lisan intent init`")
    before = path.read_text(encoding="utf-8")
    chosen = editor or os.environ.get("EDITOR") or "vi"
    subprocess.run([chosen, str(path)], check=True)
    after = path.read_text(encoding="utf-8")
    if after == before:
        return {"changed": False, "version": parse_intent(before).frontmatter.get("version"), "issues": []}
    history = intent_history_dir(vault)
    history.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    snapshot = history / f"intent-{stamp}.md"
    counter = 1
    while snapshot.exists():
        snapshot = history / f"intent-{stamp}-{counter}.md"
        counter += 1
    snapshot.write_text(before, encoding="utf-8")
    _bump_version(vault)
    _record_known_hash(vault)
    issues = validate_intent_file(vault)
    version = parse_intent(path.read_text(encoding="utf-8")).frontmatter.get("version")
    return {"changed": True, "version": version, "snapshot": snapshot, "issues": issues}


# ---------------------------------------------------------------------------
# Rendering

def format_intent(vault: Path) -> str:
    path = intent_path(vault)
    if not path.exists():
        return f"No intent.md at {path}. Run `lisan intent init`."
    text = path.read_text(encoding="utf-8")
    intent = parse_intent(text, path=path)
    issues = validate_intent_text(text)
    lines = [
        f"intent.md  version {intent.frontmatter.get('version')}  "
        f"updated {intent.frontmatter.get('updated')}  "
        f"status {intent.frontmatter.get('status')}",
        f"hash {intent.content_hash[:12]}",
    ]
    if issues:
        lines.append(f"INVALID ({len(issues)} issue(s); the Adjutant will refuse to start):")
        lines.extend(f"  - {issue}" for issue in issues)
    lines.append("")
    lines.append(text.rstrip())
    return "\n".join(lines)


def format_intent_history(vault: Path) -> str:
    snapshots = list_intent_history(vault)
    if not snapshots:
        return "No intent history."
    lines = []
    for snap in snapshots:
        try:
            version = load_markdown(snap).frontmatter.get("version", "?")
        except FrontmatterError:
            version = "?"
        lines.append(f"{snap.name}  (version {version})")
    return "\n".join(lines)
