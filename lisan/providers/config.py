from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ProviderSelection:
    provider: str
    model: str | None


def select_provider(config: dict[str, Any], agent: str, significance: str, override_provider: str | None = None, override_model: str | None = None) -> ProviderSelection:
    routing = config.get("routing", {})
    providers = config.get("providers", {})
    provider = override_provider or routing.get(agent, {}).get(significance, "local")
    model = override_model or providers.get(provider, {}).get("default_model")
    return ProviderSelection(provider=provider, model=model)
