from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from ..paths import vault_root


_logger: logging.Logger | None = None


def get_logger(vault: Path | None = None) -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    vault = vault or vault_root()
    log_dir = vault / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "lisan.log"

    logger = logging.getLogger("lisan")
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,  # 2 MB per file
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(handler)

    _logger = logger
    return logger


def log_capture(vault: Path, text: str, result: dict[str, Any]) -> None:
    logger = get_logger(vault)
    score   = result.get("listener", {}).get("score", "?")
    action  = result.get("action", "?")
    mode    = result.get("mode", "?")
    reasons = result.get("listener", {}).get("reason", [])
    draft   = result.get("draft_path") or ""
    logger.info(
        f"capture | score={score} action={action} mode={mode} "
        f"reasons={reasons} draft={'yes' if draft else 'no'} "
        f"text={text[:80]!r}"
    )


def log_error(vault: Path, context: str, exc: Exception) -> None:
    get_logger(vault).error(f"{context}: {exc}", exc_info=True)


def tail_log(vault: Path | None = None, lines: int = 50) -> str:
    vault = vault or vault_root()
    log_path = vault / "logs" / "lisan.log"
    if not log_path.exists():
        return "No log file found."
    all_lines = log_path.read_text(encoding="utf-8").splitlines()
    return "\n".join(all_lines[-lines:])
