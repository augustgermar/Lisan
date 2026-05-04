from __future__ import annotations

from datetime import date
from pathlib import Path

from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso


def epoch_entity(
    path: Path,
    summary: str,
    disambiguation: str | None = None,
) -> Path:
    if not path.exists():
        raise FileNotFoundError(path)
    doc = load_markdown(path)
    fm = dict(doc.frontmatter)
    if str(fm.get("type")) != "entity":
        raise ValueError("Only entity files can be epoch-ed")

    current_epoch = int(fm.get("epoch", 1) or 1)
    next_epoch = current_epoch + 1
    archive_dir = path.parents[2] / "archive" / "entities"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{path.stem}-epoch{current_epoch}.md"
    archived_fm = dict(fm)
    archived_fm["id"] = f"{fm.get('id', path.stem)}.epoch{current_epoch}"
    archived_fm["status"] = "archived"
    archived_fm["previous_epochs"] = list(fm.get("previous_epochs", []))
    archived_fm["epoch"] = current_epoch
    archived_fm["epoch_started"] = fm.get("epoch_started", today_iso())
    archived_fm["updated"] = today_iso()
    write_markdown(archive_path, archived_fm, doc.body)

    previous_epochs = list(fm.get("previous_epochs", []))
    previous_epochs.append(
        {
            "epoch": current_epoch,
            "period": f"{fm.get('epoch_started', today_iso())} to {today_iso()}",
            "archived": str(archive_path),
            "summary": str(fm.get("summary", "")),
        }
    )

    fm["updated"] = today_iso()
    fm["significance"] = fm.get("significance", "low")
    fm["epoch"] = next_epoch
    fm["epoch_started"] = today_iso()
    fm["previous_epochs"] = previous_epochs
    fm["summary"] = summary
    if disambiguation:
        fm["disambiguation"] = disambiguation

    body = f"# {fm.get('canonical_name', path.stem)}\n\n{summary}\n"
    write_markdown(path, fm, body)
    return path
