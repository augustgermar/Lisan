"""Telegram bridge for Lisan.

Drives the same chat-turn pipeline as the interactive CLI
(:func:`lisan.tools.chat._process_chat_turn`) over Telegram long-polling.
Pure standard library (``urllib``) — no third-party dependency, matching
Lisan's stdlib-only core.

Configuration is read from the environment (never committed):

    LISAN_TELEGRAM_TOKEN     Bot token from @BotFather (required).
    LISAN_TELEGRAM_ALLOWED   Comma-separated numeric Telegram user IDs allowed
                             to talk to the bot. Empty/unset means refuse
                             everyone (safe default) — set it to your own id.

A ``telegram`` block in ``config.yaml`` may supply the same values
(``token`` / ``allowed_user_ids``); the environment takes precedence.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..config import load_config
from ..paths import vault_root
from ..utils import today_iso
from .chat import _process_chat_turn, startup_check
from .log import get_logger, log_error
from .transcripts import append_transcript

_API_URL = "https://api.telegram.org/bot{token}/{method}"
_MSG_LIMIT = 4096          # Telegram hard limit per message
_POLL_TIMEOUT = 50         # long-poll seconds (server holds the request)
_HTTP_MARGIN = 15          # socket timeout beyond the long-poll window

_HELP_TEXT = (
    "Lisan over Telegram.\n\n"
    "Just send a message and I'll respond and remember what matters.\n\n"
    "Commands:\n"
    "/new — start a fresh conversation\n"
    "/domain <name> — pin the retrieval domain (no arg clears it)\n"
    "/logs [N] — show recent log lines\n"
    "/help — this message"
)


class _ChatState:
    """Per-Telegram-chat state, mirroring what run_chat threads between turns."""

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.advice_history: list[dict[str, str]] = []
        self.advice_context_active = False
        self.advice_topic: str | None = None
        self.domain_override: str | None = None


def _chunk(text: str, limit: int = _MSG_LIMIT) -> list[str]:
    """Split text into <=limit pieces, preferring newline boundaries."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


