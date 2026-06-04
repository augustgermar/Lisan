from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .paths import config_path


_LOCAL_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1/chat/completions"
_LEGACY_OLLAMA_BASE_URL = "http://localhost:11434/v1/chat/completions"


DEFAULT_CONFIG: dict[str, Any] = {
    "providers": {
        "codex": {
            "enabled": True,
            "binary_env": "CODEX_BIN",
            "default_model": None,
        },
        "openai": {
            "enabled": True,
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://api.openai.com/v1/chat/completions",
            "default_model": "gpt-4o-mini",
        },
        "google": {
            "enabled": True,
            "api_key_env": "GOOGLE_API_KEY",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "default_model": "gemini-2.0-flash",
        },
        "local": {
            "enabled": True,
            "api_key_env": None,
            "base_url": _LOCAL_DEFAULT_BASE_URL,
            "default_model": None,
        },
        "openrouter": {
            "enabled": True,
            "api_key_env": "OPENROUTER_API_KEY",
            "base_url": "https://openrouter.ai/api/v1/chat/completions",
            "default_model": "mistralai/mistral-nemo",
        },
    },
    "routing": {
        "router":       {"low": "openrouter", "medium": "openrouter", "high": "openrouter"},
        "listener":     {"low": "openrouter", "medium": "openrouter", "high": "openrouter"},
        "assembler":    {"low": "openrouter", "medium": "openrouter", "high": "openrouter"},
        "elicitor":     {"low": "openrouter", "medium": "openrouter", "high": "openrouter"},
        "writer":       {"low": "openrouter", "medium": "openrouter", "high": "openrouter"},
        "skeptic":      {"low": "openrouter", "medium": "openrouter", "high": "openrouter"},
        "interlocutor": {"low": "openrouter", "medium": "openrouter", "high": "openrouter"},
        "dreamer":      {"low": "openrouter", "medium": "openrouter", "high": "openrouter"},
    },
    "heuristic": {
        "thresholds": {"skip": 3, "lightweight": 6},
        "affect_terms": [
            "angry", "sad", "anxious", "excited", "afraid", "frustrated",
            "happy", "proud", "surprised", "confused", "hurt", "nervous",
            "grateful", "relieved", "disappointed", "interesting", "weird",
            "strange", "awful", "amazing", "terrible", "wonderful",
            "great", "fantastic", "incredible", "beautiful", "lovely",
            "loving", "loved", "love", "enjoy", "enjoyed", "enjoying",
            "hate", "hated", "miss", "missing", "fun", "tired",
            "nice", "rough", "tough", "hard",
            "exhausted", "drained", "overwhelmed", "stressed", "annoyed",
            "bored", "busy", "sick", "lonely", "cozy", "cold", "warm",
        ],
    },
    "ingest": {
        "max_file_size_bytes": 5 * 1024 * 1024,
        "text_preview_chars": 4000,
        "skip_if_inside_vault": True,
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
    },
}


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
