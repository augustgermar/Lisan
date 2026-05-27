from __future__ import annotations

import json
import re
from typing import Any

from .base import LLMResponse, ProviderClient, _post_json


def _extract_json_from_reasoning(text: str) -> str | None:
    """
    Reasoning models (e.g. MLX supergemma4) sometimes output their answer as
    markdown bullets inside the "reasoning" field rather than as JSON in
    "content".  The typical format is:

        *   `key`: value
        *   `key`: "string"
        *   `key`: [...]

    This function locates the first run of such bullet lines, converts them to
    a JSON object string, and returns it.  Returns None if the text does not
    look like this format or if the conversion produces invalid JSON.
    """
    # First try: maybe there's a JSON block somewhere (fenced or raw)
    json_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if json_block:
        return json_block.group(1)
    raw_obj = re.search(r"(\{[^{]*\})", text, re.DOTALL)
    if raw_obj:
        try:
            json.loads(raw_obj.group(1))
            return raw_obj.group(1)
        except json.JSONDecodeError:
            pass

    # Second try: parse the leading bullet-point block
    # Match lines like: *   `key`: value   (indented or not, backtick key optional)
    bullet_re = re.compile(
        r"^\s*\*+\s+`?(\w+)`?\s*:\s*(.+)$", re.MULTILINE
    )
    obj: dict[str, Any] = {}
    for m in bullet_re.finditer(text):
        key = m.group(1).lower()  # normalise to lowercase
        raw_val = m.group(2).strip()
        # Strip trailing parenthetical comments like "(correct)" or "(It's a rich narrative)."
        raw_val = re.sub(r"\s*\([^)]*\)[.]*\s*$", "", raw_val).strip()
        # Strip wrapping backticks from values like `full` → full (then re-quote for JSON)
        if raw_val.startswith("`") and raw_val.endswith("`"):
            raw_val = f'"{raw_val[1:-1]}"'
        # Skip duplicate/repeated keys (keep first occurrence)
        if key in obj:
            continue
        try:
            obj[key] = json.loads(raw_val)
        except json.JSONDecodeError:
            # Store as string with quotes stripped
            obj[key] = raw_val.strip("\"'")
    if obj:
        try:
            serialised = json.dumps(obj)
            json.loads(serialised)  # validate
            return serialised
        except (json.JSONDecodeError, TypeError):
            pass
    return None


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
            "max_tokens": 4096,
        }
        if schema:
            payload["messages"].insert(0, {"role": "system", "content": f"Return output compatible with schema: {schema.get('$id') or schema.get('title') or 'provided schema'}"})
            payload["response_format"] = {"type": "json_object"}
        data = _post_json(base_url, payload, {"Content-Type": "application/json"}, timeout=150)
        msg = data["choices"][0]["message"]
        # Some reasoning models (e.g. MLX supergemma4) emit the JSON answer
        # inside the "reasoning" field when the prompt is long, leaving
        # "content" absent or empty.  Fall back to "reasoning" so the pipeline
        # doesn't crash; then try to normalise markdown-bullet output to JSON.
        text = msg.get("content") or msg.get("reasoning", "")
        if schema and text:
            from ..tools.structured import extract_json
            if extract_json(text) is None:
                recovered = _extract_json_from_reasoning(text)
                if recovered is not None:
                    text = recovered
        return LLMResponse(text=text, provider=self.name, model=chosen_model, raw=data)
