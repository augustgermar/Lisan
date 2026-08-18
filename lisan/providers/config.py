"""Which provider answers for which agent.

The resolution order is deliberate. An unrouted agent used to fall through
to a hardcoded ``"local"``, which is how ``self_repair_author`` — added with
no routing entry — spent 2026-08-16 talking to whatever happened to own port
8080 instead of the codex provider every other agent uses. A fallback is
fine; an invisible one that names a specific provider is not. The default now
lives in the routing table where an owner can see and change it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_ROUTING_KEY = "default"


class ProviderRoutingError(RuntimeError):
    """No routing entry and no configured default for an agent."""


@dataclass(slots=True)
class ProviderSelection:
    provider: str
    model: str | None


@dataclass(slots=True)
class RetrySettings:
    transient_retries: int
    base_delay_seconds: float
    max_delay_seconds: float


def resolve_route(routing: dict[str, Any], agent: str, significance: str) -> str:
    """Return the provider for one agent, or raise if nothing routes it.

    A dotted agent name (``elicitor.prose_recovery``) inherits its parent's
    route: a sub-call is the same agent doing the same class of work, and
    demanding a separate entry for each one is how entries get forgotten.
    """
    candidates = [agent]
    while "." in candidates[-1]:
        candidates.append(candidates[-1].rsplit(".", 1)[0])
    candidates.append(DEFAULT_ROUTING_KEY)
    for candidate in candidates:
        entry = routing.get(candidate)
        if not isinstance(entry, dict):
            continue
        provider = entry.get(significance)
        if provider:
            return str(provider)
    raise ProviderRoutingError(
        f"no provider routes agent {agent!r} at significance {significance!r}; "
        f"add it to config routing, or set a routing.{DEFAULT_ROUTING_KEY} entry"
    )


def select_provider(config: dict[str, Any], agent: str, significance: str, override_provider: str | None = None, override_model: str | None = None) -> ProviderSelection:
    routing = config.get("routing", {})
    providers = config.get("providers", {})
    provider = override_provider or resolve_route(routing, agent, significance)
    model = override_model or providers.get(provider, {}).get("default_model")
    return ProviderSelection(provider=provider, model=model)


def transient_retry_settings(config: dict[str, Any]) -> RetrySettings:
    block = config.get("provider_resilience", {}) or {}
    retries = int(block.get("transient_retries", 2) or 2)
    base_delay = float(block.get("base_delay_seconds", 0.5) or 0.5)
    max_delay = float(block.get("max_delay_seconds", 2.0) or 2.0)
    return RetrySettings(
        transient_retries=max(0, retries),
        base_delay_seconds=max(0.0, base_delay),
        max_delay_seconds=max(base_delay, max_delay),
    )
