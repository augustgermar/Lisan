from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import textwrap
import time
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any
from .db import connect as _db_connect

from ..config import load_config
from ..paths import sqlite_path, vault_root
from .primer_index import assistant_display_name
from ..utils import today_iso
from .chat_turns import classify_turn
from .conversation_policy import assess_conversation_turn
from .log import log_error, tail_log
from .transcripts import append_transcript
from .tracing import finalize_turn_trace, record_inline_step, record_jobs_queued, reset_current_turn_trace, start_turn_trace
from ..providers.base import ProviderError
from .provider_diagnostics import ProviderDiagnosticResult, diagnose_provider
from .term import color, readline_prompt, BOLD, DIM, ITALIC, CYAN, GREEN, YELLOW, RED, BLUE, BLUE_DEEP, SKY, GREY, GREY_DIM, GREY_FAINT, CODEX, CODEX_DIM, CODEX_CMD




# ── Startup check ─────────────────────────────────────────────────────────────

def startup_check(vault: Path, config: dict[str, Any], *, defer_embedder: bool = False) -> bool:
    """Verify vault, index, and provider. Auto-fix what can be fixed. Returns True if ready.

    ``defer_embedder=True`` skips the eager embedding probe so the ONNX model
    is not loaded into memory until the first real query. The Telegram service
    uses this: an idle bot that holds ~700 MB of FastEmbed weights between
    messages is wasteful, and the retrieval layer degrades gracefully when the
    embedder is cold.
    """
    print(color("  ⣿ checking system", GREY_DIM))

    vault_ok = _check_vault(vault)
    index_ok = _check_index(vault)
    if defer_embedder:
        print(color('  · ', GREY_DIM) + color('retrieval  embedder deferred (loads on first query)', GREY))
    else:
        _check_embedder(config)
    provider_name, provider_ok, provider_diagnostic = _check_provider(config)

    if provider_ok:
        print(color('  ✓ ', GREEN) + color(f'provider  {provider_name}', GREY))
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
    # Under launchd/systemd stdout is a file, so Python block-buffers it and
    # this checklist can sit unwritten for hours (2026-07-27: a service log's
    # last line was three hours stale while the process ran fine). A startup
    # diagnostic nobody can read is not a diagnostic — flush it now.
    try:
        sys.stdout.flush()
    except Exception:
        pass
    return vault_ok and index_ok and provider_ok


def _check_embedder(config: dict[str, Any]) -> bool:
    """Say out loud whether the semantic retrieval lane is actually alive.

    ``unreachable_policy: skip`` degrades silently by design — retrieval just
    drops the vector leg and keeps working on SQL + FTS. That silence is why
    this install ran keyword-only for its entire life without anyone noticing
    (2026-07-27: the venv predated the fastembed dependency; embed jobs kept
    reporting *succeeded* while embedding nothing). A degraded lane is a fine
    fallback and a terrible secret, so it gets a line in the startup checklist
    next to vault, index, and provider.

    The probe is a real one-string embed rather than an import check, because
    only an embed proves the backend works. On the long-lived services that is
    also free: it warms the in-process model that the first real query would
    have paid for anyway. Never raises — a broken probe must not stop startup.
    """
    from ..config import embedding_settings

    try:
        settings = embedding_settings(config)
        mode = str(settings.get("mode", "auto"))
        policy = str(settings.get("unreachable_policy", "skip"))
        if mode == "hash":
            print(color('  ✓ ', GREEN) + color('retrieval  keyword + hash vectors (mode=hash, semantic off by config)', GREY))
            return True

        from ..providers.embeddings import EmbeddingProvider

        probe = EmbeddingProvider(config).embed_query("healthcheck")
        if probe.reachable:
            model = settings.get("model") or "unknown"
            print(color('  ✓ ', GREEN) + color(f'retrieval  semantic lane live  {model} ({probe.dimension}d)', GREY))
            return True

        lane = "hash vectors" if policy == "hash" else "keyword only"
        print(f"  {color('!', YELLOW)} Retrieval: semantic lane DOWN — running {lane}")
        print(f"    provider={settings.get('provider')} unreachable_policy={policy}")
        print("    fix: pip install fastembed  (then: lisan rebuild-index)")
        return False
    except Exception as exc:  # a health probe must never block startup
        print(f"  {color('!', YELLOW)} Retrieval: could not check the embedder ({exc.__class__.__name__}: {exc})")
        return False


