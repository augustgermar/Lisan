from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .paths import config_path


DEFAULT_CONFIG: dict[str, Any] = {
    "providers": {
        "openai": {
            "enabled": True,
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://api.openai.com/v1/chat/completions",
            "default_model": "gpt-4o-mini",
        },
        "anthropic": {
            "enabled": True,
            "api_key_env": "ANTHROPIC_API_KEY",
            "base_url": "https://api.anthropic.com/v1/messages",
            "default_model": "claude-3-5-sonnet-latest",
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
            "base_url": "http://localhost:11434/v1/chat/completions",
            "default_model": "llama3.1",
        },
    },
    "routing": {
        "listener": {"low": "local", "medium": "local", "high": "openai"},
        "assembler": {"low": "local", "medium": "local", "high": "local"},
        "elicitor": {"low": "google", "medium": "openai", "high": "anthropic"},
        "writer": {"low": "local", "medium": "openai", "high": "anthropic"},
        "skeptic": {"low": "local", "medium": "anthropic", "high": "anthropic"},
        "interlocutor": {"low": "google", "medium": "openai", "high": "anthropic"},
        "dreamer": {"low": "local", "medium": "openai", "high": "anthropic"},
    },
    "heuristic": {
        "thresholds": {"skip": 3, "lightweight": 6},
        "affect_terms": ["angry", "sad", "anxious", "excited", "afraid", "frustrated"],
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

