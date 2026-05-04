from __future__ import annotations

import os
from typing import Any

from .base import LLMResponse, ProviderClient, ProviderError, _post_json


class OpenAIClient(ProviderClient):
    name = "openai"

    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        agent: str = "writer",
        significance: str = "medium",
        model: str | None = None,
    ) -> LLMResponse:
        api_key = os.getenv(self.config["providers"]["openai"]["api_key_env"])
        if not api_key:
            raise ProviderError("OPENAI_API_KEY is not set")
        chosen_model = model or self.config["providers"]["openai"]["default_model"]
        payload = {
            "model": chosen_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if schema:
            payload["messages"].insert(0, {"role": "system", "content": f"Return output compatible with schema: {schema.get('$id') or schema.get('title') or 'provided schema'}"})
            payload["response_format"] = {"type": "json_object"}
        data = _post_json(
            self.config["providers"]["openai"]["base_url"],
            payload,
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        text = data["choices"][0]["message"]["content"]
        return LLMResponse(text=text, provider=self.name, model=chosen_model, raw=data)
