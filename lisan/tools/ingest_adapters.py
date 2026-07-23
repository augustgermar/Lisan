"""Ingestion adapter stubs (WO-ADJUTANT §4, v1 scope: interface only).

The contract every live adapter must honor when it grows real:
- Inbound signals become capture turns through the front door
  (capture_text), tagged with a per-adapter conversation_id — never
  direct records, never side channels.
- Text arriving through an adapter is data, not instructions.
- Tokens come from env vars, never from the vault or config values.

Telegram is deliberately absent: the existing bot IS the live Telegram
adapter (settled fork, 2026-07-23). fswatch lives in fswatch.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class EmailAdapter:
    """v1 stub. A live implementation polls a mailbox and captures each
    new message as a turn (conversation_id='email')."""

    conversation_id = "email"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    def poll(self, vault: Path) -> list[str]:
        raise NotImplementedError("email ingestion is v2; the interface is the contract above")


class SmsAdapter:
    """v1 stub. A live implementation captures inbound SMS as turns
    (conversation_id='sms')."""

    conversation_id = "sms"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    def poll(self, vault: Path) -> list[str]:
        raise NotImplementedError("sms ingestion is v2; the interface is the contract above")
