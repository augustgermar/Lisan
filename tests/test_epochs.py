"""Entity epochs: the old epoch is archived whole before the entity is replaced."""
from __future__ import annotations

from pathlib import Path

import pytest

from lisan.frontmatter import load_markdown, write_markdown
from lisan.tools.epochs import epoch_entity
from lisan.utils import today_iso


def _entity(vault: Path, **overrides: object) -> Path:
    path = vault / "entities" / "people" / "mara.md"
    fm = {
        "id": "person.mara",
        "type": "entity",
        "canonical_name": "Mara",
        "status": "active",
        "summary": "Works at the lab.",
        "epoch_started": "2024-01-01",
    }
    fm.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(path, fm, "# Mara\n\nOriginal body.\n")
    return path


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        epoch_entity(tmp_path / "entities" / "people" / "nobody.md", summary="x")


def test_only_entities_can_be_epoched(tmp_path: Path) -> None:
    path = _entity(tmp_path, type="episode")
    with pytest.raises(ValueError, match="Only entity files"):
        epoch_entity(path, summary="x")
    assert not (tmp_path / "archive").exists()


def test_first_epoch_archives_the_old_record_and_advances(tmp_path: Path) -> None:
    path = _entity(tmp_path)

    assert epoch_entity(path, summary="Left the lab; now freelancing.") == path

    archive_path = tmp_path / "archive" / "entities" / "mara-epoch1.md"
    archived = load_markdown(archive_path)
    assert archived.frontmatter["id"] == "person.mara.epoch1"
    assert archived.frontmatter["status"] == "archived"
    assert archived.frontmatter["epoch"] == 1
    assert archived.frontmatter["epoch_started"] == "2024-01-01"
    assert archived.frontmatter["summary"] == "Works at the lab."
    assert "Original body." in archived.body

    current = load_markdown(path)
    assert current.frontmatter["id"] == "person.mara"
    assert current.frontmatter["status"] == "active"
    assert current.frontmatter["epoch"] == 2
    assert current.frontmatter["epoch_started"] == today_iso()
    assert current.frontmatter["updated"] == today_iso()
    assert current.frontmatter["summary"] == "Left the lab; now freelancing."
    assert current.frontmatter["significance"] == "low"
    assert "disambiguation" not in current.frontmatter
    assert current.frontmatter["previous_epochs"] == [
        {
            "epoch": 1,
            "period": f"2024-01-01 to {today_iso()}",
            "archived": str(archive_path),
            "summary": "Works at the lab.",
        }
    ]
    assert current.body.strip() == "# Mara\n\nLeft the lab; now freelancing."
    assert "Original body." not in current.body


def test_second_epoch_keeps_history_and_existing_significance(tmp_path: Path) -> None:
    path = _entity(tmp_path, significance="high")
    epoch_entity(path, summary="Second chapter.")
    epoch_entity(path, summary="Third chapter.", disambiguation="the freelancer, not the lab manager")

    current = load_markdown(path).frontmatter
    assert current["epoch"] == 3
    assert current["significance"] == "high"
    assert current["disambiguation"] == "the freelancer, not the lab manager"
    assert [entry["epoch"] for entry in current["previous_epochs"]] == [1, 2]
    assert current["previous_epochs"][1]["summary"] == "Second chapter."

    second_archive = load_markdown(tmp_path / "archive" / "entities" / "mara-epoch2.md").frontmatter
    assert second_archive["id"] == "person.mara.epoch2"
    assert second_archive["summary"] == "Second chapter."
    assert [entry["epoch"] for entry in second_archive["previous_epochs"]] == [1]
    assert (tmp_path / "archive" / "entities" / "mara-epoch1.md").exists()


def test_title_falls_back_to_the_file_stem(tmp_path: Path) -> None:
    path = _entity(tmp_path)
    doc = load_markdown(path)
    fm = dict(doc.frontmatter)
    del fm["canonical_name"]
    del fm["id"]
    write_markdown(path, fm, doc.body)

    epoch_entity(path, summary="New.")

    assert load_markdown(path).body.startswith("# mara\n")
    archived = load_markdown(tmp_path / "archive" / "entities" / "mara-epoch1.md")
    assert archived.frontmatter["id"] == "mara.epoch1"
