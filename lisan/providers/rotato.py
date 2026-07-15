"""Rotato: OpenAI-compatible calls through the local key-rotation proxy.

Rotato (localhost service) fronts hosted models with a rotating key pool and
an OpenAI chat-completions dialect per configured provider route
(e.g. ``/gemflash/chat/completions``). This client is the LocalClient
protocol pointed at its own config block, so routing can send some agents to
rotato-backed hosted models while others stay on codex or the local model.

Config::

    "providers": {
      "rotato": {
        "base_url": "http://localhost:8990/gemflash/chat/completions",
        "default_model": "gemini-2.5-pro",
        "timeout_seconds": 120
      }
    }
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from .base import LLMResponse, ProviderClient, ProviderError


class RotatoClient(ProviderClient):
    name = "rotato"

    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        agent: str = "writer",
        significance: str = "medium",
        model: str | None = None,
        working_directory=None,
    ) -> LLMResponse:
        cfg = self.config.get("providers", {}).get("rotato", {})
        base_url = str(cfg.get("base_url") or "http://localhost:8990/gemflash/chat/completions")
        chosen_model = model or str(cfg.get("default_model") or "gemini-2.5-pro")
        timeout = float(cfg.get("timeout_seconds") or 120)

        full_prompt = prompt
        if schema:
            full_prompt = (
                prompt
                + "\n\nRespond with valid JSON only — no prose, no code fences. "
                + f"Your response must match this schema:\n{json.dumps(schema, indent=2)}"
            )
        payload = {
            "model": chosen_model,
            "messages": [{"role": "user", "content": full_prompt}],
            "temperature": temperature,
            # Without an explicit ceiling the proxy's default silently cut
            # long writer outputs mid-JSON (13 dropped memory extractions in
            # the week of 2026-07-06). High on purpose; truncation below is
            # an error, never a result.
            "max_tokens": int(cfg.get("max_output_tokens") or 16384),
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(base_url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise ProviderError(f"rotato request failed: {exc}") from exc

        choices = body.get("choices") or []
        if not choices:
            raise ProviderError(f"rotato returned no choices: {str(body)[:200]}")
        finish_reason = str(choices[0].get("finish_reason") or "").lower()
        if finish_reason in {"length", "max_tokens"}:
            # A truncated response is not a response. Returning it hands the
            # parser half a JSON object and the caller a silent fallback;
            # raising makes it a provider failure the retry machinery owns.
            raise ProviderError(
                f"rotato response truncated (finish_reason={finish_reason}); "
                "raise providers.rotato.max_output_tokens if this recurs"
            )
        message = choices[0].get("message") or {}
        text = str(message.get("content") or "").strip()
        if not text:
            raise ProviderError("rotato returned an empty message")
        if schema:
            from ..tools.structured import extract_json

            parsed = extract_json(text)
            if isinstance(parsed, dict):
                text = json.dumps(parsed, indent=2, ensure_ascii=True)
        return LLMResponse(text=text, provider=self.name, model=chosen_model, raw=body)