def _check_vault(vault: Path) -> bool:
    if vault.exists():
        print(color('  ✓ ', GREEN) + color(f'vault  {vault}', GREY))
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
            conn = _db_connect(db)
            count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            conn.close()
            if count == 0:
                needs_rebuild = True
            else:
                print(color('  ✓ ', GREEN) + color(f'index  {count} record' + ('s' if count != 1 else ''), GREY))
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
    agent_name = assistant_display_name(vault)
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
            raw = input(readline_prompt("  › ", SKY, BOLD)).strip()
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
            print(color(f"  ✦ new conversation  {conv_id}", SKY))
            print()
            continue

        if lowered == "/help":
            _print_help()
            continue

        if lowered == "/status":
            startup_check(vault, config)
            continue

        if lowered == "/verbose" or lowered.startswith("/verbose "):
            arg = lowered.split(maxsplit=1)[1].strip() if " " in lowered else ""
            set_codex_verbose(arg in ("on", "1", "true", "yes") if arg else not codex_verbose())
            state = "on — full commands, output tails, reasoning" if codex_verbose() else "off — compact Codex activity"
            print(color(f"  ✦ codex verbose {state}", SKY))
            print()
            continue

        if lowered == "/id":
            print(color(f"  conversation_id  {conv_id}", GREY))
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
                print(color(f"  ● {agent_name}", BLUE, BOLD) + color("  ", DIM) + response)
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
            print(color(f"  ● {agent_name}", BLUE, BOLD) + color("  ", DIM) + response)
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
            agent_name=assistant_display_name(vault),
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
        agent_name = assistant_display_name(vault) if vault else "Lisan"
        print()
        print(color(f"  ● {agent_name}", BLUE, BOLD) + color("  ", DIM) + response_text)
        print()
    elif result.get("mode", "skip") not in ("skip",):
        # Fallback dot for extraction when interlocutor produced nothing.
        print(color("  ·", DIM))
        print()


# ── Helpers ───────────────────────────────────────────────────────────────────

_WORDMARK = (
    "  ██╗     ██╗███████╗ █████╗ ███╗   ██╗",
    "  ██║     ██║██╔════╝██╔══██╗████╗  ██║",
    "  ██║     ██║███████╗███████║██╔██╗ ██║",
    "  ██║     ██║╚════██║██╔══██║██║╚██╗██║",
    "  ███████╗██║███████║██║  ██║██║ ╚████║",
    "  ╚══════╝╚═╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═══╝",
)


def _print_header(version: str, conv_id: str, agent_name: str = "Lisan") -> None:
    print()
    for i, line in enumerate(_WORDMARK):
        # gradient: deeper blue at the top, brighter azure toward the bottom
        print(color(line, BLUE_DEEP if i < 3 else BLUE))
    print()
    print(color(f"  {agent_name}", SKY, BOLD)
          + color("  ·  ", GREY_FAINT)
          + color(f"v{version}", GREY)
          + color("  ·  ", GREY_FAINT)
          + color(conv_id, GREY))
    print()
    rule = color("  " + "─" * 46, GREY_FAINT)
    print(rule)
    for cmd, desc in (
        ("/new", "start a new conversation"),
        ("/status", "system health check"),
        ("/help", "all commands"),
        ("/quit", "exit"),
    ):
        print(color(f"  {cmd:<9}", SKY) + color(desc, GREY_DIM))
    print(rule)
    print()


