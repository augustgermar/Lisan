from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..config import load_config
from ..paths import sqlite_path, vault_root
from .primer_index import assistant_name as _assistant_name
from ..utils import today_iso
from .chat_turns import classify_turn
from .conversation_policy import assess_conversation_turn
from .log import log_error, tail_log
from .transcripts import append_transcript
from .tracing import finalize_turn_trace, record_inline_step, record_jobs_queued, reset_current_turn_trace, start_turn_trace
from ..providers.base import ProviderError
from .provider_diagnostics import ProviderDiagnosticResult, diagnose_provider
from .term import color, BOLD, DIM, CYAN, GREEN, YELLOW, RED




# ── Startup check ─────────────────────────────────────────────────────────────

def startup_check(vault: Path, config: dict[str, Any]) -> bool:
    """Verify vault, index, and provider. Auto-fix what can be fixed. Returns True if ready."""
    print(color("Checking system…", DIM))

    vault_ok = _check_vault(vault)
    index_ok = _check_index(vault)
    provider_name, provider_ok, provider_diagnostic = _check_provider(config)

    if provider_ok:
        print(f"  {color('✓', GREEN)} Provider: {provider_name}")
    else:
        print(f"  {color('!', YELLOW)} Provider: {provider_name} not reachable")
        if provider_diagnostic is not None:
            diagnostic = provider_diagnostic
            for error_text in diagnostic.errors:
                print(f"    {error_text}")
            for fix in diagnostic.suggested_fixes:
                print(f"    fix: {fix}")
        else:
            print("    Set CODEX_BIN or add an API key, then update routing in config.json")

    print()
    return vault_ok and index_ok and provider_ok


def _check_vault(vault: Path) -> bool:
    if vault.exists():
        print(f"  {color('✓', GREEN)} Vault: {vault}")
        return True
    print(f"  {color('!', YELLOW)} Vault not found — initializing {vault}")
    try:
        from ..paths import ensure_repo_layout
        ensure_repo_layout()
        print(f"  {color('✓', GREEN)} Vault initialized")
        return True
    except Exception as exc:
        print(f"  {color('✗', RED)} Vault init failed: {exc}")
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
                print(f"  {color('✓', GREEN)} Index: {count} record{'s' if count != 1 else ''}")
                return True
        except Exception:
            needs_rebuild = True

    print(f"  {color('!', YELLOW)} Index missing or empty — rebuilding…")
    try:
        from .rebuild_index import rebuild_index
        counts = rebuild_index(vault)
        print(f"  {color('✓', GREEN)} Index built: {counts['files']} records")
        return True
    except Exception as exc:
        print(f"  {color('✗', RED)} Index rebuild failed: {exc}")
        return False


def _check_provider(config: dict[str, Any]) -> tuple[str, bool, ProviderDiagnosticResult | None]:
    routing = config.get("routing", {})
    name = str(routing.get("elicitor", {}).get("medium", "local"))

    if name == "codex":
        binary_env = config.get("providers", {}).get("codex", {}).get("binary_env", "CODEX_BIN")
        binary = os.environ.get(binary_env) or "codex"
        return name, bool(shutil.which(binary)), None

    if name in ("openai", "anthropic", "google", "openrouter"):
        key_env = str(config.get("providers", {}).get(name, {}).get("api_key_env") or "")
        return name, bool(key_env and os.environ.get(key_env)), None

    if name == "local":
        diagnostic = _diagnose_local_provider(config)
        return name, diagnostic.status == "ok", diagnostic

    return name, True, None  # local / unknown — assume reachable


