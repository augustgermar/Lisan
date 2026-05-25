from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

from ..config import load_config
from ..paths import sqlite_path
from .config import select_provider
from ..tools.tracing import record_llm_call


@dataclass(slots=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    raw: dict[str, Any] | None = None


class ProviderError(RuntimeError):
    pass


class ProviderClient(ABC):
    name: str

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or load_config()

    @abstractmethod
    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        agent: str = "writer",
        significance: str = "medium",
        model: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError


class LisanLLM:
    def __init__(self, config: dict[str, Any] | None = None, db_path: Path | None = None):
        self.config = config or load_config()
        self.db_path = db_path or sqlite_path()

    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        agent: str = "writer",
        significance: str = "medium",
        provider: str | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        selected = select_provider(self.config, agent=agent, significance=significance, override_provider=provider, override_model=model)
        chosen_provider = selected.provider
        client = _client_for(chosen_provider, self.config)
        prompt_version = f"{agent}_{significance}"
        start = time.time()
        response_text = ""
        error_text: str | None = None
        try:
            response = client.complete(prompt, schema=schema, temperature=temperature, agent=agent, significance=significance, model=selected.model)
            response_text = response.text
            return response
        except Exception as exc:
            error_text = str(exc)
            raise
        finally:
            latency_ms = int((time.time() - start) * 1000)
            record_llm_call(
                call_name=agent,
                provider=chosen_provider,
                model=selected.model or _default_model(self.config, chosen_provider),
                prompt=prompt,
                output=response_text,
                elapsed_ms=latency_ms,
                success=error_text is None,
                error=error_text,
            )
            _log_call(
                db_path=self.db_path,
                agent=agent,
                provider=chosen_provider,
                model=selected.model or _default_model(self.config, chosen_provider),
                prompt_version=prompt_version,
                input_hash=_sha(prompt),
                output_hash=_sha(response_text) if response_text else None,
                schema_version=_schema_version(schema),
                latency_ms=latency_ms,
                success=error_text is None,
            )


def _schema_version(schema: dict[str, Any] | None) -> str:
    if not schema:
        return ""
    return schema.get("$id") or schema.get("title") or "schema"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _default_model(config: dict[str, Any], provider: str) -> str:
    return str(config.get("providers", {}).get(provider, {}).get("default_model", ""))


def _client_for(provider: str, config: dict[str, Any]) -> "ProviderClient":
    provider = provider.lower()
    if provider == "openai":
        from .openai import OpenAIClient

        return OpenAIClient(config)
    if provider == "codex":
        from .codex import CodexClient

        return CodexClient(config)
    if provider == "anthropic":
        from .anthropic import AnthropicClient

        return AnthropicClient(config)
    if provider == "google":
        from .google import GoogleClient

        return GoogleClient(config)
    if provider == "local":
        from .local import LocalClient

        return LocalClient(config)
    raise ProviderError(f"Unknown provider: {provider}")


def _log_call(
    db_path: Path,
    agent: str,
    provider: str,
    model: str,
    prompt_version: str,
    input_hash: str | None,
    output_hash: str | None,
    schema_version: str,
    latency_ms: int,
    success: bool,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                agent TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                input_hash TEXT,
                output_hash TEXT,
                schema_version TEXT,
                cost_usd REAL,
                latency_ms INTEGER,
                success BOOLEAN
            )
            """
        )
        conn.execute(
            """
            INSERT INTO llm_call_log (
                agent, provider, model, prompt_version, input_hash,
                output_hash, schema_version, cost_usd, latency_ms, success
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (agent, provider, model, prompt_version, input_hash, output_hash, schema_version, None, latency_ms, int(success)),
        )
        conn.commit()
    finally:
        conn.close()


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 120) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"HTTP {exc.code} from {url}: {body}") from exc
