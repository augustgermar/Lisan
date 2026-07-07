"""`lisan restart` — bounce the resident service without orphaning work.

Exists for two reasons, both from the same week. The agent once told the
owner to run `lisan restart` when no such command existed (an invented
capability); and the developer once bounced the service mid-drain with a
raw `launchctl kickstart -k`, orphaning seven claimed jobs. The command is
now real, and it institutionalizes the discipline: it looks at what is in
flight BEFORE it kills anything, and refuses a mid-drain bounce unless
forced. The stale-job reclaimer would eventually recover orphans anyway
(45 minutes) — this avoids creating them at all.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from ..paths import sqlite_path

_LAUNCHD_LABEL = "com.lisan.telegram"
_SYSTEMD_UNIT = "lisan-telegram.service"


def running_jobs(db_path: Path | None = None) -> list[dict[str, Any]]:
    from .db import connect

    db = db_path or sqlite_path()
    if not Path(db).exists():
        return []
    try:
        conn = connect(db, readonly=True)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT id, job_type, started_at, worker_id FROM jobs "
            "WHERE status = 'running' ORDER BY started_at"
        ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []
    finally:
        conn.close()


def restart_service(
    *,
    db_path: Path | None = None,
    force: bool = False,
    runner: Any = subprocess.run,
    system: str | None = None,
) -> dict[str, Any]:
    """Refuse to bounce over in-flight jobs unless forced; then restart the
    resident service (which hosts the scheduler thread). Returns a report
    dict; never raises for service-manager failures — the caller renders."""
    in_flight = running_jobs(db_path)
    if in_flight and not force:
        return {
            "restarted": False,
            "reason": "jobs_in_flight",
            "running_jobs": in_flight,
            "hint": (
                "These jobs are mid-run; a restart now orphans them until the "
                "45-minute stale reclaim. Wait for them to finish, or re-run "
                "with --force if the service itself is the problem."
            ),
        }

    import platform

    system = system or platform.system()
    if system == "Darwin":
        cmd = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"]
    elif system == "Linux":
        cmd = ["systemctl", "--user", "restart", _SYSTEMD_UNIT]
    else:
        return {"restarted": False, "reason": f"unsupported_platform:{system}"}

    try:
        result = runner(cmd, capture_output=True, text=True, timeout=30)
        ok = result.returncode == 0
        return {
            "restarted": ok,
            "command": " ".join(cmd),
            "forced_over_jobs": [j["id"] for j in in_flight] if in_flight else [],
            **({} if ok else {"reason": (result.stderr or result.stdout or "").strip() or "nonzero exit"}),
        }
    except Exception as exc:
        return {"restarted": False, "reason": str(exc), "command": " ".join(cmd)}


def render_restart_report(report: dict[str, Any]) -> str:
    if report.get("restarted"):
        lines = [f"Service restarted ({report.get('command', '')})."]
        if report.get("forced_over_jobs"):
            lines.append(
                "Forced over running jobs: "
                + ", ".join(report["forced_over_jobs"])
                + " — they will re-run via the stale reclaim."
            )
        return "\n".join(lines)
    if report.get("reason") == "jobs_in_flight":
        lines = ["Not restarting — jobs are mid-run:"]
        for job in report.get("running_jobs", []):
            lines.append(f"  {job['id']}  {job['job_type']}  started {job.get('started_at')}")
        lines.append(report.get("hint", ""))
        return "\n".join(lines)
    return f"Restart failed: {report.get('reason', 'unknown')}"
