"""Behavioral metrics for the Phase 2 eval instrumentation (WO-3).

Everything here reads existing state and defaults to zero for organs that
are not built yet — that zero IS the baseline the later work orders are
measured against. Sources:

- open-loop closure: frontmatter status over ``open_loops/``
- unprompted callbacks: ``drive.callback`` marker lines in the vault log
  (emitted by WO-5's delivery seam; zero until then)
- self-revision events: ``self_belief`` records with a revision chain
  (WO-4/WO-6; zero until then)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisan.frontmatter import load_markdown  # noqa: E402


def _frontmatter(path: Path) -> dict:
    try:
        return dict(load_markdown(path).frontmatter)
    except Exception:
        return {}


def open_loop_metrics(vault: Path) -> dict:
    total = resolved = 0
    root = vault / "open_loops"
    if root.exists():
        for path in root.glob("*.md"):
            fm = _frontmatter(path)
            if str(fm.get("type") or "") != "open_loop":
                continue
            total += 1
            if str(fm.get("status") or "") in ("resolved", "closed", "archived"):
                resolved += 1
    return {
        "open_loops_total": total,
        "open_loops_resolved": resolved,
        "closure_rate": round(resolved / total, 3) if total else 0.0,
    }


def callback_metrics(vault: Path) -> dict:
    log_path = vault / "logs" / "lisan.log"
    delivered = suppressed = 0
    if log_path.exists():
        try:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if "drive.callback.delivered" in line:
                    delivered += 1
                elif "drive.callback.suppressed" in line:
                    suppressed += 1
        except OSError:
            pass
    return {"callbacks_delivered": delivered, "callbacks_suppressed": suppressed}


def self_revision_metrics(vault: Path) -> dict:
    count = 0
    root = vault / "self"
    if root.exists():
        for path in root.rglob("*.md"):
            fm = _frontmatter(path)
            if str(fm.get("type") or "") == "self_belief" and (fm.get("revisions") or []):
                count += 1
    return {"self_belief_revisions": count}


def compute_metrics(vault: Path) -> dict:
    out: dict = {}
    out.update(open_loop_metrics(vault))
    out.update(callback_metrics(vault))
    out.update(self_revision_metrics(vault))
    return out


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, default=Path("/Users/august/.lisan/vault"))
    args = parser.parse_args()
    print(json.dumps(compute_metrics(args.vault), indent=2))