def _diagnose_local_provider(config: dict[str, Any]):
    diag_config = deepcopy(config)
    providers = dict(diag_config.get("providers", {}))
    local_cfg = dict(providers.get("local", {}))
    local_cfg["timeout_seconds"] = min(int(local_cfg.get("timeout_seconds", 120)), 5)
    providers["local"] = local_cfg
    diag_config["providers"] = providers
    return diagnose_provider(provider="local", config=diag_config)


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
    _refresh_capabilities_primer(vault)

    if needs_onboarding(vault):
        run_onboarding(vault)

    conv_id = conversation_id or today_iso()
    agent_name = _assistant_name(vault)
    _print_header(__version__, conv_id, agent_name)

    if not ready:
        print(
            color("  No provider is reachable. Configure one in config.json before chatting.\n", YELLOW)
        )
        # Don't hard-exit — let the user at least see the interface.

    _enable_readline()

    from .narrative_state import reset_narrative_state

    advice_history: list[dict[str, str]] = []
    advice_context_active = False
    advice_topic: str | None = None
    domain_override: str | None = None

    while True:
        try:
            raw = input(color("You: ", BOLD)).strip()
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
            print(color(f"  New conversation: {conv_id}", DIM))
            print()
            continue

        if lowered == "/help":
            _print_help()
            continue

        if lowered == "/status":
            startup_check(vault, config)
            continue

        if lowered == "/id":
            print(color(f"  conversation_id: {conv_id}", DIM))
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
            print(color(tail_log(vault, lines=n), DIM))
            print()
            continue

        if lowered.startswith("/domain") or lowered.startswith("/arena"):
            parts = raw.split(maxsplit=1)
            if len(parts) > 1:
                domain_override = parts[1].strip().lower() or None
                print(color(f"  Domain context set to: {domain_override}", DIM))
            else:
                domain_override = None
                print(color("  Domain context cleared (auto-detect)", DIM))
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
                print(color(f"{agent_name}: ", CYAN) + response)
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
            print(color(f"{agent_name}: ", CYAN) + response)
            print()
        else:
            advice_context_active = False
            if turn_result.get("route") != "advice":
                advice_topic = None

        _print_background_summary(turn_result)
        if trace:
            trace_text = str(turn_result.get("trace_summary") or "")
            if trace_text:
                print(color(f"  trace: {trace_text}", DIM))


def _process_chat_turn(
    *,
    vault: Path,
    conversation_id: str,
    text: str,
    provider: str | None,
    model: str | None,
    advice_history: list[dict[str, str]] | None = None,
    advice_context_active: bool = False,
    advice_topic: str | None = None,
    domain_override: str | None = None,
    db_path: Path | None = None,
    approval_fn=None,
) -> dict[str, Any]:

    advice_history = advice_history if advice_history is not None else []

    classification = classify_turn(text, vault=vault, conversation_id=conversation_id)
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
            # Even a canned exchange is part of the conversation: without the
            # transcript, later turns can't see it and the thread breaks.
            try:
                append_transcript(vault=vault, conversation_id=conversation_id, speaker="USER", text=text)
                append_transcript(
                    vault=vault, conversation_id=conversation_id, speaker="LISAN",
                    text=classification.deterministic_response,
                )
            except Exception:
                pass
            return result

        # Every non-trivial turn goes to the one conversational agent: full
        # rolling history, retrieved context, capabilities, every tool. Memory
        # capture observes the finished exchange in the background — it never
        # again stands between the user and the reply.
        from .conversation import run_conversation_turn

        turn_result = _run_with_thinking_indicator(
            lambda: run_conversation_turn(
                vault=vault,
                text=content_text,
                conversation_id=conversation_id,
                provider=provider,
                model=model,
                db_path=db_path,
                approval_fn=approval_fn,
            ),
            agent_name=_assistant_name(vault),
        )
        result["route"] = "conversation"
        result["response"] = turn_result.get("response") or ""
        result["queued_jobs"] = turn_result.get("queued_jobs") or []
        result["tool_calls"] = turn_result.get("tool_calls") or []
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
    elicitor = result.get("elicitor") or {}
    response_text = str(elicitor.get("response") or "").strip()
    if not response_text:
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


def _print_background_summary(result: dict[str, Any]) -> None:
    trace = result.get("trace") or {}
    route = str(result.get("route") or "").strip() or "unknown"
    kind = str(result.get("kind") or "").strip() or "unknown"
    queued_jobs = [job for job in (result.get("queued_jobs") or []) if isinstance(job, dict)]
    inline_steps = trace.get("inline_steps") or []
    llm_calls = trace.get("llm_calls") or []
    elapsed_ms = trace.get("elapsed_ms") or 0
    retrieval_count = trace.get("retrieval_record_count") if trace.get("retrieval_used") else 0
    graph_count = trace.get("graph_expanded_count") if trace.get("retrieval_used") else 0
    jobs_queued = trace.get("jobs_queued") or 0

    print(color("  background:", DIM))
    print(color(f"    route: {route} | kind: {kind}", DIM))

    if inline_steps:
        print(color("    stages:", DIM))
        for step in inline_steps:
            print(color(f"      • {_humanize_trace_step(str(step))}", DIM))
    else:
        print(color("    stages: none", DIM))

    if llm_calls:
        print(color("    llm calls:", DIM))
        for call in llm_calls:
            call_name = str(call.get("call_name") or "llm")
            provider = str(call.get("provider") or "")
            model = str(call.get("model") or "")
            elapsed = call.get("elapsed_ms") or 0
            prompt_tokens = call.get("prompt_token_estimate") or 0
            output_tokens = call.get("output_token_estimate") or 0
            parts = [call_name]
            if provider:
                parts.append(provider)
            if model:
                parts.append(model)
            parts.append(f"{elapsed}ms")
            parts.append(f"prompt~{prompt_tokens}")
            parts.append(f"output~{output_tokens}")
            print(color(f"      • {' | '.join(parts)}", DIM))

    if queued_jobs:
        job_list = ", ".join(f"{job['job_type']}:{job['job_id']}" for job in queued_jobs)
        print(color(f"    queued jobs: {job_list}", DIM))
    else:
        print(color("    queued jobs: none", DIM))

    print(
        color(
            f"    trace: fast_path={str(bool(trace.get('fast_path_used'))).lower()} | "
            f"retrieval={retrieval_count} | graph={graph_count} | jobs={jobs_queued} | elapsed={elapsed_ms}ms",
            DIM,
        )
    )
    print()


