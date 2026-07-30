"""The complete verdict matrix — what the calibration soak was actually for.

WO-ADJUTANT's remaining gate was an owner-run soak: accumulate dry verdicts,
audit `adjutant_log` for false taskings, then earn `enabled: true`. The soak ran
2026-07-23 → 07-29 and produced **three** task verdicts, all `report_only`, all
from one record, because 0 records carried `task_kind` and 245 of 248 cycles
logged `tasks=0`. Extending it produces more of the same: a calendar, not
evidence.

What the soak was *trying* to buy is coverage — has every (scope mode ×
capability grant × task kind) combination been seen, and did each one decide the
way the delegation contract says it should? That is a fixture, not a wait. This
module enumerates the matrix exhaustively and deterministically, in
milliseconds, and it runs on every push rather than once.

It is strictly stronger than the soak in three ways. It covers combinations a
real vault may not produce for months; it re-runs on every change to the
resolver, which a one-time audit does not; and it fails loudly instead of
requiring someone to read a log and notice an absence.

What it deliberately does NOT replace: the owner's judgement about whether the
*tasks being created* are ones they wanted. That is a question about the writer
and about intent.md's content, not about the gate — and no amount of soaking
answers it either, which is part of why the soak was the wrong instrument.
"""
from __future__ import annotations

import json

import pytest

from lisan.tools.adjutant_gate import TASK_KIND_CAPABILITIES, gate
from lisan.tools.intent import (
    CONFIRM,
    DENY,
    EXECUTE,
    REPORT_ONLY,
    default_intent_document,
    parse_intent,
)

ALL_CAPABILITIES = sorted(
    {cap for caps in TASK_KIND_CAPABILITIES.values() for cap in caps}
)


def _intent(delegations: dict):
    doc = default_intent_document(today="2026-07-30")
    start = doc.index("```json")
    end = doc.index("```", start + 7) + 3
    return parse_intent(doc[:start] + "```json\n" + json.dumps(delegations, indent=2) + "\n```" + doc[end:])


def _task(scope: str, kind: str, blocked: list[str] | None = None) -> dict:
    return {"scope": scope, "task_kinds": [kind], "blocked_contexts": blocked or []}


# ---------------------------------------------------------------------------
# 1. Every task kind, under every mode, with everything granted

@pytest.mark.parametrize("kind", sorted(TASK_KIND_CAPABILITIES))
def test_fully_granted_execute_scope_executes_every_kind(kind):
    """The only configuration in which unattended action is reachable at all."""
    intent = _intent({
        "defaults": {"mode": "report_only"},
        "scopes": {"greenhouse": {"mode": "execute", "capabilities": ALL_CAPABILITIES}},
    })
    assert gate(_task("greenhouse", kind), intent).decision == EXECUTE


@pytest.mark.parametrize("kind", sorted(TASK_KIND_CAPABILITIES))
def test_report_only_scope_never_executes_any_kind(kind):
    intent = _intent({
        "defaults": {"mode": "report_only"},
        "scopes": {"greenhouse": {"mode": "report_only", "capabilities": ALL_CAPABILITIES}},
    })
    assert gate(_task("greenhouse", kind), intent).decision == REPORT_ONLY


@pytest.mark.parametrize("kind", sorted(TASK_KIND_CAPABILITIES))
def test_disabled_scope_denies_every_kind(kind):
    intent = _intent({
        "defaults": {"mode": "report_only"},
        "scopes": {"greenhouse": {"mode": "disabled", "capabilities": ALL_CAPABILITIES}},
    })
    assert gate(_task("greenhouse", kind), intent).decision == DENY


@pytest.mark.parametrize("kind", sorted(TASK_KIND_CAPABILITIES))
def test_unscoped_record_never_executes_any_kind(kind):
    """The state every record in the production vault is currently in."""
    intent = _intent({
        "defaults": {"mode": "execute", "capabilities": ALL_CAPABILITIES},
        "scopes": {"greenhouse": {"mode": "execute", "capabilities": ALL_CAPABILITIES}},
    })
    verdict = gate(_task("", kind), intent)
    assert verdict.decision == REPORT_ONLY
    assert verdict.rule == "no_scope"


@pytest.mark.parametrize("kind", sorted(TASK_KIND_CAPABILITIES))
def test_unlisted_scope_never_executes_any_kind(kind):
    intent = _intent({
        "defaults": {"mode": "execute", "capabilities": ALL_CAPABILITIES},
        "scopes": {"greenhouse": {"mode": "execute", "capabilities": ALL_CAPABILITIES}},
    })
    assert gate(_task("observatory", kind), intent).decision == REPORT_ONLY


# ---------------------------------------------------------------------------
# 2. Partial grants — a kind needs ALL of its capabilities

@pytest.mark.parametrize("kind,caps", sorted(TASK_KIND_CAPABILITIES.items()))
def test_missing_any_single_capability_blocks_the_kind(kind, caps):
    """Most restrictive wins: withholding one required capability is enough."""
    for withheld in caps:
        granted = [c for c in ALL_CAPABILITIES if c != withheld]
        intent = _intent({
            "defaults": {"mode": "report_only"},
            "scopes": {"greenhouse": {"mode": "execute", "capabilities": granted}},
        })
        verdict = gate(_task("greenhouse", kind), intent)
        assert verdict.decision != EXECUTE, (
            f"{kind} executed without {withheld}; it requires {caps}"
        )


# ---------------------------------------------------------------------------
# 3. Confirmation and never-rules

