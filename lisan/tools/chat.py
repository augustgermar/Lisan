from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
import threading
import uuid
from pathlib import Path
from typing import Any

from ..config import load_config
from ..paths import sqlite_path, vault_root
from ..utils import today_iso
from .chat_turns import classify_turn
from .conversation_policy import assess_conversation_turn
from .log import log_error, tail_log
from .transcripts import append_transcript
from .tracing import finalize_turn_trace, record_inline_step, record_jobs_queued, reset_current_turn_trace, start_turn_trace
from ..providers.base import ProviderError


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
    trace: bool = False,
    db_path: Path | None = None,
) -> int:
    from .. import __version__
    from .onboarding import needs_onboarding, run_onboarding

    config = load_config()
    ready = startup_check(vault, config)

    if needs_onboarding(vault):
        run_onboarding(vault)

    conv_id = conversation_id or today_iso()
    _print_header(__version__, conv_id)

    if not ready:
        print(
            _c("  No provider is reachable. Configure one in config.yaml before chatting.\n", YELLOW)
        )
        # Don't hard-exit — let the user at least see the interface.

    _enable_readline()

    from ..tools.narrative_state import reset_narrative_state

    advice_history: list[dict[str, str]] = []
    advice_context_active = False
    advice_topic: str | None = None
    domain_override: str | None = None

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

        if lowered.startswith("/domain") or lowered.startswith("/arena"):
            parts = raw.split(maxsplit=1)
            if len(parts) > 1:
                domain_override = parts[1].strip().lower() or None
                print(_c(f"  Domain context set to: {domain_override}", DIM))
            else:
                domain_override = None
                print(_c("  Domain context cleared (auto-detect)", DIM))
            print()
            continue

        turn_result = _process_chat_turn(
            vault=vault,
            conversation_id=conv_id,
            text=raw,
            provider=provider,
            model=model,
            advice_history=advice_history,
            advice_context_active=advice_context_active,
            advice_topic=advice_topic,
            domain_override=domain_override,
            db_path=db_path,
        )

        response = str(turn_result.get("response") or "").strip()
        if response:
            if turn_result.get("provider_failure"):
                print()
                print(_c("Lisan: ", CYAN) + response)
                print()
            elif turn_result.get("route") == "advice":
                advice_context_active = True
                advice_topic = str(turn_result.get("topic") or advice_topic or "")
                advice_history.append({"speaker": "user", "text": str(turn_result.get("content_text") or raw)})
                advice_history.append({"speaker": "assistant", "text": response})
                append_transcript(vault=vault, conversation_id=conv_id, speaker="LISAN", text=response)
            else:
                advice_context_active = False
                if turn_result.get("route") != "advice":
                    advice_topic = None
            print()
            print(_c("Lisan: ", CYAN) + response)
            print()
        else:
            advice_context_active = False
            if turn_result.get("route") != "advice":
                advice_topic = None

        queued_jobs = turn_result.get("queued_jobs") or []
        if queued_jobs:
            job_list = ", ".join(f"{job['job_type']}:{job['job_id']}" for job in queued_jobs if isinstance(job, dict))
            print(_c(f"  queued jobs: {job_list}", DIM))
        if trace:
            trace_text = str(turn_result.get("trace_summary") or "")
            if trace_text:
                print(_c(f"  {trace_text}", DIM))


