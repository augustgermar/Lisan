"""Terminal styling for interactive output. Colors apply only on a TTY.

The palette is a codex-style scheme: blues for the agent and structure, soft
greys for chrome and secondary text, sparing green/yellow/red for status.
256-color codes are used where they read better than the 8-color basics, with
graceful fallback on terminals that don't support them.
"""
from __future__ import annotations

import os
import sys

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"

# 8-color basics (kept for existing call sites)
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

# Codex-style blue/grey palette (256-color)
BLUE = "\033[38;5;39m"        # bright azure — the agent's voice
BLUE_DEEP = "\033[38;5;33m"   # deeper blue — headings, the rule
SKY = "\033[38;5;117m"        # pale sky blue — accents, prompt glyph
GREY = "\033[38;5;245m"       # mid grey — secondary text
GREY_DIM = "\033[38;5;240m"   # dim grey — chrome, separators
GREY_FAINT = "\033[38;5;236m" # faintest grey — rules, backgrounds


def color(text: str, *codes: str) -> str:
    if not _USE_COLOR:
        return text
    return "".join(codes) + text + RESET


def supports_color() -> bool:
    return _USE_COLOR
