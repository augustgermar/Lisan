from __future__ import annotations

import shutil
from pathlib import Path

from ..paths import repo_root


def wipe_live_run(run_dir: Path) -> dict[str, object]:
    run_dir = run_dir.resolve()
    expected_root = (repo_root() / ".lisan_live_eval_runs").resolve()
    if expected_root not in run_dir.parents:
        return {
            "run_dir": str(run_dir),
            "wiped": False,
            "reason": "run directory is outside .lisan_live_eval_runs",
        }
    marker = run_dir / ".lisan_eval_vault"
    if not marker.exists():
        return {
            "run_dir": str(run_dir),
            "wiped": False,
            "reason": "missing .lisan_eval_vault marker",
        }
    if not run_dir.exists():
        return {
            "run_dir": str(run_dir),
            "wiped": False,
            "reason": "run directory does not exist",
        }
    shutil.rmtree(run_dir)
    return {
        "run_dir": str(run_dir),
        "wiped": True,
        "reason": "marker-gated wipe",
    }
