from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..paths import sqlite_path, vault_root
from ..utils import approx_token_count


_CURRENT_TRACE: ContextVar["TurnTrace | None"] = ContextVar("lisan_current_turn_trace", default=None)


@dataclass(slots=True)
class TurnTrace:
    turn_id: str
    user_text: str
    turn_classification: str
    fast_path_used: bool
    created_at: str
    started_at_monotonic: float = field(default_factory=time.monotonic)
    finished_at: str | None = None
    elapsed_ms: int | None = None
    retrieval_used: bool = False
    retrieval_record_count: int = 0
    graph_expanded_count: int = 0
    jobs_queued: int = 0
    inline_steps: list[str] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    trace_path: str | None = None

    def add_step(self, step: str) -> None:
        if step and step not in self.inline_steps:
            self.inline_steps.append(step)

    def add_llm_call(
        self,
        *,
        call_name: str,
        provider: str,
        model: str | None,
        prompt: str,
        output: str | None,
        elapsed_ms: int,
        success: bool,
        error: str | None = None,
        error_type: str | None = None,
    ) -> None:
        self.llm_calls.append(
            {
                "call_name": call_name,
                "provider": provider,
                "model": model or "",
                "prompt_token_estimate": approx_token_count(prompt),
                "output_token_estimate": approx_token_count(output or "") if output else 0,
                "elapsed_ms": elapsed_ms,
                "success": bool(success),
                "error": error or "",
                "error_type": error_type or "",
            }
        )

    def mark_retrieval(self, record_count: int, graph_count: int) -> None:
        self.retrieval_used = True
        self.retrieval_record_count = max(self.retrieval_record_count, int(record_count))
        self.graph_expanded_count = max(self.graph_expanded_count, int(graph_count))

    def mark_jobs_queued(self, count: int) -> None:
        self.jobs_queued = max(self.jobs_queued, int(count))

    def finish(self) -> None:
        self.finished_at = _iso_now()
        self.elapsed_ms = max(0, int((time.monotonic() - self.started_at_monotonic) * 1000))

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "user_text": self.user_text,
            "turn_classification": self.turn_classification,
            "fast_path_used": self.fast_path_used,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "elapsed_ms": self.elapsed_ms,
            "retrieval_used": self.retrieval_used,
            "retrieval_record_count": self.retrieval_record_count,
            "graph_expanded_count": self.graph_expanded_count,
            "jobs_queued": self.jobs_queued,
            "inline_steps": list(self.inline_steps),
            "llm_calls": list(self.llm_calls),
            "trace_path": self.trace_path,
        }

    def summary(self) -> str:
        return (
            f"trace: fast_path={str(self.fast_path_used).lower()}, "
            f"llm_calls={len(self.llm_calls)}, "
            f"retrieval={self.retrieval_record_count if self.retrieval_used else 0}, "
            f"jobs={self.jobs_queued}, "
            f"elapsed={self.elapsed_ms or 0}ms"
        )


def start_turn_trace(turn_id: str, user_text: str, turn_classification: str, fast_path_used: bool) -> tuple[TurnTrace, Token]:
    trace = TurnTrace(
        turn_id=turn_id,
        user_text=user_text,
        turn_classification=turn_classification,
        fast_path_used=fast_path_used,
        created_at=_iso_now(),
    )
    token = _CURRENT_TRACE.set(trace)
    return trace, token


def get_current_turn_trace() -> TurnTrace | None:
    return _CURRENT_TRACE.get()


def reset_current_turn_trace(token: Token | None) -> None:
    if token is not None:
        _CURRENT_TRACE.reset(token)


def record_inline_step(step: str) -> None:
    trace = get_current_turn_trace()
    if trace is not None:
        trace.add_step(step)


def record_llm_call(
    *,
    call_name: str,
    provider: str,
    model: str | None,
    prompt: str,
    output: str | None,
    elapsed_ms: int,
    success: bool,
    error: str | None = None,
    error_type: str | None = None,
) -> None:
    trace = get_current_turn_trace()
    if trace is not None:
        trace.add_llm_call(
            call_name=call_name,
            provider=provider,
            model=model,
            prompt=prompt,
            output=output,
            elapsed_ms=elapsed_ms,
            success=success,
            error=error,
            error_type=error_type,
        )


def record_retrieval_result(record_count: int, graph_count: int) -> None:
    trace = get_current_turn_trace()
    if trace is not None:
        trace.mark_retrieval(record_count, graph_count)


def record_jobs_queued(count: int) -> None:
    trace = get_current_turn_trace()
    if trace is not None:
        trace.mark_jobs_queued(count)


def finalize_turn_trace(
    trace: TurnTrace,
    *,
    db_path: Path | None = None,
    vault: Path | None = None,
) -> TurnTrace:
    trace.finish()
    _persist_trace(trace, db_path=db_path, vault=vault)
    return trace


