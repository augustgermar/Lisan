from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .paths import config_path


_LOCAL_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1/chat/completions"
_LEGACY_OLLAMA_BASE_URL = "http://localhost:11434/v1/chat/completions"


DEFAULT_CONFIG: dict[str, Any] = {
    "mail": {
        "sender": "",
        "relay": "localhost",
        "port": 25,
        "default_domain": "",
    },
    "providers": {
        "local": {
            "enabled": True,
            "api_key_env": None,
            "base_url": _LOCAL_DEFAULT_BASE_URL,
            "default_model": None,
        },
        "rotato": {
            "base_url": "http://localhost:8990/gemflash/chat/completions",
            "default_model": "gemini-2.5-pro",
            "timeout_seconds": 120,
        },
        # Local `claude` CLI via the Claude Agent SDK (pip install claude-agent-sdk).
        # No API key. Text-only by default; `allowed_tools` is opt-in.
        # `fallback` names the provider tried when this one fails (str or list).
        "claude": {
            "enabled": True,
            "binary_env": "CLAUDE_BIN",
            "default_model": "claude-sonnet-5-5",
            "timeout_seconds": 300,
            "allowed_tools": [],
            "fallback": None,
        },
        "codex": {
            "enabled": True,
            "binary_env": "CODEX_BIN",
            "default_model": None,
            # Lisan cognition is read-only by default. Any write-capable
            # workflow must name an explicit, bounded capability profile;
            # prompt text is never an authority boundary.
            "sandbox_mode": "read-only",
            "all_agents_sandbox_mode": "read-only",
            # Per-agent override, e.g. {"writer": "workspace-write", "skeptic":
            # "read-only"}. An entry here for an agent's exact name wins over
            # both settings above. Empty until the owner dials individual
            # agents in — see lisan/providers/codex.py:_resolve_sandbox_mode.
            "sandbox_mode_by_agent": {},
            # Structural scope metadata for the default cognition profile.
            # `repo` and `vault` resolve through the canonical path seam;
            # the Codex sandbox remains the enforcement boundary.
            "capability_scopes": {
                "default": {
                    "filesystem": {"read": ["repo", "vault"], "write": []},
                    "network": False,
                    "identity": "lisan-cognition",
                    "duration_seconds": 900,
                },
            },
            # Wall-clock limit for one `codex exec`; 0 disables it. See
            # lisan/providers/codex.py:_resolve_timeout (LISAN_CODEX_TIMEOUT wins).
            "timeout_seconds": 1800,
        },
        "openai": {
            "enabled": False,
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://api.openai.com/v1/chat/completions",
            "default_model": "gpt-4o-mini",
        },
        "google": {
            "enabled": False,
            "api_key_env": "GOOGLE_API_KEY",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "default_model": "gemini-2.0-flash",
        },
        "openrouter": {
            "enabled": False,
            "api_key_env": "OPENROUTER_API_KEY",
            "base_url": "https://openrouter.ai/api/v1/chat/completions",
            "default_model": "mistralai/mistral-nemo",
        },
    },
    # Every agent that calls an LLM needs an entry here. "default" catches
    # anything not listed (a `lisan complete --agent` one-off, a derived
    # sub-agent name) so an omission degrades to the ordinary provider rather
    # than to whatever is listening on the local port — the 2026-08-16
    # self_repair_author failure. tests/test_provider_routing.py is the gate:
    # a new agent name without an entry fails the suite.
    "routing": {
        "default":            {"low": "codex", "medium": "codex", "high": "codex"},
        "router":             {"low": "codex", "medium": "codex", "high": "codex"},
        "listener":           {"low": "codex", "medium": "codex", "high": "codex"},
        "assembler":          {"low": "codex", "medium": "codex", "high": "codex"},
        "elicitor":           {"low": "codex", "medium": "codex", "high": "codex"},
        "writer":             {"low": "codex", "medium": "codex", "high": "codex"},
        "skeptic":            {"low": "codex", "medium": "codex", "high": "codex"},
        "interlocutor":       {"low": "codex", "medium": "codex", "high": "codex"},
        "dreamer":            {"low": "codex", "medium": "codex", "high": "codex"},
        "advice":             {"low": "codex", "medium": "codex", "high": "codex"},
        "analyst":            {"low": "codex", "medium": "codex", "high": "codex"},
        "adjutant":           {"low": "codex", "medium": "codex", "high": "codex"},
        # The executor label. Its two call sites construct CodexClient
        # directly, so this entry is not consulted today; it is here so the
        # name resolves correctly if either one ever routes through LisanLLM.
        "codex":              {"low": "codex", "medium": "codex", "high": "codex"},
        "delegate":           {"low": "codex", "medium": "codex", "high": "codex"},
        "skill_reviewer":     {"low": "codex", "medium": "codex", "high": "codex"},
        "provider_check":     {"low": "codex", "medium": "codex", "high": "codex"},
        "self_repair_author": {"low": "codex", "medium": "codex", "high": "codex"},
    },
    # How much vault fits in one provider window. The ceiling is the
    # provider's, not ours: codex rejects >1,048,576 characters outright.
    # Bundles larger than the budget are PARTITIONED across passes, not
    # trimmed — per_record_chars is a backstop against one pathological
    # record, deliberately set above the largest real one.
    "context": {
        "provider_input_chars": 1_048_576,
        "reserve_chars": 131_072,
        "per_record_chars": 100_000,
        "max_chunks": 12,
    },
    "heuristic": {
        "thresholds": {"skip": 3, "lightweight": 6},
        "high_stakes_terms": None,
        "biographical_terms": None,
        "affect_terms": None,
    },
    # IIP (interpersonal interpretation protocol): the deterministic
    # validator on interpretation-of-a-person turns. validator_enabled is
    # the owner's runtime kill switch; max_regenerations starts at 1
    # (owner-set 2026-07-15, revisit after a week of challenge-log data).
    "iip": {
        "validator_enabled": True,
        "max_regenerations": 1,
    },
    # The hypothesis language gate on MACHINE-AUTHORED psychology (analyst
    # patterns, predictions). User text is always stored verbatim; this
    # never touches it. null = built-in default terms; [] = gate disabled
    # (a deployment whose operator is licensed to mint diagnostic language);
    # a list replaces the default. Owner decision 2026-07-15.
    "psyche": {
        "banned_hypothesis_terms": None,
        # Automatic longitudinal analysis is deliberately quiet until there
        # is enough repeated observation to say something useful.
        "analyst_min_observations": 5,
        "analyst_min_weeks": 3,
        "context_tags": [
            "school-day", "caregiver-day", "schedule-change", "transition",
            "sleep", "illness", "appointment", "weekend", "holiday",
        ],
    },
    "enrichment": {
        "daily_cap": 2,
        "max_reads_per_loop": 5,
        "max_candidates_per_source": 5,
        "max_model_calls_per_loop": 2,
    },
    "conversation": {
        # Tool calls one turn may make before the agent must answer. Ten
        # suits a chat turn and starves a runbook: a single console step is
        # eight calls, so anything with stages needs headroom. Capped at 60
        # in code — a loop that cannot finish in 60 is a loop, not a task.
        "max_tool_iterations": 10,
    },
    "sources": {
        "local_files": {
            "enabled": False,
            "roots": [],
            "include_extensions": [".md", ".txt", ".json", ".csv", ".pdf"],
        },
        "web": {
            "enabled": False,
            # Every backend needs a key, read from api_key_env or from
            # <credentials_root>/<provider>.json. "tavily" grants 1,000
            # calls a month without a card; "brave" is metered as of
            # 2026-02 and asks for attribution. "html_scrape" is the
            # retired path: scraping a consumer engine returned results
            # unrelated to the query (2026-08-21).
            # "browser" needs no account: the owner's own Chrome drives a
            # search engine and the results are read off the page. "tavily"
            # and "brave" are keyed APIs, read from api_key_env or from
            # <credentials_root>/<provider>.json. "html_scrape" is retired:
            # scraping an engine without a browser session returned results
            # unrelated to the query (2026-08-21).
            "provider": "browser",
            "engines": ["google", "duckduckgo"],
            "settle_seconds": 2.5,
            "api_key_env": "TAVILY_API_KEY",
            "search_endpoint": "https://api.tavily.com/search",
            "search_depth": "basic",
            "include_raw_content": False,
            "timeout_seconds": 20,
        },
    },
    "adjutant": {
        # Master switch (WO-ADJUTANT). False = `adjutant run` is dry-run:
        # verdicts are logged, nothing executes. Only the owner flips this,
        # after watching the dry-run audit trail — the same graduated-
        # autonomy posture as action_policy tiers.
        "enabled": False,
        # Calibration posture (the soak): v2 writers ON so turns may acquire
        # task fields, and every cycle FORCED dry regardless of `enabled` —
        # the audit trail fills while execution stays impossible. The
        # key-turn sequence is: calibration on -> audit the log -> then
        # calibration off, enabled on.
        "calibration": False,
        "interval_minutes": 15,
        # Allowlist of directories run_script tasks may execute from.
        "script_dirs": [],
        # Allowlist of paths collect tasks may scan.
        "collect_paths": [],
    },
    "ingest": {
        "max_file_size_bytes": 5 * 1024 * 1024,
        "text_preview_chars": 4000,
        "skip_if_inside_vault": True,
        # Directories the fswatch adapter polls; new/changed files become
        # capture turns (conversation "fswatch"). Empty = adapter off.
        "fswatch_paths": [],
    },
    "jobs": {
        # After a capture writes records it enqueues index.rebuild_record
        # jobs. With drain_on_capture on (default), capture drains those indexing
        # jobs in-process before returning so semantic retrieval works without a
        # manual `lisan jobs run`. Only indexing jobs are drained; LLM-heavy
        # maintenance (analyst/dreamer) stays queued for batch/cron. Set False
        # for a pure-async caller that runs its own worker.
        "drain_on_capture": True,
    },
    # The learning loop (lisan/tools/learning.py, docs/learning_loop_workorder.md).
    "learning": {
        # off     nothing recorded
        # observe record events and the skill-usage ledger (deterministic, no
        #         model call, writes nothing to any skill) — the default
        # shadow / auto arrive with the reviewer (steps 2-3 of the work order)
        "mode": "observe",
        # A conversation turn is recorded at this many tool calls, or when it
        # used a skill.
        "min_tool_calls": 5,
        # ...or when it hands work to one of these. One run_codex call can be an
        # hour of shell work, so a call count undercounts it (measured: 5 of 583
        # real turns reached 5 calls; 65 handed work to the executor).
        "work_tools": ["execute_task", "run_codex"],
        # Reviewer (shadow / auto modes). A review runs once this many events have
        # not been reviewed and the system has been quiet for min_idle_minutes.
        "review_every": 6,
        "min_idle_minutes": 5,
        # How much one review reads: bounded by the context window, never by cost.
        "review_char_budget": 160000,
        "max_review_events": 12,
        # A lesson about the agent's own environment (sandbox, approvals,
        # permissions) needs evidence from within this many days: those rules
        # change within weeks, and old events describe a system that no longer exists.
        "environment_staleness_days": 14,
        # Largest SKILL.md the loop may write (a reference file may be twice this).
        "max_skill_bytes": 16000,
        # Auto mode applies at most this many gate-passing changes per review: a
        # bound on what any single review can do. The rest stay in its artifact.
        "auto_apply_max_per_review": 3,
        # Tell the owner when a review proposed something.
        "digest": True,
    },
    # Delegated workers (lisan/tools/delegation.py, docs/delegation_workorder.md).
    "delegation": {
        # How many delegated children run at once (the scheduler's delegate lane).
        "max_concurrent": 3,
        # Queued + running children allowed at once; delegate() refuses beyond it.
        "max_outstanding": 12,
        # Children one fan-out step or one chat delegate call may start.
        "max_children": 6,
        # Wall limit for a child that does not name one. Hard ceiling is 2400s:
        # the queue requeues any job "running" past 45 minutes, so a longer
        # child would be run twice.
        "default_timeout_seconds": 900,
    },
    "backup": {
        "destination_dir": "backups",
        "encrypt_by_default": False,
        "recipient_env": "LISAN_BACKUP_RECIPIENT",
        "identity_env": "LISAN_BACKUP_IDENTITY",
        "age_binary_env": "AGE_BIN",
    },
    "retrieval": {
        "fusion": {
            "enabled": True,
            "method": "rrf",
            "rrf_k": 60,
            "per_layer_limit": 30,
            "fused_limit": 20,
        },
        "embeddings": {
            # mode: auto | semantic | hash
            #   auto     - attempt the semantic embedder; the embed call itself
            #              is the reachability probe. If reachable, use it; if
            #              not, apply unreachable_policy. Full semantic search
            #              out of the box whenever a server is present.
            #   semantic - same as auto, but emit a loud WARNING when the
            #              embedder is unreachable (for an operator who expects
            #              it to be up).
            #   hash     - deterministic hash only; never touches the network
            #              (reproducible CI / byte-stable baseline).
            "mode": "auto",
            # provider: fastembed | local | sentence-transformers
            #   fastembed (default, recommended) - in-process ONNX embedder.
            #     Installed by default with Lisan. The `embeddings` extra remains
            #     as a compatibility alias for older install flows.
            #   local - OpenAI-compatible POST {base_url}/v1/embeddings server.
            #   sentence-transformers - secondary in-process backend (lazy).
            "provider": "fastembed",
            # FastEmbed default model (BGE small, 384-dim). For provider:local
            # set this to your server's embedding model instead.
            "model": "BAAI/bge-small-en-v1.5",
            # HINT ONLY. The authoritative dimension is whatever the embedder
            # actually returns; it is written into the embeddings.bin header. If
            # the observed dimension differs from this hint we warn.
            "dimensions": 384,
            # FastEmbed weight cache. null -> $FASTEMBED_CACHE_PATH, else
            # ~/.cache/lisan/fastembed (never the system temp dir). First use
            # downloads the model (~90MB for the default).
            "cache_dir": None,
            # Query/passage convention. The default model (BGE small) does not
            # apply any distinction through FastEmbed's native query_embed /
            # passage_embed methods, so we apply its DOCUMENTED convention
            # explicitly: queries get the instruction prefix, passages get none.
            # This is the silent-quality footgun, defaulted correctly.
            #   - Set both to null to defer to FastEmbed's native methods (use
            #     this for a model whose FastEmbed build applies its own
            #     query/passage logic).
            #   - Set custom strings for a model with a different convention.
            # Changing passage_prefix requires a full rebuild-index (every
            # stored passage vector changes). Changing query_prefix does NOT:
            # query vectors are computed fresh per query against the existing
            # bare passage vectors, so the new instruction just takes effect on
            # the next query.
            "query_prefix": "Represent this sentence for searching relevant passages: ",
            "passage_prefix": "",
            # Used by provider:local (the HTTP endpoint).
            "base_url": "http://127.0.0.1:8080",
            "api_key_env": None,
            "timeout_seconds": 30,
            "batch_size": 64,
            # unreachable_policy: skip | hash
            #   skip - drop the vector leg and mark records embedding_status=
            #          "pending" so a later sweep can re-embed them (default).
            #          Never writes hash vectors into a semantic index.
            #   hash - substitute deterministic hash vectors.
            "unreachable_policy": "skip",
            # Only used by mode:hash or unreachable_policy:hash.
            "hash_dimensions": 32,
        },
    },
}


def embedding_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the retrieval.embeddings block, backfilling any missing keys
    from the defaults so older config files keep working."""
    defaults = DEFAULT_CONFIG["retrieval"]["embeddings"]
    merged = deepcopy(defaults)
    block = config.get("retrieval", {}).get("embeddings", {})
    if isinstance(block, dict):
        merged.update(block)
    return merged


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or config_path()
    if not cfg_path.exists():
        return deepcopy(DEFAULT_CONFIG)
    raw = cfg_path.read_text(encoding="utf-8").strip()
    if not raw:
        return deepcopy(DEFAULT_CONFIG)
    data = json.loads(raw)
    merged = deepcopy(DEFAULT_CONFIG)
    _deep_merge(merged, data)
    _normalize_local_provider_defaults(merged)
    return merged


def save_default_config(path: Path | None = None) -> Path:
    cfg_path = path or config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return cfg_path


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _normalize_local_provider_defaults(config: dict[str, Any]) -> None:
    local = config.get("providers", {}).get("local", {})
    if local.get("base_url") == _LEGACY_OLLAMA_BASE_URL:
        local["base_url"] = _LOCAL_DEFAULT_BASE_URL
