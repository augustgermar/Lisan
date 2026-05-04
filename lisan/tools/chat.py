from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
import threading
from pathlib import Path
from typing import Any

from ..config import load_config
from ..paths import sqlite_path, vault_root
from ..utils import today_iso
from .conversation_policy import assess_conversation_turn
from .heuristic_gate import score_text
from .log import log_error, tail_log
from .transcripts import append_transcript
from .turn_router import decide_turn_route


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
    advice_topic: str | None = None
    arena_override: str | None = None

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

        if lowered.startswith("/logs"):
            n = 20
            parts = lowered.split()
            if len(parts) > 1:
                try:
                    n = int(parts[1])
                except ValueError:
                    pass
            print(_c(tail_log(vault, lines=n), DIM))
            print()
            continue

        if lowered.startswith("/arena"):
            parts = raw.split(maxsplit=1)
            if len(parts) > 1:
                arena_override = parts[1].strip().lower() or None
                print(_c(f"  Arena context set to: {arena_override}", DIM))
            else:
                arena_override = None
                print(_c("  Arena context cleared (auto-detect)", DIM))
            print()
            continue

        # Strip /remember and /forget prefixes before routing and capture
        text = raw
        if lowered.startswith("/remember "):
            text = raw[len("/remember "):].strip()
        elif lowered.startswith("/forget "):
            text = raw[len("/forget "):].strip()

        listener_score = score_text(raw, config, db_path=sqlite_path())
        current_state = _load_current_state(vault, conv_id)
        route_hint = _run_with_thinking_indicator(
            lambda: decide_turn_route(
                vault=vault,
                text=text,
                conversation_id=conv_id,
                provider=provider,
                model=model,
                listener_score=listener_score.as_dict(),
                advice_context_active=advice_context_active,
                advice_topic=advice_topic,
            )
        )
        policy = assess_conversation_turn(
            text,
            state=current_state,
            listener=listener_score.as_dict(),
            advice_context_active=advice_context_active,
            advice_topic=advice_topic,
            route_hint=route_hint.as_dict(),
        )
        if _should_answer_directly(text, listener_score, advice_context_active, policy, route_hint=route_hint.as_dict()):
            try:
                response = _run_with_thinking_indicator(
                    lambda: _run_advice_response(
                        vault=vault,
                        text=text,
                        provider=provider,
                        model=model,
                        history=advice_history,
                        conversation_policy=policy,
                    )
                )
            except Exception as exc:
                log_error(vault, "chat.run_chat.advice", exc)
                print(_c(f"\n  Error: {exc}\n", RED))
                continue

            if response:
                advice_context_active = True
                advice_topic = route_hint.topic_hint or policy.topic
                advice_history.append({"speaker": "user", "text": text})
                advice_history.append({"speaker": "assistant", "text": response})
                append_transcript(vault=vault, conversation_id=conv_id, speaker="LISAN", text=response)
                print()
                print(_c("Lisan: ", CYAN) + response)
                print()
                continue

        effective_policy = dict(policy.as_dict())
        if arena_override:
            effective_policy["arena_override"] = arena_override

        try:
            result = _run_with_thinking_indicator(
                lambda: capture_text(
                    vault=vault,
                    text=text,
                conversation_id=conv_id,
                speaker="USER",
                provider=provider,
                model=model,
                conversation_policy=effective_policy,
            )
            )
        except Exception as exc:
            log_error(vault, "chat.run_chat", exc)
            print(_c(f"\n  Error: {exc}\n", RED))
            continue

        if policy.route != "advice" or result.get("mode") != "skip":
            advice_context_active = False
            if policy.route != "advice":
                advice_topic = None
        _render_response(result, vault=vault, conversation_id=conv_id)


# ── Response rendering ────────────────────────────────────────────────────────

def _render_response(result: dict[str, Any], vault: Path | None = None, conversation_id: str | None = None) -> None:
    mode       = result.get("mode", "skip")
    elicitor   = result.get("elicitor") or {}

    response_text = str(elicitor.get("response") or "").strip()

    if mode == "elicitor" and response_text:
        if vault and conversation_id:
            append_transcript(vault=vault, conversation_id=conversation_id, speaker="LISAN", text=response_text)
        print()
        print(_c("Lisan: ", CYAN) + response_text)
        print()
    else:
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
    print(_c("  /logs     show recent log entries (/logs N for last N lines)", DIM))
    print(_c("  /quit     exit", DIM))
    print(bar)
    print()


def _print_help() -> None:
    print()
    print(_c("  Commands:", BOLD))
    print(_c("  /new        start a new conversation (clears narrative state)", DIM))
    print(_c("  /status     re-run system health check", DIM))
    print(_c("  /id         show the current conversation ID", DIM))
    print(_c("  /logs [N]   show last N log lines (default 20)", DIM))
    print(_c("  /arena [name]  override retrieval arena (empty to clear)", DIM))
    print(_c("  /help       show this message", DIM))
    print(_c("  /quit       exit", DIM))
    print()
    print(_c("  Prefixes:", BOLD))
    print(_c("  /remember   force capture regardless of score", DIM))
    print(_c("  /forget     suppress capture for this turn", DIM))
    print()
    print(_c("  Advice questions are answered directly and are not stored in the vault.", DIM))
    print()


def _should_answer_directly(
    text: str,
    score: Any,
    advice_context_active: bool,
    policy: Any | None = None,
    route_hint: Any | None = None,
) -> bool:
    route = str((route_hint or {}).get("route") or "").lower()
    if route == "advice":
        return True
    if route == "memory":
        return False
    return advice_context_active


def _run_advice_response(
    vault: Path,
    text: str,
    provider: str | None,
    model: str | None,
    history: list[dict[str, str]],
    conversation_policy: Any | None = None,
) -> str:
    from ..agents import AdviceAgent

    agent = AdviceAgent(vault=vault)
    result = agent.run(
        text,
        significance="low",
        provider=provider,
        model=model,
        conversation_history=_format_advice_history(history),
        conversation_policy=conversation_policy.as_dict() if conversation_policy is not None else {},
    )
    return str(result.text).strip()


def _run_with_thinking_indicator(callable_obj):
    done = threading.Event()
    started = time.time()

    def _show_waiting() -> None:
        if not done.wait(0.7):
            print()
            print(_c("Lisan: ", CYAN) + _c("thinking…", DIM))

    watcher = threading.Thread(target=_show_waiting, daemon=True)
    watcher.start()
    try:
        return callable_obj()
    finally:
        done.set()
        elapsed_ms = int((time.time() - started) * 1000)
        if elapsed_ms >= 700:
            print(_c(f"  [took {elapsed_ms} ms]", DIM))


def _load_current_state(vault: Path, conversation_id: str) -> Any:
    from .narrative_state import load_narrative_state

    try:
        return load_narrative_state(vault, conversation_id)
    except Exception:
        return None


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
