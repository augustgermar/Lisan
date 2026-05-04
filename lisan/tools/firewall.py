from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .log import log_error


_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(previous|all|prior)\s+(instructions?|context|rules?|system)", re.IGNORECASE),
    re.compile(r"(new|updated?)\s+(system\s+)?prompt[:\s]", re.IGNORECASE),
    re.compile(r"(disregard|forget|override)\s+(everything|all\s+instructions?|system|previous)", re.IGNORECASE),
    re.compile(r"act\s+as\s+(if\s+you\s+are\s+)?a?\s*(different|new|unrestricted)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+a", re.IGNORECASE),
    re.compile(r"</?(system|instruction|context|prompt)\s*/?>", re.IGNORECASE),
    re.compile(r"\[INST\]|\[/INST\]|\[SYS\]", re.IGNORECASE),
    re.compile(r"<\|im_start\|>|<\|im_end\|>", re.IGNORECASE),
)

_SENTINEL = "[LISAN_CONTENT_BLOCKED]"


@dataclass(slots=True)
class FirewallResult:
    text: str
    flagged: bool
    patterns_found: list[str]

    @property
    def clean(self) -> bool:
        return not self.flagged


def scan_text(text: str, vault: Path | None = None) -> FirewallResult:
    """
    Detect prompt injection attempts in user-provided text.

    Flagged text is returned with suspicious patterns replaced.
    The original text is never executed as an instruction.
    """
    found: list[str] = []
    sanitized = text
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(sanitized):
            found.append(pattern.pattern)
            sanitized = pattern.sub(_SENTINEL, sanitized)

    if found and vault is not None:
        try:
            log_error(vault, "firewall.injection_detected", ValueError(
                f"Potential prompt injection blocked: {found[:3]}"
            ))
        except Exception:
            pass

    return FirewallResult(text=sanitized, flagged=bool(found), patterns_found=found)
