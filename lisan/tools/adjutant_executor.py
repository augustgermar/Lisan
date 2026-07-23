"""The Adjutant executor: local task kinds — run_script, draft, collect.

Deterministic wherever possible: script resolution, allowlists, timeouts,
and collection are pure code. The LLM appears exactly once — inside a
draft task's generation call — and arrives as an injected callable so
tests (and a missing provider) degrade to a clean failure, never a hang.

Sandboxing v1 (pragmatic, per spec): subprocess with a per-task scratch
cwd, wall-clock timeout from intent, full stdout/stderr capture. Network
denial for run_script uses macOS sandbox-exec when available; when it is
not, the result says so out loud — honesty over pretend isolation.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..frontmatter import dump_markdown, load_markdown, write_markdown
from ..utils import slugify

CompleteFn = Callable[[str], str]

# Deny-network profile for sandbox-exec (macOS). Everything else allowed:
# v1 is a tripwire against accidental egress, not a jail.
_SANDBOX_PROFILE = "(version 1) (allow default) (deny network*)"

_COLLECT_MATCH_CAP = 200


@dataclass(slots=True)
class ExecutionResult:
    task_id: str
    kind: str
    ok: bool
    actions: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_seconds: float = 0.0
    confidence: str = "medium"


def _script_dir_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    entries = []
    for item in (config.get("adjutant") or {}).get("script_dirs", []) or []:
        if isinstance(item, str):
            entries.append({"path": item, "network_ok": False})
        elif isinstance(item, dict) and item.get("path"):
            entries.append({"path": str(item["path"]), "network_ok": bool(item.get("network_ok", False))})
    return entries


def resolve_script(name: str, config: dict[str, Any]) -> tuple[Path, bool] | None:
    """Locate ``name`` under the allowlist. Returns (path, network_ok) or
    None. Containment is checked on resolved paths — a payload cannot
    traverse out of an allowlisted directory."""
    candidate_name = Path(name)
    if candidate_name.is_absolute():
        return None
    for entry in _script_dir_entries(config):
        base = Path(entry["path"]).expanduser().resolve()
        candidate = (base / candidate_name).resolve()
        if base not in candidate.parents:
            continue
        if candidate.is_file():
            return candidate, entry["network_ok"]
    return None


def execute_run_script(
    task_id: str,
    payload: dict[str, Any],
    *,
    config: dict[str, Any],
    timeout_seconds: int,
    scratch_root: Path | None = None,
) -> ExecutionResult:
    result = ExecutionResult(task_id=task_id, kind="run_script", ok=False)
    name = str(payload.get("script", "")).strip()
    if not name:
        result.errors.append("payload carries no script name")
        return result
    args = payload.get("args", []) or []
    if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args):
        result.errors.append("script args must be a list of scalars (from the task record, never generation)")
        return result
    resolved = resolve_script(name, config)
    if resolved is None:
        result.errors.append(
            f"script {name!r} is not under any allowlisted adjutant.script_dirs directory; refusing to run"
        )
        return result
    script, network_ok = resolved

    scratch = Path(tempfile.mkdtemp(prefix=f"adjutant-{slugify(task_id)}-", dir=scratch_root))
    command: list[str] = [str(script), *[str(a) for a in args]]
    if not network_ok:
        sandbox = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
        if sandbox:
            command = [sandbox, "-p", _SANDBOX_PROFILE, *command]
            result.actions.append("network denied via sandbox-exec")
        else:
            result.actions.append("NOTE: network isolation unavailable on this host; script ran without it")

    start = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=scratch,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        result.stdout = proc.stdout
        result.stderr = proc.stderr
        result.exit_code = proc.returncode
        result.ok = proc.returncode == 0
        result.actions.append(f"ran {script.name} with {len(args)} arg(s), exit {proc.returncode}")
        if proc.returncode != 0:
            result.errors.append(f"script exited {proc.returncode}")
    except subprocess.TimeoutExpired:
        result.errors.append(f"script exceeded max_task_wall_seconds={timeout_seconds}; killed")
    except OSError as exc:
        result.errors.append(f"script failed to start: {exc}")
    result.duration_seconds = round(time.time() - start, 3)
    produced = sorted(p for p in scratch.rglob("*") if p.is_file())
    result.artifacts = [str(p) for p in produced]
    result.confidence = "high" if result.ok else "low"
    return result


def execute_collect(
    task_id: str,
    payload: dict[str, Any],
    *,
    config: dict[str, Any],
) -> ExecutionResult:
    result = ExecutionResult(task_id=task_id, kind="collect", ok=False)
    allowlist = [Path(p).expanduser().resolve() for p in (config.get("adjutant") or {}).get("collect_paths", []) or []]
    if not allowlist:
        result.errors.append("adjutant.collect_paths is empty; nothing is collectable")
        return result
    requested = payload.get("paths") or []
    roots: list[Path] = []
    if requested:
        for raw in requested:
            candidate = Path(str(raw)).expanduser().resolve()
            if any(base == candidate or base in candidate.parents for base in allowlist):
                roots.append(candidate)
            else:
                result.errors.append(f"path {raw!r} is outside adjutant.collect_paths; skipped")
    else:
        roots = list(allowlist)

    pattern = str(payload.get("pattern", "*")) or "*"
    since = str(payload.get("modified_since", "")) or None
    since_ts = None
    if since:
        try:
            since_ts = time.mktime(date.fromisoformat(since).timetuple())
        except ValueError:
            result.errors.append(f"invalid modified_since {since!r}; ignoring")
    capped = False
    for root in roots:
        if not root.exists():
            result.errors.append(f"collect root missing: {root}")
            continue
        for path in sorted(root.rglob(pattern)):
            if not path.is_file():
                continue
            stat = path.stat()
            if since_ts is not None and stat.st_mtime < since_ts:
                continue
            result.findings.append(
                {
                    "path": str(path),
                    "size_bytes": stat.st_size,
                    "modified": time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime)),
                }
            )
            if len(result.findings) >= _COLLECT_MATCH_CAP:
                capped = True
                break
        if capped:
            break
    result.actions.append(f"scanned {len(roots)} root(s) for {pattern!r}: {len(result.findings)} match(es)")
    if capped:
        result.actions.append(f"NOTE: capped at {_COLLECT_MATCH_CAP} matches; narrow the criteria for the rest")
    result.ok = True
    result.confidence = "high"
    return result


def execute_draft(
    task_id: str,
    payload: dict[str, Any],
    *,
    vault: Path,
    complete: CompleteFn | None,
    context: str = "",
) -> ExecutionResult:
    result = ExecutionResult(task_id=task_id, kind="draft", ok=False)
    title = str(payload.get("title", "")).strip() or f"Adjutant draft for {task_id}"
    instructions = str(payload.get("instructions", "")).strip()
    if not instructions:
        result.errors.append("payload carries no instructions; a draft task must say what to draft")
        return result
    if complete is None:
        result.errors.append("no generation provider available; draft tasks require one")
        return result
    from ..prompts import load_prompt

    template = load_prompt("adjutant_draft_v1")
    prompt = (
        template.replace("{{title}}", title)
        .replace("{{instructions}}", instructions)
        .replace("{{context}}", context or "(no assembled context)")
    )
    try:
        text = complete(prompt)
    except Exception as exc:
        result.errors.append(f"generation failed: {exc}")
        return result
    if not str(text).strip():
        result.errors.append("generation returned empty text")
        return result

    today = date.today().isoformat()
    out = vault / "drafts" / f"{today}-adjutant-{slugify(title)}.md"
    counter = 1
    while out.exists():
        out = vault / "drafts" / f"{today}-adjutant-{slugify(title)}-{counter}.md"
        counter += 1
    frontmatter = {
        "id": f"draft.adjutant.{today}-{slugify(title)}",
        "type": "report",
        "created": today,
        "updated": today,
        "status": "active",
        "summary": title,
        "source": "adjutant",
        "source_task": task_id,
    }
    out.write_text(dump_markdown(frontmatter, str(text).strip()), encoding="utf-8")
    result.artifacts.append(str(out))
    result.actions.append(f"drafted {out.name}")
    result.ok = True
    return result


def execute_task(
    task_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    vault: Path,
    config: dict[str, Any],
    timeout_seconds: int,
    complete: CompleteFn | None = None,
    context: str = "",
    scratch_root: Path | None = None,
) -> ExecutionResult:
    if kind == "run_script":
        return execute_run_script(task_id, payload, config=config, timeout_seconds=timeout_seconds, scratch_root=scratch_root)
    if kind == "collect":
        return execute_collect(task_id, payload, config=config)
    if kind == "draft":
        return execute_draft(task_id, payload, vault=vault, complete=complete, context=context)
    result = ExecutionResult(task_id=task_id, kind=kind, ok=False)
    result.errors.append(f"task kind {kind!r} is not executable in this step (research/notify land in step 5)")
    return result


def task_payload_from_record(vault: Path, path: str) -> dict[str, Any]:
    """The payload comes from the record, never from generation."""
    try:
        doc = load_markdown(vault / path)
    except Exception:
        return {}
    payload = doc.frontmatter.get("task_payload") or doc.frontmatter.get("payload") or {}
    return payload if isinstance(payload, dict) else {}


def set_task_status(vault: Path, path: str, status: str, db_path: Path | None = None) -> None:
    """Move a record's task_status (pending -> running -> resolved/blocked)
    and reindex so the poller sees it. Task lifecycle only — the record's
    own status (the open loop being open) belongs to the memory pipeline."""
    record = vault / path
    doc = load_markdown(record)
    fm = dict(doc.frontmatter)
    fm["task_status"] = status
    fm["updated"] = date.today().isoformat()
    write_markdown(record, fm, doc.body)
    from .rebuild_index import reindex_record

    reindex_record(record, vault, db_path, quiet=True)
