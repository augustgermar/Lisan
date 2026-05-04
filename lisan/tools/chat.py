from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from ..config import load_config
from ..paths import sqlite_path, vault_root
from ..utils import today_iso
from .heuristic_gate import is_general_advice_question, score_text
from .log import log_error, tail_log


# ── ANSI helpers ─────────────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()


def _c(text: str, *codes: str) -> str:
    if not _USE_COLOR:
        return text
    return "".join(codes) + text + "\033[0m"


BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"


# ── Startup check ─────────────────────────────────────────────────────────────

def startup_check(vault: Path, config: dict[str, Any]) -> bool:
    """Verify vault, index, and provider. Auto-fix what can be fixed. Returns True if ready."""
    print(_c("Checking system…", DIM))

    vault_ok = _check_vault(vault)
    index_ok = _check_index(vault)
    provider_name, provider_ok = _check_provider(config)

    if provider_ok:
        print(f"  {_c('✓', GREEN)} Provider: {provider_name}")
    else:
        print(
            f"  {_c('!', YELLOW)} Provider: {provider_name} not reachable\n"
            f"    Set CODEX_BIN or add an API key, then update routing in config.yaml"
        )

    print()
    return vault_ok and index_ok and provider_ok


def _check_vault(vault: Path) -> bool:
    if vault.exists():
        print(f"  {_c('✓', GREEN)} Vault: {vault}")
        return True
    print(f"  {_c('!', YELLOW)} Vault not found — initializing {vault}")
    try:
        from ..paths import ensure_repo_layout
        ensure_repo_layout()
        print(f"  {_c('✓', GREEN)} Vault initialized")
        return True
    except Exception as exc:
        print(f"  {_c('✗', RED)} Vault init failed: {exc}")
        return False


def _check_index(vault: Path) -> bool:
    db = sqlite_path()
    needs_rebuild = not db.exists()
    if db.exists():
        try:
            conn = sqlite3.connect(db)
            count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            conn.close()
            if count == 0:
                needs_rebuild = True
            else:
                print(f"  {_c('✓', GREEN)} Index: {count} record{'s' if count != 1 else ''}")
                return True
        except Exception:
            needs_rebuild = True

    print(f"  {_c('!', YELLOW)} Index missing or empty — rebuilding…")
    try:
        from ..tools.rebuild_index import rebuild_index
        counts = rebuild_index(vault)
        print(f"  {_c('✓', GREEN)} Index built: {counts['files']} records")
        return True
    except Exception as exc:
        print(f"  {_c('✗', RED)} Index rebuild failed: {exc}")
        return False


def _check_provider(config: dict[str, Any]) -> tuple[str, bool]:
    routing = config.get("routing", {})
    name = str(routing.get("elicitor", {}).get("medium", "codex"))

    if name == "codex":
        binary_env = config.get("providers", {}).get("codex", {}).get("binary_env", "CODEX_BIN")
        binary = os.environ.get(binary_env) or "codex"
        return name, bool(shutil.which(binary))

    if name in ("openai", "anthropic", "google"):
        key_env = str(config.get("providers", {}).get(name, {}).get("api_key_env") or "")
        return name, bool(key_env and os.environ.get(key_env))

    return name, True  # local / unknown — assume reachable


# ── Chat loop ─────────────────────────────────────────────────────────────────