_TRACE_STEP_LABELS: dict[str, str] = {
    "classify_turn": "classify the turn",
    "fast_path_response": "answer from the fast path",
    "advice_response": "draft a direct advice reply",
    "memory_capture": "capture the turn into memory",
    "memory_pipeline.start": "start the memory pipeline",
    "memory_pipeline.transcript": "append the transcript",
    "memory_pipeline.listener": "run the listener",
    "memory_pipeline.assembler": "assemble retrieval context",
    "memory_pipeline.interlocutor": "run the interlocutor",
    "memory_pipeline.writer": "run the writer",
    "memory_pipeline.skeptic": "run the skeptic",
    "memory_pipeline.writer.artifacts": "expand writer artifacts",
    "memory_pipeline.fanout": "fan out records into the vault",
    "memory_pipeline.fanout.skeptic_blocked": "hold fan-out because the skeptic blocked it",
    "memory_pipeline.elicitor": "enter the elicitor loop",
}


def _humanize_trace_step(step: str) -> str:
    if step in _TRACE_STEP_LABELS:
        return _TRACE_STEP_LABELS[step]
    step = step.replace(".", " ")
    step = step.replace("_", " ")
    return step.strip()


# ── Response rendering ────────────────────────────────────────────────────────

def _render_response(result: dict[str, Any], vault: Path | None = None, conversation_id: str | None = None) -> None:
    elicitor     = result.get("elicitor") or {}
    response_text = str(elicitor.get("response") or "").strip()

    if not response_text:
        interlocutor = result.get("interlocutor") or {}
        response_text = str(interlocutor.get("response") or "").strip()

    if response_text:
        if vault and conversation_id:
            append_transcript(vault=vault, conversation_id=conversation_id, speaker="LISAN", text=response_text)
        agent_name = _assistant_name(vault) if vault else "Lisan"
        print()
        print(color(f"{agent_name}: ", CYAN) + response_text)
        print()
    elif result.get("mode", "skip") not in ("skip",):
        # Fallback dot for extraction when interlocutor produced nothing.
        print(color("  ·", DIM))
        print()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print_header(version: str, conv_id: str, agent_name: str = "Lisan") -> None:
    bar = color("─" * 44, DIM)
    print(bar)
    print(color(f"  {agent_name}  ·  v{version}  ·  {conv_id}", BOLD))
    print(bar)
    print(color("  /new      start a new conversation", DIM))
    print(color("  /status   system health check", DIM))
    print(color("  /help     all commands", DIM))
    print(color("  /logs     show recent log entries (/logs N for last N lines)", DIM))
    print(color("  /quit     exit", DIM))
    print(bar)
    print()


def _print_help() -> None:
    print()
    print(color("  Commands:", BOLD))
    print(color("  /new        start a new conversation (clears narrative state)", DIM))
    print(color("  /status     re-run system health check", DIM))
    print(color("  /id         show the current conversation ID", DIM))
    print(color("  /logs [N]   show last N log lines (default 20)", DIM))
    print(color("  /domain [name] override retrieval domain (legacy /arena)", DIM))
    print(color("  /help       show this message", DIM))
    print(color("  /quit       exit", DIM))
    print()
    print(color("  Prefixes:", BOLD))
    print(color("  /remember   force capture regardless of score", DIM))
    print(color("  /forget     suppress capture for this turn", DIM))
    print()
    print(color("  Advice questions are answered directly and are not stored in the vault.", DIM))
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
        capabilities=_capability_index_safe(),
        self_state=_self_state_safe(vault),
    )
    return str(result.text).strip()


