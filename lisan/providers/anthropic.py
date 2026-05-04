from __future__ import annotations

import os
from typing import Any

from .base import LLMResponse, ProviderClient, ProviderError, _post_json


class AnthropicClient(ProviderClient):
    name = "anthropic"

    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        agent: str = "writer",
        significance: str = "medium",
        model: str | None = None,
    ) -> LLMResponse:
        api_key = os.getenv(self.config["providers"]["anthropic"]["api_key_env"])
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        chosen_model = model or self.config["providers"]["anthropic"]["default_model"]
        messages = [{"role": "user", "content": prompt}]
        if schema:
            messages.insert(0, {"role": "assistant", "content": f"Return output compatible with schema: {schema.get('$id') or schema.get('title') or 'provided schema'}"})
        payload = {
            "model": chosen_model,
            "max_tokens": 4096,
            "temperature": temperature,
            "messages": messages,
        }
        data = _post_json(
            self.config["providers"]["anthropic"]["base_url"],
            payload,
            {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        )
        text = "".join(part.get("text", "") for part in data.get("content", []))
        return LLMResponse(text=text, provider=self.name, model=chosen_model, raw=data)