def load_turn_trace(turn_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = sqlite3.connect(db_path or sqlite_path())
    conn.row_factory = sqlite3.Row
    try:
        ensure_turn_traces_table(conn)
        row = conn.execute("SELECT * FROM turn_traces WHERE turn_id = ?", (turn_id,)).fetchone()
        if row is None:
            return None
        return _row_to_trace_dict(row)
    finally:
        conn.close()


def list_recent_turn_traces(limit: int = 20, db_path: Path | None = None) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path or sqlite_path())
    conn.row_factory = sqlite3.Row
    try:
        ensure_turn_traces_table(conn)
        rows = conn.execute(
            "SELECT * FROM turn_traces ORDER BY created_at DESC, turn_id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [_row_to_trace_dict(row) for row in rows]
    finally:
        conn.close()


def format_recent_turn_traces(traces: list[dict[str, Any]]) -> str:
    if not traces:
        return "No traces found."
    lines = ["turn_id | created_at | class | fast | llm | retrieval | jobs | elapsed"]
    for trace in traces:
        lines.append(
            " | ".join(
                [
                    str(trace.get("turn_id") or ""),
                    str(trace.get("created_at") or ""),
                    str(trace.get("turn_classification") or ""),
                    str(bool(trace.get("fast_path_used"))).lower(),
                    str(len(trace.get("llm_calls") or [])),
                    str(trace.get("retrieval_record_count") if trace.get("retrieval_used") else 0),
                    str(trace.get("jobs_queued") or 0),
                    f"{trace.get('elapsed_ms') or 0}ms",
                ]
            )
        )
    return "\n".join(lines)


def format_turn_trace(trace: dict[str, Any]) -> str:
    if not trace:
        return "Trace not found."
    lines = [
        f"turn_id: {trace.get('turn_id')}",
        f"created_at: {trace.get('created_at')}",
        f"finished_at: {trace.get('finished_at')}",
        f"elapsed_ms: {trace.get('elapsed_ms')}",
        f"user_text: {trace.get('user_text')}",
        f"turn_classification: {trace.get('turn_classification')}",
        f"fast_path_used: {str(bool(trace.get('fast_path_used'))).lower()}",
        f"retrieval_used: {str(bool(trace.get('retrieval_used'))).lower()}",
        f"retrieval_record_count: {trace.get('retrieval_record_count')}",
        f"graph_expanded_count: {trace.get('graph_expanded_count')}",
        f"jobs_queued: {trace.get('jobs_queued')}",
        "inline_steps:",
    ]
    inline_steps = trace.get("inline_steps") or []
    if inline_steps:
        lines.extend(f"- {step}" for step in inline_steps)
    else:
        lines.append("- none")
    lines.append("llm_calls:")
    llm_calls = trace.get("llm_calls") or []
    if llm_calls:
        for call in llm_calls:
            lines.append(
                "- {call_name} | provider={provider} | model={model} | prompt_tokens={prompt_token_estimate} | "
                "output_tokens={output_token_estimate} | elapsed_ms={elapsed_ms} | success={success} | error={error}".format(
                    call_name=call.get("call_name"),
                    provider=call.get("provider"),
                    model=call.get("model") or "",
                    prompt_token_estimate=call.get("prompt_token_estimate"),
                    output_token_estimate=call.get("output_token_estimate"),
                    elapsed_ms=call.get("elapsed_ms"),
                    success=str(bool(call.get("success"))).lower(),
                    error=call.get("error") or "",
                )
            )
    else:
        lines.append("- none")
    trace_path = trace.get("trace_path")
    if trace_path:
        lines.append(f"trace_path: {trace_path}")
    return "\n".join(lines)


def ensure_turn_traces_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS turn_traces (
            turn_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            elapsed_ms INTEGER,
            user_text TEXT NOT NULL,
            turn_classification TEXT NOT NULL,
            fast_path_used INTEGER NOT NULL,
            retrieval_used INTEGER NOT NULL,
            retrieval_record_count INTEGER NOT NULL,
            graph_expanded_count INTEGER NOT NULL,
            jobs_queued INTEGER NOT NULL,
            inline_steps_json TEXT NOT NULL,
            llm_calls_json TEXT NOT NULL,
            trace_path TEXT
        )
        """
    )


def _persist_trace(trace: TurnTrace, db_path: Path | None = None, vault: Path | None = None) -> None:
    db = db_path or sqlite_path()
    conn = sqlite3.connect(db)
    try:
        ensure_turn_traces_table(conn)
        trace_dir = (vault or vault_root()) / "logs" / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_path = trace_dir / f"{trace.turn_id}.json"
        trace.trace_path = str(trace_path)
        payload = trace.as_dict()
        trace_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        conn.execute(
            """
            INSERT OR REPLACE INTO turn_traces (
                turn_id, created_at, finished_at, elapsed_ms, user_text, turn_classification,
                fast_path_used, retrieval_used, retrieval_record_count, graph_expanded_count,
                jobs_queued, inline_steps_json, llm_calls_json, trace_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace.turn_id,
                trace.created_at,
                trace.finished_at,
                trace.elapsed_ms,
                trace.user_text,
                trace.turn_classification,
                int(trace.fast_path_used),
                int(trace.retrieval_used),
                int(trace.retrieval_record_count),
                int(trace.graph_expanded_count),
                int(trace.jobs_queued),
                json.dumps(trace.inline_steps),
                json.dumps(trace.llm_calls),
                trace.trace_path,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_trace_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "turn_id": row["turn_id"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "elapsed_ms": row["elapsed_ms"],
        "user_text": row["user_text"],
        "turn_classification": row["turn_classification"],
        "fast_path_used": bool(row["fast_path_used"]),
        "retrieval_used": bool(row["retrieval_used"]),
        "retrieval_record_count": int(row["retrieval_record_count"] or 0),
        "graph_expanded_count": int(row["graph_expanded_count"] or 0),
        "jobs_queued": int(row["jobs_queued"] or 0),
        "inline_steps": _json_loads(row["inline_steps_json"]),
        "llm_calls": _json_loads(row["llm_calls_json"]),
        "trace_path": row["trace_path"],
    }


def _json_loads(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@contextmanager
def turn_trace_scope(turn_id: str, user_text: str, turn_classification: str, fast_path_used: bool) -> Iterator[TurnTrace]:
    trace, token = start_turn_trace(turn_id, user_text, turn_classification, fast_path_used)
    try:
        yield trace
    finally:
        reset_current_turn_trace(token)
