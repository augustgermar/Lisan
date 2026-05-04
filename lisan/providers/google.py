from __future__ import annotations

import os
from typing import Any

from .base import LLMResponse, ProviderClient, ProviderError, _post_json


class GoogleClient(ProviderClient):
    name = "google"

    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        agent: str = "writer",
        significance: str = "medium",
        model: str | None = None,
    ) -> LLMResponse:
        api_key = os.getenv(self.config["providers"]["google"]["api_key_env"])
        if not api_key:
            raise ProviderError("GOOGLE_API_KEY is not set")
        chosen_model = model or self.config["providers"]["google"]["default_model"]
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        if schema:
            payload["systemInstruction"] = {"parts": [{"text": f"Return output compatible with schema: {schema.get('$id') or schema.get('title') or 'provided schema'}"}]}
        url = f"{self.config['providers']['google']['base_url']}/models/{chosen_model}:generateContent?key={api_key}"
        data = _post_json(url, payload, {"Content-Type": "application/json"})
        candidates = data.get("candidates", [])
        text = ""
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(part.get("text", "") for part in parts)
        return LLMResponse(text=text, provider=self.name, model=chosen_model, raw=data)

