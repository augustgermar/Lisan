from __future__ import annotations

import os
from typing import Any

from .base import LLMResponse, ProviderClient, ProviderError, _post_json


class LocalClient(ProviderClient):
    name = "local"

    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        agent: str = "writer",
        significance: str = "medium",
        model: str | None = None,
    ) -> LLMResponse:
        base_url = os.getenv("LISAN_LOCAL_MODEL_URL", self.config["providers"]["local"]["base_url"])
        chosen_model = model or os.getenv("LISAN_LOCAL_MODEL", self.config["providers"]["local"]["default_model"])
        payload = {
            "model": chosen_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if schema:
            payload["messages"].insert(0, {"role": "system", "content": f"Return output compatible with schema: {schema.get('$id') or schema.get('title') or 'provided schema'}"})
            payload["response_format"] = {"type": "json_object"}
        data = _post_json(base_url, payload, {"Content-Type": "application/json"})
        text = data["choices"][0]["message"]["content"]
        return LLMResponse(text=text, provider=self.name, model=chosen_model, raw=data)
