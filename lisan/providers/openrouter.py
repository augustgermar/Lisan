from __future__ import annotations

import os
from typing import Any

from .base import LLMResponse, ProviderClient, ProviderError, _post_json


class OpenRouterClient(ProviderClient):
    name = "openrouter"

    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        agent: str = "writer",
        significance: str = "medium",
        model: str | None = None,
    ) -> LLMResponse:
        api_key = os.getenv(self.config["providers"]["openrouter"]["api_key_env"])
        if not api_key:
            raise ProviderError("OPENROUTER_API_KEY is not set")
        chosen_model = model or self.config["providers"]["openrouter"]["default_model"]

        messages: list[dict[str, str]] = []
        if schema:
            messages.append({
                "role": "system",
                "content": (
                    "You must respond with a valid JSON object only. "
                    "No prose, no markdown fences, no explanation. "
                    "Output only the raw JSON object that satisfies the requested schema."
                ),
            })
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": messages,
            "temperature": temperature,
        }
        # Best-effort: models that support json_object mode will use it;
        # others fall back to the system prompt instruction above.
        if schema:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/lisan-app/lisan",
            "X-Title": "Lisan",
        }
        data = _post_json(
            self.config["providers"]["openrouter"]["base_url"],
            payload,
            headers,
            timeout=60,
        )
        text = data["choices"][0]["message"]["content"]
        return LLMResponse(text=text, provider=self.name, model=chosen_model, raw=data)