def run_chat(
    vault: Path,
    conversation_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> int:
    from .. import __version__

    config = load_config()
    ready = startup_check(vault, config)

    conv_id = conversation_id or today_iso()
    _print_header(__version__, conv_id)

    if not ready:
        print(
            _c("  No provider is reachable. Configure one in config.yaml before chatting.\n", YELLOW)
        )
        # Don't hard-exit — let the user at least see the interface.

    _enable_readline()

    from ..tools.capture import capture_text
    from ..tools.narrative_state import reset_narrative_state

    advice_history: list[dict[str, str]] = []
    advice_context_active = False

    while True:
        try:
            raw = input(_c("You: ", BOLD)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _farewell()
            return 0

        if not raw:
            continue

        lowered = raw.lower()

        if lowered in ("/quit", "/exit", "/q"):
            _farewell()
            return 0

        if lowered in ("/new", "/reset"):
            reset_narrative_state(vault, conv_id)
            conv_id = f"{today_iso()}-{int(time.time())}"
            print(_c(f"  New conversation: {conv_id}", DIM))
            print()
            continue

        if lowered == "/help":
            _print_help()
            continue

        if lowered == "/status":
            startup_check(vault, config)
            continue

        if lowered == "/id":
            print(_c(f"  conversation_id: {conv_id}", DIM))
            print()
            continue

        listener_score = score_text(raw, config)
        if _should_answer_directly(raw, listener_score, advice_context_active):
            try:
                response = _run_advice_response(
                    vault=vault,
                    text=raw,
                    provider=provider,
                    model=model,
                    history=advice_history,
                )
            except Exception as exc:
                log_error(vault, "chat.run_chat.advice", exc)
                print(_c(f"\n  Error: {exc}\n", RED))
                continue

            if response:
                advice_context_active = True
                advice_history.append({"speaker": "user", "text": raw})
                advice_history.append({"speaker": "assistant", "text": response})
                print()
                print(_c("Lisan: ", CYAN) + response)
                print()
                continue

        try:
            result = capture_text(
                vault=vault,
                text=raw,
                conversation_id=conv_id,
                speaker="USER",
                provider=provider,
                model=model,
            )
        except Exception as exc:
            log_error(vault, "chat.run_chat", exc)
            print(_c(f"\n  Error: {exc}\n", RED))
            continue

        if result.get("mode") != "skip":
            advice_context_active = False
        _render_response(result)


# ── Response rendering ────────────────────────────────────────────────────────

def _render_response(result: dict[str, Any]) -> None:
    action     = result.get("action", "skip")
    mode       = result.get("mode", "skip")
    elicitor   = result.get("elicitor") or {}
    draft_path = result.get("draft_path", "")

    response_text = str(elicitor.get("response") or "").strip()
    topic_closed  = str((elicitor.get("updated_narrative_state") or {}).get("mode_status", "")).lower() == "closed"

    if mode == "elicitor" and response_text:
        print()
        print(_c("Lisan: ", CYAN) + response_text)
        if topic_closed and draft_path:
            print(_c(f"  [draft saved → {draft_path}]", DIM))
        print()

    elif draft_path:
        print()
        print(_c("Lisan: ", CYAN) + "Got it — draft saved for review.")
        print(_c(f"  [→ {draft_path}]", DIM))
        print()

    elif action in ("full", "lightweight") and not draft_path:
        print()
        print(_c("Lisan: ", CYAN) + _c("Noted.", DIM))
        print()

    else:
        # Skipped — show a minimal acknowledgment so the UI doesn't look frozen
        print(_c("  ·", DIM))
        print()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print_header(version: str, conv_id: str) -> None:
    bar = _c("─" * 44, DIM)
    print(bar)
    print(_c(f"  Lisan  ·  v{version}  ·  {conv_id}", BOLD))
    print(bar)
    print(_c("  /new      start a new conversation", DIM))
    print(_c("  /status   system health check", DIM))
    print(_c("  /help     all commands", DIM))
    print(_c("  /quit     exit", DIM))
    print(bar)
    print()


def _print_help() -> None:
    print()
    print(_c("  Commands:", BOLD))
    print(_c("  /new        start a new conversation (clears narrative state)", DIM))
    print(_c("  /status     re-run system health check", DIM))
    print(_c("  /id         show the current conversation ID", DIM))
    print(_c("  /help       show this message", DIM))
    print(_c("  /quit       exit", DIM))
    print()
    print(_c("  Prefixes:", BOLD))
    print(_c("  /remember   force capture regardless of score", DIM))
    print(_c("  /forget     suppress capture for this turn", DIM))
    print()
    print(_c("  Advice questions are answered directly and are not stored in the vault.", DIM))
    print()


def _should_answer_directly(text: str, score: Any, advice_context_active: bool) -> bool:
    if score.action != "skip":
        return False
    if is_general_advice_question(text):
        return True
    return advice_context_active


def _run_advice_response(
    vault: Path,
    text: str,
    provider: str | None,
    model: str | None,
    history: list[dict[str, str]],
) -> str:
    from ..agents import AdviceAgent

    agent = AdviceAgent(vault=vault)
    result = agent.run(
        text,
        significance="low",
        provider=provider,
        model=model,
        conversation_history=_format_advice_history(history),
    )
    return str(result.text).strip()


def _format_advice_history(history: list[dict[str, str]]) -> str:
    if not history:
        return ""
    return json.dumps(history[-8:], indent=2, ensure_ascii=True)


def _farewell() -> None:
    print(_c("  Goodbye.", DIM))


def _enable_readline() -> None:
    try:
        import readline
        history = Path.home() / ".lisan_history"
        try:
            readline.read_history_file(history)
        except FileNotFoundError:
            pass
        import atexit
        atexit.register(readline.write_history_file, history)
        readline.set_history_length(500)
    except ImportError:
        pass
