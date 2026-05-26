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


class MockClient(ProviderClient):
    name = "mock"

    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        agent: str = "writer",
        significance: str = "medium",
        model: str | None = None,
    ) -> LLMResponse:
        text = self._response_for(agent=agent, prompt=prompt, schema=schema)
        if schema and not text.strip().startswith("{"):
            text = json.dumps({"response": text}, indent=2, ensure_ascii=True)
        return LLMResponse(text=text, provider=self.name, model=model or "mock")

    def _response_for(self, *, agent: str, prompt: str, schema: dict[str, Any] | None) -> str:
        lowered = prompt.lower()
        if agent == "listener":
            if any(marker in lowered for marker in ["/remember", "name is jordan", "daughter maya", "two cats", "pip", "varga"]):
                return json.dumps(
                    {
                        "worth_remembering": True,
                        "mode": "writer",
                        "reason": ["memory-worthy"],
                        "memory_events": [],
                        "action": "full",
                        "score": 9,
                        "seed_score": 8,
                        "narrative_score": 1,
                        "memory_type": "knowledge",
                    }
                )
            return json.dumps(
                {
                    "worth_remembering": False,
                    "mode": "skip",
                    "reason": ["routine chat"],
                    "memory_events": [],
                    "action": "skip",
                    "score": 1,
                    "seed_score": 0,
                    "narrative_score": 0,
                }
            )
        if agent == "writer":
            summary = "Memory draft"
            if "daughter maya" in lowered:
                summary = "Jordan is here with his daughter Maya watching a YouTube video about mixing ice cream flavors."
            elif "two cats" in lowered or "pip" in lowered or "varga" in lowered:
                summary = "Jordan has two cats named Momo and Boots."
            elif "name is jordan" in lowered:
                summary = "Jordan said his name is Jordan."
            return json.dumps(
                {
                    "record_type": "episode",
                    "summary": summary,
                    "significance": "medium",
                    "frontmatter": {
                        "summary": summary,
                        "significance": "medium",
                        "confidence": "low",
                        "confidence_basis": "mock provider",
                        "review_after": "",
                        "links": [],
                    },
                    "sections": {"event_timeline": prompt[:120]},
                    "questions": [],
                    "significance_rationale": "mock",
                    "entities_to_create": [{"name": "Jordan", "subtype": "person", "summary": "Jordan mentioned in conversation."}],
                    "evidence_to_create": [{"title": "Conversation evidence", "summary": summary, "source_type": "manual_note", "arena": "cross_arena", "reliability": "medium", "sensitivity": "low"}],
                    "claims_to_create": [{"claim_text": summary, "status": "active", "confidence": 0.6, "summary": summary}],
                    "state_updates": [{"category": "relational", "summary": summary, "confidence": "low"}],
                    "open_loops_to_create": [],
                    "decisions_to_create": [],
                }
            )
        if agent == "skeptic":
            return json.dumps(
                {
                    "approved": True,
                    "approved_for_dreamer": True,
                    "issues": [],
                    "risk": "low",
                    "recommended_action": "approve",
                    "priority_questions": [],
                    "observed_facts": [],
                    "interpretations": [],
                    "alternative_hypotheses": [],
                    "evidence_needed": [],
                    "claim_updates": [],
                    "confidence_adjustments": [],
                    "reasoning_errors": [],
                    "reviewed_record_id": "",
                    "reviewed_record_type": "draft",
                    "pattern_status": "approved",
                    "counterexample_search": {"performed": True},
                    "summary": "Approved",
                }
            )
        if agent == "interlocutor":
            if "daughter maya" in lowered:
                return json.dumps(
                    {
                        "response": "You're here with your daughter Maya, watching a YouTube video about mixing ice cream flavors.",
                        "questions": [],
                        "recommended_action": "auto_commit",
                        "updated_narrative_state": {"next_step": "Continue", "mode_status": "developing"},
                    }
                )
            if "two cats" in lowered or "pip" in lowered or "varga" in lowered:
                return json.dumps(
                    {
                        "response": "Got it. You have two cats, Momo and Boots.",
                        "questions": [],
                        "recommended_action": "auto_commit",
                        "updated_narrative_state": {"next_step": "Continue", "mode_status": "developing"},
                    }
                )
            if "name is jordan" in lowered:
                return json.dumps(
                    {
                        "response": "You want me to remember that you go by Jordan.",
                        "questions": [],
                        "recommended_action": "auto_commit",
                        "updated_narrative_state": {"next_step": "Continue", "mode_status": "developing"},
                    }
                )
            return json.dumps(
                {
                    "response": "Got it.",
                    "questions": [],
                    "recommended_action": "auto_commit",
                    "updated_narrative_state": {"next_step": "Continue", "mode_status": "developing"},
                }
            )
        if agent == "elicitor":
            if "daughter maya" in lowered:
                return json.dumps(
                    {
                        "response": "Got it. Maya is your daughter, and you're watching a YouTube video about mixing ice cream flavors.",
                        "updated_narrative_state": {"mode_status": "developing", "next_step": "Continue"},
                        "questions": [],
                    }
                )
            if "two cats" in lowered or "pip" in lowered or "varga" in lowered:
                return json.dumps(
                    {
                        "response": "Got it. You have two cats, Momo and Boots.",
                        "updated_narrative_state": {"mode_status": "developing", "next_step": "Continue"},
                        "questions": [],
                    }
                )
            if "name is jordan" in lowered:
                return json.dumps(
                    {
                        "response": "You want me to remember that you go by Jordan.",
                        "updated_narrative_state": {"mode_status": "developing", "next_step": "Continue"},
                        "questions": [],
                    }
                )
            return json.dumps(
                {
                    "response": "Tell me more.",
                    "updated_narrative_state": {"mode_status": "developing", "next_step": "Continue"},
                    "questions": [],
                }
            )
        if agent == "advice":
            if "psychologically manipulate" in lowered or "manipulate" in lowered:
                return "I can’t help with manipulating someone. Use boundaries, specific asks, de-escalation, and clear communication instead."
            if "what is your name" in lowered or "what are you" in lowered:
                return "My name is Lisan. I am your local personal assistant and memory system."
            if "do you know my name now" in lowered or "what is my name" in lowered:
                return "Your name is Jordan."
            return "Sure."
        return "OK"


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
        error_type: str | None = None
        try:
            response = client.complete(prompt, schema=schema, temperature=temperature, agent=agent, significance=significance, model=selected.model)
            response_text = response.text
            return response
        except Exception as exc:
            error_text = str(exc)
            error_type = exc.__class__.__name__
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
                error_type=error_type,
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
    if provider == "mock":
        return MockClient(config)
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
