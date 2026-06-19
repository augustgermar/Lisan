from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ProviderSelection:
    provider: str
    model: str | None


@dataclass(slots=True)
class RetrySettings:
    transient_retries: int
    base_delay_seconds: float
    max_delay_seconds: float


def select_provider(config: dict[str, Any], agent: str, significance: str, override_provider: str | None = None, override_model: str | None = None) -> ProviderSelection:
    routing = config.get("routing", {})
    providers = config.get("providers", {})
    provider = override_provider or routing.get(agent, {}).get(significance, "local")
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