def _print_help() -> None:
    print()
    print(color("  Commands", SKY, BOLD))
    for cmd, desc in (
        ("/new", "start a new conversation (clears narrative state)"),
        ("/status", "re-run system health check"),
        ("/id", "show the current conversation ID"),
        ("/logs [N]", "show last N log lines (default 20)"),
        ("/domain [name]", "override retrieval domain (legacy /arena)"),
        ("/verbose [on|off]", "toggle detailed Codex activity (or LISAN_CODEX_VERBOSE=1)"),
        ("/help", "show this message"),
        ("/quit", "exit"),
    ):
        print(color(f"  {cmd:<18}", SKY) + color(desc, GREY_DIM))
    print()
    print(color("  Prefixes", SKY, BOLD))
    for cmd, desc in (
        ("/remember", "force capture regardless of score"),
        ("/forget", "suppress capture for this turn"),
    ):
        print(color(f"  {cmd:<18}", SKY) + color(desc, GREY_DIM))
    print()
    print(color("  Advice questions are answered directly and are not stored in the vault.", GREY_DIM, ITALIC))
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


_CODEX_VERBOSE = os.environ.get("LISAN_CODEX_VERBOSE", "").strip().lower() in {"1", "true", "yes", "on"}


def codex_verbose() -> bool:
    return _CODEX_VERBOSE


def set_codex_verbose(enabled: bool) -> None:
    global _CODEX_VERBOSE
    _CODEX_VERBOSE = bool(enabled)


def _compact_count(value: int) -> str:
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