class TelegramBot:
    def __init__(
        self,
        *,
        token: str,
        allowed_user_ids: set[int],
        vault: Path,
        provider: str | None = None,
        model: str | None = None,
        db_path: Path | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.token = token
        self.allowed_user_ids = allowed_user_ids
        self.vault = vault
        self.provider = provider
        self.model = model
        self.db_path = db_path
        self.config = config if config is not None else load_config()
        self._chats: dict[int, _ChatState] = {}
        self._offset: int | None = None

    # ── Telegram transport (mockable in tests) ──────────────────────────────
    def _call_api(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        url = _API_URL.format(token=self.token, method=method)
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _send_message(self, chat_id: int, text: str) -> None:
        for piece in _chunk(text):
            try:
                self._call_api("sendMessage", {"chat_id": chat_id, "text": piece}, timeout=30)
            except Exception as exc:  # network hiccup shouldn't kill the bot
                log_error(self.vault, "telegram sendMessage failed", exc)

    def _typing(self, chat_id: int) -> None:
        try:
            self._call_api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
        except Exception:
            pass  # purely cosmetic

    # ── State + auth ────────────────────────────────────────────────────────
    def _is_allowed(self, user_id: int) -> bool:
        return user_id in self.allowed_user_ids

    def _state_for(self, chat_id: int) -> _ChatState:
        state = self._chats.get(chat_id)
        if state is None:
            state = _ChatState(conversation_id=f"telegram-{chat_id}-{today_iso()}")
            self._chats[chat_id] = state
        return state

    # ── Update handling (unit-tested without network) ───────────────────────
    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        user_id = sender.get("id")
        if chat_id is None or user_id is None:
            return

        if not self._is_allowed(int(user_id)):
            self._send_message(int(chat_id), "Not authorized.")
            get_logger(self.vault).warning("telegram: rejected unauthorized user %s", user_id)
            return

        self._handle_text(int(chat_id), text.strip())

    def _handle_text(self, chat_id: int, text: str) -> None:
        state = self._state_for(chat_id)
        lowered = text.lower()

        if lowered in ("/start", "/help"):
            self._send_message(chat_id, _HELP_TEXT)
            return
        if lowered in ("/new", "/reset"):
            from .narrative_state import reset_narrative_state

            reset_narrative_state(self.vault, state.conversation_id)
            state.conversation_id = f"telegram-{chat_id}-{int(time.time())}"
            state.advice_history = []
            state.advice_context_active = False
            state.advice_topic = None
            self._send_message(chat_id, "Started a fresh conversation.")
            return
        if lowered.startswith("/domain") or lowered.startswith("/arena"):
            parts = text.split(maxsplit=1)
            state.domain_override = parts[1].strip().lower() if len(parts) > 1 and parts[1].strip() else None
            msg = f"Domain set to: {state.domain_override}" if state.domain_override else "Domain cleared (auto-detect)."
            self._send_message(chat_id, msg)
            return
        if lowered.startswith("/logs"):
            from .log import tail_log

            n = 20
            parts = lowered.split()
            if len(parts) > 1:
                try:
                    n = int(parts[1])
                except ValueError:
                    pass
            self._send_message(chat_id, tail_log(self.vault, lines=n) or "(no logs)")
            return

        # Normal turn — show "typing" while the model works, then reply.
        self._typing(chat_id)
        try:
            result = _process_chat_turn(
                vault=self.vault,
                conversation_id=state.conversation_id,
                text=text,
                provider=self.provider,
                model=self.model,
                advice_history=state.advice_history,
                advice_context_active=state.advice_context_active,
                advice_topic=state.advice_topic,
                domain_override=state.domain_override,
                db_path=self.db_path,
            )
        except Exception as exc:
            log_error(self.vault, "telegram turn failed", exc)
            self._send_message(chat_id, f"Something went wrong handling that: {exc}")
            return

        response = str(result.get("response") or "").strip()
        self._update_state_after_turn(state, result, text, response)
        self._send_message(chat_id, response or "(no response)")

    def _update_state_after_turn(self, state: _ChatState, result: dict[str, Any], text: str, response: str) -> None:
        """Mirror run_chat's advice-context bookkeeping so multi-turn advice works."""
        if response and not result.get("provider_failure") and result.get("route") == "advice":
            state.advice_context_active = True
            state.advice_topic = str(result.get("topic") or state.advice_topic or "")
            state.advice_history.append({"speaker": "user", "text": str(result.get("content_text") or text)})
            state.advice_history.append({"speaker": "assistant", "text": response})
            append_transcript(vault=self.vault, conversation_id=state.conversation_id, speaker="LISAN", text=response)
        else:
            state.advice_context_active = False
            if result.get("route") != "advice":
                state.advice_topic = None

    # ── Long-poll loop ──────────────────────────────────────────────────────
    def poll_once(self) -> int:
        """Fetch and dispatch one batch of updates. Returns the count handled."""
        params: dict[str, Any] = {"timeout": _POLL_TIMEOUT}
        if self._offset is not None:
            params["offset"] = self._offset
        payload = self._call_api("getUpdates", params, timeout=_POLL_TIMEOUT + _HTTP_MARGIN)
        updates = payload.get("result") or []
        for update in updates:
            self._offset = int(update["update_id"]) + 1
            try:
                self.handle_update(update)
            except Exception as exc:  # one bad update must not kill the loop
                log_error(self.vault, "telegram handle_update error", exc)
        return len(updates)

    def run(self, *, _forever: bool = True, _on_idle: Callable[[], None] | None = None) -> int:
        backoff = 1
        while True:
            try:
                self.poll_once()
                backoff = 1
            except urllib.error.URLError as exc:
                log_error(self.vault, f"telegram poll network error; retry in {backoff}s", exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                log_error(self.vault, f"telegram poll error; retry in {backoff}s", exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            if not _forever:
                if _on_idle:
                    _on_idle()
                return 0


def _resolve_settings(config: dict[str, Any]) -> tuple[str, set[int]]:
    """Token + allowlist from env (preferred) or the config telegram block."""
    tg_cfg = config.get("telegram", {}) if isinstance(config.get("telegram"), dict) else {}

    token = (os.environ.get("LISAN_TELEGRAM_TOKEN") or str(tg_cfg.get("token") or "")).strip()

    raw_allowed = os.environ.get("LISAN_TELEGRAM_ALLOWED")
    if raw_allowed is None:
        raw_allowed = tg_cfg.get("allowed_user_ids") or ""
    if isinstance(raw_allowed, (list, tuple)):
        allowed_iter = [str(x) for x in raw_allowed]
    else:
        allowed_iter = str(raw_allowed).split(",")
    allowed = {int(x.strip()) for x in allowed_iter if str(x).strip().lstrip("-").isdigit()}

    return token, allowed


def run_telegram_bot(
    *,
    vault: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    db_path: Path | None = None,
) -> int:
    vault = vault or vault_root()
    config = load_config()
    token, allowed = _resolve_settings(config)

    if not token:
        print("✗ No Telegram bot token. Set LISAN_TELEGRAM_TOKEN (from @BotFather).")
        return 1
    if not allowed:
        print(
            "✗ No allowed users. Set LISAN_TELEGRAM_ALLOWED to your numeric Telegram\n"
            "  user id (from @userinfobot) so only you can talk to the bot."
        )
        return 1

    # Surface provider reachability up front (informational; doesn't block).
    try:
        startup_check(vault, config)
    except Exception:
        pass

    bot = TelegramBot(
        token=token,
        allowed_user_ids=allowed,
        vault=vault,
        provider=provider,
        model=model,
        db_path=db_path,
        config=config,
    )
    print(f"⚕ Lisan Telegram bot running — {len(allowed)} allowed user(s). Ctrl-C to stop.")
    return bot.run()
