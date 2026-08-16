"""Graduated autonomy policy (Phase 2 WO-7).

The autonomy surface is explicit, enforced in code at one dispatch seam —
never in prompts. Three tiers, configured as ``drive.action_tier``:

- **Tier 0 — queue-for-next-session (default, and the only tier this
  phase enables).** Drive output reaches the user solely as the
  session-open callback. Nothing leaves the vault unprompted.
- **Tier 1 — scheduled owner-gated delivery.** Would allow the drive to
  schedule a message through the existing owner-only channel (the
  scheduler's allowlist-locked Telegram delivery). Present in config,
  requires the owner to raise the tier.
- **Tier 2 — autonomous checks.** Allows drive-initiated read-only jobs
  (verify a fix, re-run a health check) without a session.
- **Tier 3 — enrichment.** Allows the agent to acquire information through
  the Ship 2 enrichment loop. The owner must raise the tier deliberately;
  the capability remains bounded by enrichment budgets and source rules.

At every tier, ordinary drive actions write only inside the vault. The sole
Phase A exception is a disposable self-repair worktree outside the live
checkout; it is isolated, bounded, and cannot apply code changes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .log import get_logger

DEFAULT_TIER = 0

# Action kind → minimum tier that permits it.
ACTION_TIERS: dict[str, int] = {
    "session_callback": 0,
    "scheduled_delivery": 1,
    "autonomous_check": 2,
    # WO-ENRICH: one capability for every entity kind. The owner raises
    # tier 3 deliberately; there is no per-subject permission gate here.
    "enrich": 3,
    # Phase A may draft and verify only at the enrichment tier. Applying a
    # self-repair proposal remains unreachable while the clamp is 3.
    "self_repair_propose": 3,
    "self_repair_apply": 4,
}


def policy_tier(config: dict[str, Any] | None) -> int:
    raw = ((config or {}).get("drive") or {}).get("action_tier", DEFAULT_TIER)
    try:
        tier = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIER
    return max(0, min(3, tier))


def action_allowed(kind: str, config: dict[str, Any] | None) -> bool:
    required = ACTION_TIERS.get(kind)
    if required is None:
        return False  # unknown action kinds are denied, not defaulted
    return policy_tier(config) >= required


def dispatch_drive_action(
    vault: Path,
    kind: str,
    action: Callable[[], Any],
    *,
    config: dict[str, Any] | None = None,
) -> Any | None:
    """The one seam every drive-initiated action passes through. A blocked
    action is logged and returns None; it is never queued for later —
    an action the policy forbids simply does not exist."""
    if not action_allowed(kind, config):
        get_logger(vault).info(f"drive.action.blocked kind={kind} tier={policy_tier(config)}")
        return None
    return action()
