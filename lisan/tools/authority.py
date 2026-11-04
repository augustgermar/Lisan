"""One place that decides whether an action may happen.

Before this module, five mechanisms each answered part of that question in
their own file: intent.md delegations (adjutant_gate / execution_tools),
the drive action tiers (action_policy), ``requires_approval`` on skills
(skill_loader), browser receipts (skills/browser), and Telegram ``/trust``.
Each was reasonable on its own; together nobody could say, in one place,
why a given action was or was not allowed — and two outbound paths
(``send_email`` and the Drive skill) had slipped through every one of them.

``rule(action, ...)`` answers with one :class:`Ruling` — ALLOW, ASK, or
DENY — in a fixed order, most restrictive wins:

1. **intent.md never-rules.** For actions that map to intent capabilities,
   the owner's standing authority document decides first. An absent,
   invalid, or unadopted intent denies (fail closed).
2. **Outbound kill switch.** ``LISAN_NO_OUTBOUND`` and the test-process
   guard deny anything that leaves the machine.
3. **Autonomy tier.** Drive-initiated actions need the owner's
   ``drive.action_tier``.
4. **Approval.** Actions that need the owner's yes for the exact action
   rule ASK — or DENY when no approval channel exists in this context.

The engine decides; it does not ask. *How* a yes is obtained — the CLI
prompt, Telegram buttons, a browser receipt, a ``/trust`` window — stays
with the channel. Every ruling is appended to ``logs/authority.jsonl`` with
the rule that produced it, so "why was this allowed?" has an answer after
the fact.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALLOW = "allow"
ASK = "ask"
DENY = "deny"


@dataclass(frozen=True)
class ActionSpec:
    """What the engine needs to know about one kind of action."""

    intent_capabilities: tuple[str, ...] = ()  # checked against intent.md (chat scope)
    outbound: bool = False                     # leaves the machine
    approval: bool = False                     # owner's yes for the exact action
    tier_action: str | None = None             # action_policy kind, for drive actions


_RUN_SCRIPT = ("run_local_scripts", "read_files", "write_files")

# The registry. An action that is not listed is denied — unknown actions are
# never defaulted to allowed.
ACTIONS: dict[str, ActionSpec] = {
    # Owner decision 2026-07-26: an in-chat command is the consent for codex
    # runs; intent never-rules remain the hard rail.
    "run_codex": ActionSpec(intent_capabilities=_RUN_SCRIPT),
    "delegate": ActionSpec(intent_capabilities=_RUN_SCRIPT),
    "plan.codex_step": ActionSpec(intent_capabilities=_RUN_SCRIPT),
    "send_email": ActionSpec(intent_capabilities=("send_outbound_message",), outbound=True, approval=True),
    "skill.outbound": ActionSpec(outbound=True, approval=True),
    "drive.session_callback": ActionSpec(tier_action="session_callback"),
    "drive.scheduled_delivery": ActionSpec(tier_action="scheduled_delivery", outbound=True),
    "drive.autonomous_check": ActionSpec(tier_action="autonomous_check"),
    "drive.enrich": ActionSpec(tier_action="enrich"),
    # Owner decision 2026-11-04: self-repair applies need no approval once
    # the owner's action_tier clamp is raised to 4. The tier check above is
    # the owner's permission; PROTECTED_PATHS/PROTECTED_BASENAMES in
    # self_repair.py (authority.py among them) remain the hard boundary no
    # tier can widen.
    "self_repair.propose": ActionSpec(tier_action="self_repair_propose"),
    "self_repair.apply": ActionSpec(tier_action="self_repair_apply"),
    "self_repair.rollback": ActionSpec(tier_action="self_repair_rollback"),
}


@dataclass(frozen=True)
class Ruling:
    decision: str
    rule: str
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW

    def explain(self) -> str:
        why = "; ".join(self.reasons) if self.reasons else self.rule
        return f"{self.decision.upper()} by {self.rule}: {why}"


def _intent_ruling(vault: Path, capabilities: tuple[str, ...]) -> Ruling | None:
    """None when intent permits (anything short of DENY); a DENY otherwise.
    Fails closed on a missing, invalid, or unadopted intent.md."""
    from .execution_tools import _chat_intent_verdict

    ruling = _chat_intent_verdict(vault, list(capabilities))
    if ruling is None:  # defensive: the verdict function fails closed itself
        return Ruling(DENY, "intent.unavailable", ("intent.md could not be checked",))
    verdict, version = ruling
    if verdict.decision == "deny":
        return Ruling(DENY, f"intent.v{version}:{verdict.rule}", tuple(verdict.reasons or ["denied"]))
    return None


def rule(
    action: str,
    *,
    vault: Path,
    config: dict[str, Any] | None = None,
    approval_channel: bool = False,
    detail: dict[str, Any] | None = None,
    audit: bool = True,
) -> Ruling:
    """Decide one action. See the module docstring for the order."""
    spec = ACTIONS.get(action)
    if spec is None:
        ruling = Ruling(DENY, "authority.unknown_action", (f"{action!r} is not a registered action",))
    else:
        ruling = _decide(action, spec, vault=vault, config=config, approval_channel=approval_channel)
    if audit:
        _audit(vault, action, ruling, detail)
    return ruling


def _decide(action: str, spec: ActionSpec, *, vault: Path, config: dict[str, Any] | None,
            approval_channel: bool) -> Ruling:
    if spec.intent_capabilities:
        denied = _intent_ruling(vault, spec.intent_capabilities)
        if denied is not None:
            return denied
    if spec.outbound:
        from .outbound import OutboundDisabled, assert_outbound_allowed

        try:
            assert_outbound_allowed(action)
        except OutboundDisabled as exc:
            return Ruling(DENY, "outbound.kill_switch", (str(exc),))
    if spec.tier_action:
        from .action_policy import ACTION_TIERS, action_allowed, policy_tier

        if not action_allowed(spec.tier_action, config):
            return Ruling(DENY, f"drive.action_tier<{ACTION_TIERS.get(spec.tier_action, '?')}",
                          (f"owner tier is {policy_tier(config)}",))
    if spec.approval:
        if not approval_channel:
            return Ruling(DENY, "approval.no_channel",
                          ("this action needs the owner's yes and no approval channel is available",))
        return Ruling(ASK, "approval.required", ("the owner approves this exact action",))
    return Ruling(ALLOW, "authority.permitted")


def _audit(vault: Path, action: str, ruling: Ruling, detail: dict[str, Any] | None) -> None:
    """Append-only, best-effort: auditing must never break the action path."""
    try:
        log_dir = vault / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            **asdict(ruling),
        }
        if detail:
            entry["detail"] = {k: str(v)[:200] for k, v in detail.items()}
        with (log_dir / "authority.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except Exception:
        pass


def registered_actions() -> dict[str, dict[str, Any]]:
    """For the capability manifest and `lisan doctor`: the whole policy
    surface as data."""
    return {name: asdict(spec) for name, spec in sorted(ACTIONS.items())}