def _process_chat_turn(
    *,
    vault: Path,
    conversation_id: str,
    text: str,
    provider: str | None,
    model: str | None,
    advice_history: list[dict[str, str]],
    advice_context_active: bool,
    advice_topic: str | None,
    domain_override: str | None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    from .capture import capture_text

    classification = classify_turn(text)
    lowered = text.lower().strip()
    content_text = text
    if lowered.startswith("/remember "):
        content_text = text[len("/remember "):].strip()
    elif lowered.startswith("/forget "):
        content_text = text[len("/forget "):].strip()
    turn_id = f"turn.{time.strftime('%Y%m%d%H%M%S')}.{uuid.uuid4().hex[:8]}"
    trace, token = start_turn_trace(turn_id, text, classification.label, classification.fast_path_used)
    record_inline_step("classify_turn")
    result: dict[str, Any] = {
        "route": classification.route,
        "kind": classification.label,
        "fast_path_used": classification.fast_path_used,
        "topic": advice_topic,
        "content_text": content_text,
        "queued_jobs": [],
        "trace_summary": None,
        "response": "",
        "error": None,
    }
    try:
        if classification.fast_path_used and classification.deterministic_response:
            record_inline_step("fast_path_response")
            result["response"] = classification.deterministic_response
            result["route"] = "advice"
            return result

        if classification.route == "advice":
            policy = assess_conversation_turn(
                content_text,
                state=_load_current_state(vault, conversation_id),
                listener={},
                advice_context_active=advice_context_active,
                advice_topic=advice_topic,
                route_hint={"route": "advice"},
            )
            record_inline_step("advice_response")
            response = _run_with_thinking_indicator(
                lambda: _run_advice_response(
                    vault=vault,
                    text=content_text,
                    provider=provider,
                    model=model,
                    history=advice_history,
                    conversation_policy=policy,
                )
            )
            result["response"] = response
            result["topic"] = policy.topic
            return result

        record_inline_step("memory_capture")
        effective_policy = assess_conversation_turn(
            content_text,
            state=_load_current_state(vault, conversation_id),
            listener={},
            advice_context_active=advice_context_active,
            advice_topic=advice_topic,
            route_hint={"route": "memory"},
        ).as_dict()
        if domain_override:
            effective_policy["domain_override"] = domain_override
            effective_policy["arena_override"] = domain_override
        response_bundle = _run_with_thinking_indicator(
            lambda: capture_text(
                vault=vault,
                text=content_text,
                conversation_id=conversation_id,
                speaker="USER",
                provider=provider,
                model=model,
                conversation_policy=effective_policy,
                db_path=db_path,
            )
        )
        result["queued_jobs"] = response_bundle.get("queued_jobs") or []
        record_jobs_queued(len(result["queued_jobs"]))
        result["response"] = _extract_capture_response(response_bundle)
        result["trace_summary"] = trace.summary()
        return result
    except ProviderError as exc:
        log_error(vault, "chat.process_chat_turn.provider", exc)
        short_reason = _short_provider_reason(exc)
        error_message = f"The local model provider failed before I could answer. Provider error: {short_reason}"
        result["response"] = error_message
        result["error"] = error_message
        result["provider_failure"] = True
        result["provider_error_type"] = exc.__class__.__name__
        return result
    except Exception as exc:
        log_error(vault, "chat.process_chat_turn", exc)
        result["error"] = str(exc)
        return result
    finally:
        finalized = finalize_turn_trace(trace, db_path=db_path or sqlite_path(), vault=vault)
        result["trace_summary"] = finalized.summary()
        result["trace"] = finalized.as_dict()
        reset_current_turn_trace(token)


def _extract_capture_response(result: dict[str, Any]) -> str:
    mode = str(result.get("mode") or "skip")
    elicitor = result.get("elicitor") or {}
    response_text = str(elicitor.get("response") or "").strip()
    if not response_text and mode == "extraction":
        interlocutor = result.get("interlocutor") or {}
        response_text = str(interlocutor.get("response") or "").strip()
    return response_text


def _short_provider_reason(exc: Exception) -> str:
    message = str(exc).strip().replace("\n", " ")
    if not message:
        return exc.__class__.__name__
    if len(message) > 180:
        return message[:177] + "..."
    return message


# ── Response rendering ────────────────────────────────────────────────────────

def _render_response(result: dict[str, Any], vault: Path | None = None, conversation_id: str | None = None) -> None:
    mode         = result.get("mode", "skip")
    elicitor     = result.get("elicitor") or {}
    response_text = str(elicitor.get("response") or "").strip()

    # In extraction mode the elicitor is silent, but the interlocutor produced an acknowledgment.
    if not response_text and mode == "extraction":
        interlocutor = result.get("interlocutor") or {}
        response_text = str(interlocutor.get("response") or "").strip()

    if response_text:
        if vault and conversation_id:
            append_transcript(vault=vault, conversation_id=conversation_id, speaker="LISAN", text=response_text)
        print()
        print(_c("Lisan: ", CYAN) + response_text)
        print()
    elif mode not in ("skip",):
        # Fallback dot for extraction when interlocutor produced nothing.
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
    print(_c("  /domain [name] override retrieval domain (legacy /arena)", DIM))
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
    from .assembler import assemble_context

    vault_context = assemble_context(text, vault=vault) or None
    agent = AdviceAgent(vault=vault)
    result = agent.run(
        text,
        significance="low",
        provider=provider,
        model=model,
        provider_error_mode="raise",
        conversation_history=_format_advice_history(history),
        conversation_policy=conversation_policy.as_dict() if conversation_policy is not None else {},
        vault_context=vault_context,
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