def test_confirm_required_grants_with_a_gate_rather_than_denying():
    """A capability named only in confirm_required is a grant-with-confirmation,
    not a refusal — the contract's least obvious clause.

    Tested at the resolver rather than through gate(), because no task kind maps
    to git_push: the contract's own example of this clause is not reachable from
    the task-kind table at all. Worth noting on its own — a capability that only
    the CLI and chat paths can request is governed by a rule the Adjutant can
    never exercise.
    """
    from lisan.tools.intent import resolve_delegation

    delegations = {
        "defaults": {"mode": "report_only"},
        "scopes": {"greenhouse": {
            "mode": "execute",
            "capabilities": ["read_files", "write_files"],
            "confirm_required": ["git_push"],
        }},
    }
    # Named only in confirm_required: granted, behind a human gate.
    assert resolve_delegation(delegations, "greenhouse", "git_push").decision == CONFIRM
    # Granted outright: no gate.
    assert resolve_delegation(delegations, "greenhouse", "read_files").decision == EXECUTE
    # Neither granted nor confirm-listed: default deny holds.
    assert resolve_delegation(delegations, "greenhouse", "spend_money").decision == REPORT_ONLY
    # And a wildcard tightens the granted list without widening it.
    wild = {**delegations, "scopes": {"greenhouse": {
        "mode": "execute", "capabilities": ["read_files"], "confirm_required": ["*"]}}}
    assert resolve_delegation(wild, "greenhouse", "read_files").decision == CONFIRM
    assert resolve_delegation(wild, "greenhouse", "spend_money").decision == REPORT_ONLY


def test_global_confirm_always_downgrades_execute_to_confirm():
    intent = _intent({
        "defaults": {"mode": "report_only"},
        "scopes": {"greenhouse": {"mode": "execute", "capabilities": ALL_CAPABILITIES}},
        "global": {"send_outbound_message": "confirm_always"},
    })
    assert gate(_task("greenhouse", "notify"), intent).decision == CONFIRM
    # ...and leaves unrelated kinds alone.
    assert gate(_task("greenhouse", "draft"), intent).decision == EXECUTE


def test_never_rule_beats_a_confirm_required_grant():
    """The settled ruling: a never-rule outranks confirmation, and outranks an
    approval already given."""
    intent = _intent({
        "defaults": {"mode": "report_only"},
        "scopes": {"greenhouse": {
            "mode": "execute",
            "capabilities": ALL_CAPABILITIES,
            "confirm_required": ["send_outbound_message"],
            "outbound_comms": "never",
        }},
        "global": {"send_outbound_message": "confirm_always"},
    })
    assert gate(_task("greenhouse", "notify"), intent).decision == DENY


def test_global_never_beats_a_scope_grant():
    intent = _intent({
        "defaults": {"mode": "report_only"},
        "scopes": {"greenhouse": {"mode": "execute", "capabilities": ALL_CAPABILITIES}},
        "global": {"send_outbound_message": "never"},
    })
    assert gate(_task("greenhouse", "notify"), intent).decision == DENY


# ---------------------------------------------------------------------------
# 4. Structural refusals — the gate's own hygiene

def test_unknown_task_kind_is_denied_not_ignored():
    intent = _intent({"defaults": {"mode": "report_only"},
                      "scopes": {"greenhouse": {"mode": "execute", "capabilities": ALL_CAPABILITIES}}})
    verdict = gate(_task("greenhouse", "transmute"), intent)
    assert verdict.decision == DENY and verdict.rule == "unknown_task_kind"


def test_no_task_kind_is_denied():
    intent = _intent({"defaults": {"mode": "report_only"},
                      "scopes": {"greenhouse": {"mode": "execute", "capabilities": ALL_CAPABILITIES}}})
    verdict = gate({"scope": "greenhouse", "task_kinds": [], "blocked_contexts": []}, intent)
    assert verdict.decision == DENY and verdict.rule == "no_task_kind"


def test_a_task_blocked_from_its_own_scope_is_denied_as_misfiled():
    intent = _intent({"defaults": {"mode": "report_only"},
                      "scopes": {"greenhouse": {"mode": "execute", "capabilities": ALL_CAPABILITIES}}})
    verdict = gate(_task("greenhouse", "draft", blocked=["greenhouse"]), intent)
    assert verdict.decision == DENY and verdict.rule == "misfiled_task"


# ---------------------------------------------------------------------------
# 5. The property that makes the whole thing safe by default

def test_execute_is_unreachable_without_a_named_scope_however_defaults_are_written():
    """Exhaustive over the mode enum: no `defaults` configuration reaches EXECUTE
    for an unscoped or unlisted record. This is the invariant that made a month
    of vocabulary mismatch harmless rather than dangerous, and it is worth
    holding a test on directly rather than inferring from the resolver."""
    for mode in ("report_only", "execute", "disabled"):
        for caps in ([], ALL_CAPABILITIES):
            delegations = {
                "defaults": {"mode": mode, **({"capabilities": caps} if caps else {})},
                "scopes": {"greenhouse": {"mode": "execute", "capabilities": ALL_CAPABILITIES}},
            }
            intent = _intent(delegations)
            for scope in ("", "observatory"):
                for kind in TASK_KIND_CAPABILITIES:
                    decision = gate(_task(scope, kind), intent).decision
                    assert decision != EXECUTE, (
                        f"defaults(mode={mode}, caps={bool(caps)}) reached EXECUTE "
                        f"for scope={scope!r} kind={kind}"
                    )
