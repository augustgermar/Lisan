"""The agent's explicit self-model: what it can do, and how it is doing.

Self-knowledge here is generated, never written by hand. Hand-written
self-descriptions are how an agent ends up confabulating capabilities —
prose about the system drifts from the system. Instead this module
introspects the running code (the argparse tree, the tool registry, the job
types, the schemas) and emits:

- a capability manifest (`build_capability_manifest`) — the complete,
  honest inventory, including a declared list of what is designed but NOT
  built, rendered into `primer/capabilities.md` as the Layer-2 identity
  document beside `identity-core.md`;
- a compact capability index (`capability_index`) injected into the
  interlocutor's context every turn, so the agent always knows *what* it
  can do and looks up *how* only when acting;
- a live operational snapshot (`snapshot_self_state`) behind the
  `self_state` tool, so questions about the agent's own state are answered
  from data, never from memory or plausibility.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any
from .db import connect as _db_connect

from .. import __version__
from ..config import load_config
from ..paths import repo_root, schemas_dir, skills_root, sqlite_path, vault_root
from ..utils import utc_now_iso

# Capabilities that are designed but deliberately not built yet. Declared
# explicitly so the agent can say "not yet" with confidence instead of
# improvising — honesty about absence is part of the self-model.
NOT_BUILT: list[dict[str, str]] = [
    {
        "name": "Obsidian life-ingestion",
        "detail": "Seeding entity stories from personal notes and wiki-links. "
                  "Reference ingestion (lisan ingest --reference) is the available alternative: "
                  "notes become citable knowledge records, not living entity stories.",
    },
    {
        "name": "Chat/SMS history import",
        "detail": "Bulk import of historical conversations with per-sender deixis resolution.",
    },
    {
        "name": "External communication",
        "detail": "Messaging anyone other than the owner. Scheduled reminders deliver to the "
                  "owner's allowlisted Telegram chat only; a disclosure gate comes first.",
    },
]


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root()), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _introspect_cli() -> list[dict[str, Any]]:
    """Walk the real argparse tree: every command, subcommand, and flag.
    Generated from the parser itself, so it can neither lie nor lag."""
    from ..cli import build_parser

    def options(parser: argparse.ArgumentParser) -> list[dict[str, str]]:
        out = []
        for action in parser._actions:
            if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
                continue
            flags = ", ".join(action.option_strings) if action.option_strings else f"<{action.dest}>"
            out.append({"arg": flags, "help": action.help or ""})
        return out

    def subcommands(parser: argparse.ArgumentParser) -> list[tuple[str, argparse.ArgumentParser, str]]:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                helps = {choice.dest: choice.help or "" for choice in action._choices_actions}
                return [(name, sub, helps.get(name, "")) for name, sub in action.choices.items()]
        return []

    commands: list[dict[str, Any]] = []
    for name, sub, help_text in subcommands(build_parser()):
        entry: dict[str, Any] = {"command": f"lisan {name}", "help": help_text, "options": options(sub)}
        nested = [
            {"command": f"lisan {name} {sub_name}", "help": sub_help, "options": options(sub_sub)}
            for sub_name, sub_sub, sub_help in subcommands(sub)
        ]
        if nested:
            entry["subcommands"] = nested
        commands.append(entry)
    return commands


def _tool_inventory(config: dict[str, Any] | None = None) -> list[dict[str, str]]:
    from .execution_tools import TOOLS
    from .skill_loader import load_skills

    tools = [{"name": t["name"], "description": str(t.get("description") or "")} for t in TOOLS]
    try:
        for skill in load_skills(skills_root()):
            tools.append({
                "name": str(skill.get("name") or ""),
                "description": f"[skill] {skill.get('description') or ''}",
            })
    except Exception:
        pass
    return tools


def build_capability_manifest(config: dict[str, Any] | None = None) -> dict[str, Any]:
    from .job_policy import DEFAULT_JOB_PRIORITIES
    from .jobs import JOB_TYPES

    config = config or load_config()
    schemas = sorted(p.stem for p in schemas_dir().glob("*.json"))
    return {
        "software": "Lisan",
        "version": __version__,
        "commit": _git_commit(),
        "generated_at": utc_now_iso(),
        "paths": {
            "repo": str(repo_root()),
            "vault": str(vault_root()),
            "database": str(sqlite_path()),
            "skills": str(skills_root()),
        },
        "cli": _introspect_cli(),
        "tools": _tool_inventory(config),
        "job_types": sorted(JOB_TYPES),
        "job_priorities": dict(sorted(DEFAULT_JOB_PRIORITIES.items())),
        "record_schemas": schemas,
        "routing": config.get("routing", {}),
        "not_built": NOT_BUILT,
    }


def render_capability_primer(manifest: dict[str, Any]) -> str:
    """The full self-model as markdown — Layer 2 of the identity model."""
    lines = [
        "---",
        json.dumps(
            {
                "type": "capabilities",
                "generated": True,
                "stamp": _stamp(manifest),
                "generated_at": manifest["generated_at"],
            },
            indent=2,
        ),
        "---",
        "",
        "# Capabilities (generated — do not edit)",
        "",
        f"Software: {manifest['software']} v{manifest['version']}"
        + (f" (commit {manifest['commit']})" if manifest["commit"] else ""),
        "",
        "This file is regenerated from the code whenever the installed version",
        "changes. It is the factual inventory of what this instance can do.",
        "",
        "## Paths",
        "",
    ]
    for key, value in manifest["paths"].items():
        lines.append(f"- {key}: `{value}`")
    lines += ["", "## Conversation tools", ""]
    for tool in manifest["tools"]:
        lines.append(f"- `{tool['name']}` — {tool['description']}")
    lines += ["", "## CLI commands (runnable via the run_codex tool)", ""]
    for cmd in manifest["cli"]:
        lines.append(f"### `{cmd['command']}` — {cmd['help']}")
        for opt in cmd["options"]:
            if opt["arg"] not in ("--vault", "--db-path"):
                lines.append(f"- `{opt['arg']}` {opt['help']}")
        for sub in cmd.get("subcommands", []):
            lines.append(f"- `{sub['command']}` — {sub['help']}")
            for opt in sub["options"]:
                if opt["arg"] not in ("--vault", "--db-path"):
                    lines.append(f"  - `{opt['arg']}` {opt['help']}")
        lines.append("")
    lines += ["## Background job types", ""]
    for job_type in manifest["job_types"]:
        lines.append(f"- `{job_type}`")
    lines += ["", "## Record types", "", ", ".join(manifest["record_schemas"]), ""]
    lines += ["## Not built yet — say so plainly when asked", ""]
    for item in manifest["not_built"]:
        lines.append(f"- **{item['name']}** — {item['detail']}")
    lines.append("")
    return "\n".join(lines)


def capability_index(manifest: dict[str, Any] | None = None) -> str:
    """The compact always-in-context version: one line per capability."""
    manifest = manifest or build_capability_manifest()
    lines = [
        f"You are {manifest['software']} v{manifest['version']}. Your capabilities (details in primer/capabilities.md; live status via the self_state tool):",
        "Tools: " + ", ".join(t["name"] for t in manifest["tools"]),
        "CLI (via run_codex): " + ", ".join(c["command"].removeprefix("lisan ") for c in manifest["cli"]),
        "Not built yet: " + "; ".join(i["name"] for i in manifest["not_built"]) + ".",
    ]
    return "\n".join(lines)


@lru_cache(maxsize=1)
def cached_capability_index() -> str:
    return capability_index()


@lru_cache(maxsize=1)
def cli_reference() -> str:
    """Compact command reference for briefing delegated codex sessions:
    every command and flag, one line each, generated from the parser."""
    manifest = build_capability_manifest()
    lines = []
    for cmd in manifest["cli"]:
        flags = " ".join(f"[{o['arg']}]" for o in cmd["options"] if o["arg"] not in ("--vault", "--db-path"))
        lines.append(f"{cmd['command']} {flags}".rstrip() + (f"  # {cmd['help']}" if cmd["help"] else ""))
        for sub in cmd.get("subcommands", []):
            sub_flags = " ".join(f"[{o['arg']}]" for o in sub["options"] if o["arg"] not in ("--vault", "--db-path"))
            lines.append(f"  {sub['command']} {sub_flags}".rstrip() + (f"  # {sub['help']}" if sub["help"] else ""))
    return "\n".join(lines)


def _stamp(manifest: dict[str, Any]) -> str:
    return f"{manifest['version']}+{manifest['commit'] or 'nocommit'}"


def ensure_capabilities_primer(vault: Path | None = None, *, force: bool = False) -> Path | None:
    """Regenerate primer/capabilities.md when the installed code changes.
    Returns the path when (re)written, None when already current."""
    vault = vault or vault_root()
    manifest = build_capability_manifest()
    path = vault / "primer" / "capabilities.md"
    stamp = _stamp(manifest)
    if not force and path.exists() and f'"stamp": "{stamp}"' in path.read_text(encoding="utf-8"):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_capability_primer(manifest), encoding="utf-8")
    return path


# ── Live operational state ───────────────────────────────────────────────────

def _skill_auth_status() -> dict:
    out: dict = {}
    try:
        import sys as _sys

        common = str(Path(__file__).resolve().parents[2] / "skills" / "_google_common")
        if common not in _sys.path:
            _sys.path.insert(0, common)
        import lisan_google as _g

        from ..config import load_config

        cfg = load_config()
        tok = _g.load_token(cfg)
        out["google"] = "authorized (token valid)" if not _g.token_expired(tok) else "token expired — refresh will run on next use"
        out["google_auth_command"] = "lisan skills auth gmail"
    except Exception:
        out["google"] = "not authorized — run: lisan skills auth gmail"
    return out


def snapshot_self_state(vault: Path | None = None, db_path: Path | None = None) -> dict[str, Any]:
    """What is actually going on right now: queue, schedule, index, services,
    recent errors. Every field comes from live data."""
    import sqlite3

    vault = vault or vault_root()
    db = db_path or sqlite_path()
    state: dict[str, Any] = {
        "version": __version__,
        "commit": _git_commit(),
        "checked_at": utc_now_iso(),
    }
    # Live skill-auth status: the agent kept answering auth questions from
    # stale memory ('the Hermes token', invented setup commands). Interoception
    # beats confabulation — put the truth where self_state can see it.
    state["skill_auth"] = _skill_auth_status()

    jobs_by_status: dict[str, dict[str, int]] = {}
    next_task = None
    index_records = 0
    try:
        conn = _db_connect(db)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute("SELECT status, job_type, COUNT(*) AS n FROM jobs GROUP BY status, job_type"):
                jobs_by_status.setdefault(str(row["status"]), {})[str(row["job_type"])] = int(row["n"])
            row = conn.execute(
                """SELECT id, job_type, scheduled_for, recurrence, payload_json FROM jobs
                   WHERE status = 'queued' AND scheduled_for IS NOT NULL
                   ORDER BY scheduled_for ASC LIMIT 1"""
            ).fetchone()
            if row:
                next_task = {k: row[k] for k in ("id", "job_type", "scheduled_for", "recurrence")}
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                    body = str(payload.get("message") or payload.get("prompt") or payload.get("task") or "").strip()
                    if body:
                        next_task["about"] = body if len(body) <= 80 else body[:77] + "..."
                except Exception:
                    pass
            due_row = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'queued' "
                "AND (scheduled_for IS NULL OR scheduled_for <= ?)",
                (utc_now_iso(),),
            ).fetchone()
            state["queued_due_now"] = int(due_row[0]) if due_row else 0
            index_records = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        finally:
            conn.close()
    except Exception as exc:
        state["database_error"] = str(exc)
    state["jobs"] = jobs_by_status
    state["next_scheduled_task"] = next_task
    state["index_records"] = index_records

    for job_type in ("dreamer.maintenance", "analyst.scan"):
        state[f"last_{job_type.split('.')[0]}_success"] = _last_success_time(job_type, db)

    try:
        from .plans import active_plans

        state["active_plans"] = [
            {"goal": pl["goal"], "progress": f"{pl['steps_done']}/{pl['steps_total']}"}
            for pl in active_plans(db_path=db)
        ]
    except Exception:
        state["active_plans"] = []

    state["services"] = _service_status()
    state["machine"] = _machine_sleep_status()

    try:
        import re as _re

        from .log import tail_log

        # Only whole, timestamped log lines: the tail of a multi-line
        # traceback ("TimeoutError: ...") is a context-free shard the model
        # narrates into a story about whatever it currently believes.
        tail = tail_log(vault, lines=40)
        stamped = [
            ln for ln in (tail.strip().splitlines() if tail else [])
            if _re.match(r"^\d{4}-\d{2}-\d{2} ", ln)
        ]
        state["recent_log_tail"] = stamped[-5:]
    except Exception:
        state["recent_log_tail"] = []
    return state


def _last_success_time(job_type: str, db_path: Path) -> str | None:
    import sqlite3

    try:
        conn = _db_connect(db_path)
        try:
            row = conn.execute(
                "SELECT finished_at FROM jobs WHERE job_type = ? AND status = 'succeeded' "
                "ORDER BY finished_at DESC LIMIT 1",
                (job_type,),
            ).fetchone()
            return str(row[0]) if row and row[0] else None
        finally:
            conn.close()
    except Exception:
        return None


def _machine_sleep_status() -> dict[str, str]:
    """When the computer last slept and woke. The agent cannot tell the
    difference between "my services are broken" and "the machine was asleep"
    without this — on 2026-07-06 that gap produced an invented 'stalled task
    processor' diagnosis (and got a healthy process killed) when the real
    story was a Mac in Deep Idle all morning."""
    import platform
    import re as _re
    from datetime import datetime, timezone

    if platform.system() != "Darwin":
        return {}
    out: dict[str, str] = {}
    for key, name in (("last_sleep", "kern.sleeptime"), ("last_wake", "kern.waketime")):
        try:
            result = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5)
            match = _re.search(r"sec = (\d+)", result.stdout)
            if match:
                stamp = datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc).astimezone()
                out[key] = stamp.strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            continue
    return out


def _service_status() -> dict[str, bool]:
    """Is the always-on layer actually alive? Platform-aware, non-fatal."""
    import platform

    services = {"telegram": False, "scheduler": False}
    labels = {"telegram": "com.lisan.telegram", "scheduler": "com.lisan.scheduler"}
    units = {"telegram": "lisan-telegram.service", "scheduler": "lisan-scheduler.service"}
    try:
        if platform.system() == "Darwin":
            result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
            for key, label in labels.items():
                for line in result.stdout.splitlines():
                    if line.endswith(label) and not line.startswith("-"):
                        services[key] = True
        elif platform.system() == "Linux":
            for key, unit in units.items():
                result = subprocess.run(
                    ["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=5
                )
                services[key] = result.stdout.strip() == "active"
    except Exception:
        pass
    # The Telegram service hosts the scheduler loop as a thread — when it is
    # up, scheduling is up. Reporting "scheduler down" because the *standalone*
    # service isn't installed would tell the user their reminders are broken
    # when they aren't.
    if services["telegram"] and not services["scheduler"]:
        services["scheduler"] = True
    return services


def render_self_state(state: dict[str, Any]) -> str:
    """Human/model-readable snapshot for the self_state tool and CLI."""
    lines = [
        f"Lisan v{state['version']}" + (f" (commit {state['commit']})" if state.get("commit") else ""),
        f"Checked at {state['checked_at']}",
        f"Index: {state.get('index_records', 0)} records",
    ]
    jobs = state.get("jobs") or {}
    if jobs:
        for status in sorted(jobs):
            per_type = ", ".join(f"{t}×{n}" for t, n in sorted(jobs[status].items()))
            line = f"Jobs {status}: {per_type}"
            # "queued" lumps due-now work with jobs waiting for a future
            # scheduled time; without the split, tomorrow's reminder reads
            # as a stuck queue (it did, twice, on 2026-07-06).
            if status == "queued" and state.get("queued_due_now") is not None:
                total = sum(jobs[status].values())
                future = total - int(state["queued_due_now"])
                if future > 0:
                    line += (
                        f" — {state['queued_due_now']} due now, {future} scheduled for a"
                        " future time (waiting for their moment, not stuck)"
                    )
            lines.append(line)
    else:
        lines.append("Jobs: queue is empty")
    nxt = state.get("next_scheduled_task")
    if nxt:
        recur = f" (recurring {nxt['recurrence']})" if nxt.get("recurrence") else ""
        about = f' — "{nxt["about"]}"' if nxt.get("about") else ""
        lines.append(f"Next scheduled: {nxt['job_type']} at {nxt['scheduled_for']}{recur}{about}")
    for key in ("last_dreamer_success", "last_analyst_success"):
        label = key.replace("last_", "").replace("_success", "")
        lines.append(f"Last {label} success: {state.get(key) or 'never'}")
    plans = state.get("active_plans") or []
    for pl in plans:
        lines.append(f"Active plan ({pl['progress']} steps): {pl['goal']}")
    services = state.get("services") or {}
    lines.append(
        "Services: " + ", ".join(f"{name} {'up' if up else 'down'}" for name, up in sorted(services.items()))
    )
    machine = state.get("machine") or {}
    if machine.get("last_wake") or machine.get("last_sleep"):
        lines.append(
            f"Machine: last slept {machine.get('last_sleep') or 'unknown'}, "
            f"last woke {machine.get('last_wake') or 'unknown'} — while the computer "
            "sleeps, all services pause and messages wait; a silent stretch between "
            "those times was the machine asleep, not a service failure."
        )
    tail = state.get("recent_log_tail") or []
    if tail:
        lines.append("Recent log: " + " | ".join(tail[-2:]))
    if state.get("database_error"):
        lines.append(f"Database error: {state['database_error']}")
    return "\n".join(lines)