class _ProgressRenderer:
    """Claude-Code-style live narration of a turn: one dim line per pipeline
    stage, tool call, and finished model call, printed as events arrive.
    Delegated Codex activity gets its own amber gutter so it is never
    mistaken for Lisan; verbose mode adds output tails and full text."""

    _VERBOSE_OUTPUT_LINES = 8
    _FAILED_OUTPUT_LINES = 3
    _VERBOSE_TEXT_LINES = 30

    def __init__(self, agent_name: str, out=print, verbose: bool | None = None, width: int | None = None):
        self.agent_name = agent_name
        self.out = out
        self.verbose = _CODEX_VERBOSE if verbose is None else verbose
        self.width = width or shutil.get_terminal_size((100, 24)).columns
        self._lock = threading.Lock()
        self._header_shown = False
        self._last_command = ""

    def ensure_header(self) -> None:
        with self._lock:
            self._ensure_header_locked()

    def _ensure_header_locked(self) -> None:
        if not self._header_shown:
            self._header_shown = True
            self.out("")
            self.out(color(f"  ● {self.agent_name}", BLUE, BOLD) + color("  thinking…", GREY_DIM, ITALIC))

    def __call__(self, event: dict) -> None:
        if event.get("kind") == "codex":
            lines = self._format_codex(event)
        else:
            line = self._format(event)
            lines = [color(f"  ▸ {line}", DIM)] if line is not None else []
        if not lines:
            return
        with self._lock:
            self._ensure_header_locked()
            for line in lines:
                self.out(line)

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

    # ── Codex ────────────────────────────────────────────────────────────

    def _clip(self, text: str, indent: int = 8) -> str:
        room = max(20, self.width - indent)
        return text if len(text) <= room else text[: room - 1] + "…"

    def _wrap(self, text: str, max_lines: int, indent: int = 8) -> list[str]:
        room = max(20, self.width - indent)
        lines: list[str] = []
        for paragraph in text.split("\n"):
            if not paragraph.strip():
                if lines and lines[-1]:
                    lines.append("")
                continue
            lines.extend(textwrap.wrap(paragraph, room, drop_whitespace=False, replace_whitespace=False) or [""])
        while lines and not lines[-1]:
            lines.pop()
        if len(lines) > max_lines:
            hidden = len(lines) - max_lines
            lines = lines[:max_lines] + [f"… {hidden} more line(s)"]
        return lines

    def _cx_body(self, text: str, *codes: str) -> str:
        return color("  │   ", CODEX_DIM) + color(text, *codes)

    def _format_codex(self, event: dict) -> list[str]:
        name = str(event.get("event") or "")
        seconds = float(event.get("elapsed_ms") or 0) / 1000.0
        if name == "start":
            agent = str(event.get("agent") or "codex")
            model = str(event.get("model") or "")
            directory = str(event.get("working_directory") or "")
            parts = [part for part in (agent, model, directory) if part]
            return [color("  ┌ codex", CODEX, BOLD) + color("  " + "  ·  ".join(parts), CODEX_DIM)]
        if name == "heartbeat":
            detail = str(event.get("detail") or "")
            text = f"… still working · {seconds:.0f}s" + (f" · {detail}" if detail else "")
            return [color("  │ ", CODEX_DIM) + color(self._clip(text, 6), GREY_DIM, ITALIC)]
        if name == "detail":
            detail = str(event.get("detail") or "")
            return [color("  │ ", CODEX_DIM) + color(self._clip(detail, 6), GREY_DIM)] if self.verbose and detail else []
        if name == "failed":
            return [color(f"  └ ✗ Codex exited with an error after {seconds:.1f}s", RED)]
        if name == "item":
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            return self._format_codex_item(payload, seconds)
        return []

    def _format_codex_item(self, payload: dict, seconds: float) -> list[str]:
        kind = payload.get("type")
        verbose = self.verbose
        lines: list[str] = []
        if kind == "command":
            command = str(payload.get("command") or "")
            if payload.get("phase") == "started":
                self._last_command = command
                command_lines = command.split("\n")
                shown = command_lines if verbose else command_lines[:1]
                if not verbose and len(command_lines) > 1:
                    shown = [shown[0] + f"  (+{len(command_lines) - 1} lines)"]
                for index, text in enumerate(shown[:12]):
                    prefix = "$" if index == 0 else " "
                    lines.append(color("  │ ", CODEX_DIM) + color(f"{prefix} ", CODEX) + color(self._clip(text), CODEX_CMD))
                return lines
            exit_code = payload.get("exit_code")
            failed = (exit_code not in (None, 0)) or payload.get("status") in ("failed", "declined")
            tail = [str(line) for line in payload.get("output_tail") or []]
            total = int(payload.get("output_lines") or 0)
            keep = self._VERBOSE_OUTPUT_LINES if verbose else (self._FAILED_OUTPUT_LINES if failed else 0)
            if keep and tail:
                shown = tail[-keep:]
                if total > len(shown):
                    lines.append(self._cx_body(f"… {total - len(shown)} earlier line(s)", GREY_DIM, ITALIC))
                lines.extend(self._cx_body(self._clip(text), GREY) for text in shown)
            status = f"exit {exit_code}" if exit_code is not None else str(payload.get("status") or "done")
            summary = f"{'✗' if failed else '✓'} {status}" + (f" · {total} line(s)" if total else " · no output")
            if command and command != self._last_command:
                summary += f" · {self._clip(command.split(chr(10), 1)[0], 60)}"
            lines.append(self._cx_body(summary, RED if failed else GREEN))
            return lines
        if kind == "reasoning":
            text = str(payload.get("text") or "")
            if verbose:
                wrapped = self._wrap(text.replace("**", ""), self._VERBOSE_TEXT_LINES)
            else:
                headline = next((line for line in text.split("\n") if line.strip()), "")
                wrapped = [self._clip(headline.replace("**", "").strip())]
            return [
                color("  │ ", CODEX_DIM) + color("✻ " if index == 0 else "  ", CODEX) + color(text_line, GREY, ITALIC)
                for index, text_line in enumerate(wrapped)
            ]
        if kind == "message":
            text = str(payload.get("text") or "")
            structured = text.lstrip().startswith(("{", "["))
            if verbose:
                wrapped = self._wrap(text, self._VERBOSE_TEXT_LINES)
            elif structured:
                wrapped = [f"replied with structured output ({len(text)} chars)"]
            else:
                first = next((line for line in text.split("\n") if line.strip()), "")
                more = " …" if len(text.strip()) > len(first.strip()) else ""
                wrapped = [self._clip(first.strip() + more)]
            return [
                color("  │ ", CODEX_DIM) + color("» " if index == 0 else "  ", CODEX, BOLD) + color(text_line, CODEX)
                for index, text_line in enumerate(wrapped)
            ]
        if kind == "file_change":
            changes = [change for change in payload.get("changes") or [] if isinstance(change, dict)]
            failed = payload.get("status") == "failed"
            limit = len(changes) if verbose else 6
            for change in changes[:limit]:
                text = f"{change.get('kind', 'update')} {change.get('path', '?')}"
                lines.append(color("  │ ", CODEX_DIM) + color("✎ ", YELLOW) + color(self._clip(text), RED if failed else YELLOW))
            if len(changes) > limit:
                lines.append(self._cx_body(f"+{len(changes) - limit} more file(s)", GREY_DIM))
            return lines
        if kind == "tool":
            tool = str(payload.get("name") or "tool")
            if payload.get("phase") == "started":
                return [color("  │ ", CODEX_DIM) + color("⚙ ", CODEX) + color(self._clip(tool), CODEX_CMD)]
            if payload.get("status") == "failed" or payload.get("error"):
                error = str(payload.get("error") or "failed")
                return [self._cx_body(self._clip(f"✗ {tool}: {error}"), RED)]
            return [self._cx_body(f"✓ {tool}", GREEN)] if verbose else []
        if kind == "web_search":
            return [color("  │ ", CODEX_DIM) + color("⌕ ", CODEX) + color(self._clip(f"search: {payload.get('query')}"), CODEX_CMD)]
        if kind == "todo":
            items = [entry for entry in payload.get("items") or [] if isinstance(entry, dict)]
            if not items or (payload.get("phase") == "updated" and not verbose):
                return []
            done = sum(1 for entry in items if entry.get("done"))
            lines.append(color("  │ ", CODEX_DIM) + color("☰ ", CODEX) + color(f"plan · {done}/{len(items)} done", CODEX))
            if verbose:
                for entry in items:
                    mark = "☑" if entry.get("done") else "☐"
                    lines.append(self._cx_body(self._clip(f"{mark} {entry.get('text')}"), GREY_DIM if entry.get("done") else GREY))
            return lines
        if kind == "error":
            return [color("  │ ", CODEX_DIM) + color(self._clip(f"✗ {payload.get('message') or 'error'}"), RED)]
        if kind == "turn":
            phase = payload.get("phase")
            if phase == "completed":
                usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                tokens = ""
                if usage.get("input_tokens") or usage.get("output_tokens"):
                    tokens = (
                        f" · {_compact_count(int(usage.get('input_tokens') or 0))} in"
                        f" / {_compact_count(int(usage.get('output_tokens') or 0))} out"
                    )
                return [color("  └ codex", CODEX, BOLD) + color(f"  done · {seconds:.1f}s{tokens}", CODEX_DIM)]
            if phase == "failed":
                message = str(payload.get("message") or "turn failed")
                return [color("  └ ", CODEX_DIM) + color(self._clip(f"✗ Codex turn failed: {message}", 4), RED)]
            return []
        if kind == "other" and verbose:
            return [color("  │ ", CODEX_DIM) + color(f"· {payload.get('item_type')}", GREY_DIM)]
        return []


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
    print()
    print(color("  ● till next time.", BLUE, DIM))
    print()


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
        # Ask the terminal to wrap pasted multiline text in bracketed-paste
        # markers. Readline then inserts the whole paste into one editing
        # buffer instead of treating every embedded newline as Enter. Without
        # this, a pasted document becomes several independent Anakin turns.
        if sys.stdin.isatty() and sys.stdout.isatty():
            sys.stdout.write("\x1b[?2004h")
            sys.stdout.flush()
            try:
                readline.parse_and_bind("set enable-bracketed-paste on")
            except Exception:
                pass

            def _disable_bracketed_paste() -> None:
                try:
                    sys.stdout.write("\x1b[?2004l")
                    sys.stdout.flush()
                except Exception:
                    pass

            atexit.register(_disable_bracketed_paste)
    except ImportError:
        pass