# Pipeline step names → what the user sees in the live activity feed.
# None = internal bookkeeping, not worth a line.
_STEP_LABELS: dict[str, str | None] = {
    "memory_pipeline.start": None,
    "memory_pipeline.transcript": None,
    "memory_capture": None,
    "advice_response": None,
    "memory_pipeline.listener": "listening — classifying this turn",
    "memory_pipeline.elicitor": "drafting clarifying questions",
    "memory_pipeline.assembler": "recalling related memories",
    "memory_pipeline.interlocutor": "composing response",
    "memory_pipeline.writer": "writer — extracting what to remember",
    "memory_pipeline.skeptic": "skeptic — checking the records",
    "memory_pipeline.writer.artifacts": "writer — artifact pass",
    "memory_pipeline.fanout": "writing records to the vault",
    "memory_pipeline.fanout.skeptic_blocked": "skeptic blocked a record",
}


class _ProgressRenderer:
    """Claude-Code-style live narration of a turn: one dim line per pipeline
    stage, tool call, and finished model call, printed as events arrive."""

    def __init__(self, agent_name: str, out=print):
        self.agent_name = agent_name
        self.out = out
        self._lock = threading.Lock()
        self._header_shown = False

    def ensure_header(self) -> None:
        with self._lock:
            self._ensure_header_locked()

    def _ensure_header_locked(self) -> None:
        if not self._header_shown:
            self._header_shown = True
            self.out("")
            self.out(color(f"{self.agent_name}: ", CYAN) + color("thinking…", DIM))

    def __call__(self, event: dict) -> None:
        line = self._format(event)
        if line is None:
            return
        with self._lock:
            self._ensure_header_locked()
            self.out(color(f"  ▸ {line}", DIM))

    def _format(self, event: dict) -> str | None:
        kind = event.get("kind")
        if kind == "step":
            step = str(event.get("step") or "")
            if step in _STEP_LABELS:
                return _STEP_LABELS[step]
            return step
        if kind == "tool":
            preview = str(event.get("args_preview") or "")
            return f"tool: {event.get('tool')} {preview}".rstrip()
        if kind == "llm_call":
            model = str(event.get("model") or "")
            if model in ("None", "null"):
                model = ""
            backend = f"{event.get('provider')}{'/' + model if model else ''}"
            seconds = float(event.get("elapsed_ms") or 0) / 1000.0
            if event.get("success"):
                return f"{event.get('call_name')} done ({backend}, {seconds:.1f}s)"
            error_type = str(event.get("error_type") or "error")
            return f"✗ {event.get('call_name')} failed ({backend}, {seconds:.1f}s, {error_type})"
        if kind == "retrieval":
            records = int(event.get("records") or 0)
            graph = int(event.get("graph") or 0)
            graph_note = f" (+{graph} via graph)" if graph else ""
            return f"recalled {records} record(s){graph_note}"
        if kind == "jobs_queued":
            return f"queued {event.get('count')} background job(s)"
        return None


def _refresh_capabilities_primer(vault: Path) -> None:
    """Keep the generated Layer-2 self-model current with the installed code."""
    try:
        from .self_model import ensure_capabilities_primer

        ensure_capabilities_primer(vault)
    except Exception:
        pass


def _self_state_safe(vault: Path) -> str | None:
    """Live operational snapshot for the advice route, which has no tools —
    state questions must be answerable from injected data, never guessed."""
    try:
        from .self_model import render_self_state, snapshot_self_state

        return render_self_state(snapshot_self_state(vault=vault))
    except Exception:
        return None


def _capability_index_safe() -> str | None:
    try:
        from .self_model import cached_capability_index

        return cached_capability_index()
    except Exception:
        return None


def _run_with_thinking_indicator(callable_obj, agent_name: str = "Lisan"):
    from .tracing import reset_progress_listener, set_progress_listener

    done = threading.Event()
    started = time.time()
    renderer = _ProgressRenderer(agent_name)

    def _show_waiting() -> None:
        # If nothing has narrated within 0.7s, show the header so the user
        # knows work started; events add their own lines under it.
        if not done.wait(0.7):
            renderer.ensure_header()

    watcher = threading.Thread(target=_show_waiting, daemon=True)
    watcher.start()
    token = set_progress_listener(renderer)
    try:
        return callable_obj()
    finally:
        reset_progress_listener(token)
        done.set()
        elapsed_ms = int((time.time() - started) * 1000)
        if elapsed_ms >= 700:
            print(color(f"  [took {elapsed_ms / 1000:.1f}s]", DIM))


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
    print(color("  Goodbye.", DIM))


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
