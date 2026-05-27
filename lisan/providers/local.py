from __future__ import annotations

from typing import Any

from .base import LLMResponse, ProviderClient, _post_json


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
        base_url = self.config["providers"]["local"]["base_url"]
        chosen_model = model or self.config["providers"]["local"]["default_model"]
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
