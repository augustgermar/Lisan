from __future__ import annotations

import json
from pathlib import Path

import pytest

from lisan.tools.skill_loader import load_skill_handlers, load_skills
from lisan.tools.skills_cli import (
    bundled_skills_root,
    install_all,
    install_skill,
    skills_status,
    uninstall_skill,
)

EXPECTED_SKILLS = {
    "arxiv_search",
    "gmail_read",
    "gmail_search",
    "gmail_send",
    "imessage_history",
    "imessage_recent",
    "imessage_search",
    "imessage_send",
    "maps",
    "obsidian_read",
    "obsidian_search",
    "polymarket",
    "youtube_transcript",
}

APPROVAL_GATED = {"gmail_send", "imessage_send"}


def test_bundled_skills_discovered() -> None:
    names = {s["name"] for s in load_skills(bundled_skills_root())}
    assert names == EXPECTED_SKILLS


def test_bundled_schemas_are_well_formed() -> None:
    for skill_dir in sorted(bundled_skills_root().iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("_"):
            continue
        schema = json.loads((skill_dir / "schema.json").read_text(encoding="utf-8"))
        assert schema.get("description"), f"{skill_dir.name}: missing description"
        params = schema.get("parameters")
        assert params and params.get("type") == "object", f"{skill_dir.name}: bad parameters"
        assert isinstance(params.get("properties"), dict), f"{skill_dir.name}: no properties"
        assert (skill_dir / "SKILL.md").exists(), f"{skill_dir.name}: missing SKILL.md"
        for shared in schema.get("shared", []):
            assert (bundled_skills_root() / shared).is_dir(), (
                f"{skill_dir.name}: declares missing shared dir {shared}"
            )


def test_bundled_send_skills_require_approval() -> None:
    by_name = {s["name"]: s for s in load_skills(bundled_skills_root())}
    for name in APPROVAL_GATED:
        assert by_name[name]["requires_approval"] is True, name
    assert by_name["gmail_search"]["requires_approval"] is False


def test_every_bundled_tool_module_loads(tmp_path: Path) -> None:
    """load_skill_handlers imports every tool.py; a skill that fails to
    import is silently skipped, so handler coverage proves import health."""
    handlers = load_skill_handlers(bundled_skills_root(), vault=tmp_path, config={})
    assert set(handlers) == EXPECTED_SKILLS


def _write_skill(root: Path, name: str, *, requires_approval: bool = False) -> None:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    (skill / "schema.json").write_text(
        json.dumps(
            {
                "description": "test skill",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "requires_approval": requires_approval,
            }
        ),
        encoding="utf-8",
    )
    (skill / "tool.py").write_text(
        "def run(args, vault, config):\n    return 'RAN'\n", encoding="utf-8"
    )


def test_gated_skill_denied_without_approval(tmp_path: Path) -> None:
    _write_skill(tmp_path, "danger_skill", requires_approval=True)
    handlers = load_skill_handlers(
        tmp_path, vault=tmp_path, config={}, approval_fn=lambda *_: False
    )
    result = handlers["danger_skill"]()
    assert "RAN" not in result
    assert "approval" in result.lower()


def test_gated_skill_runs_with_approval(tmp_path: Path) -> None:
    _write_skill(tmp_path, "danger_skill", requires_approval=True)
    seen: list[tuple[str, dict]] = []

    def approve(tool_name: str, args: dict) -> bool:
        seen.append((tool_name, args))
        return True

    handlers = load_skill_handlers(tmp_path, vault=tmp_path, config={}, approval_fn=approve)
    assert handlers["danger_skill"]() == "RAN"
    assert seen and seen[0][0] == "danger_skill"
    assert "task" in seen[0][1]


def test_gated_skill_denied_when_no_approval_channel(tmp_path: Path) -> None:
    _write_skill(tmp_path, "danger_skill", requires_approval=True)
    handlers = load_skill_handlers(tmp_path, vault=tmp_path, config={}, approval_fn=None)
    result = handlers["danger_skill"]()
    assert "RAN" not in result


def test_ungated_skill_never_asks_for_approval(tmp_path: Path) -> None:
    _write_skill(tmp_path, "calm_skill", requires_approval=False)

    def explode(*_args) -> bool:
        raise AssertionError("approval_fn must not be called for ungated skills")

    handlers = load_skill_handlers(tmp_path, vault=tmp_path, config={}, approval_fn=explode)
    assert handlers["calm_skill"]() == "RAN"


def test_install_skill_copies_shared_deps(tmp_path: Path) -> None:
    dest = tmp_path / "installed"
    written = install_skill("gmail_search", installed_dir=dest)
    assert (dest / "gmail_search" / "tool.py").exists()
    assert (dest / "_google_common" / "lisan_google.py").exists()
    assert len(written) == 2
    handlers = load_skill_handlers(dest, vault=tmp_path, config={})
    assert "gmail_search" in handlers


def test_install_refuses_overwrite_without_force(tmp_path: Path) -> None:
    dest = tmp_path / "installed"
    install_skill("maps", installed_dir=dest)
    with pytest.raises(FileExistsError):
        install_skill("maps", installed_dir=dest)
    install_skill("maps", installed_dir=dest, force=True)


def test_install_unknown_skill_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        install_skill("nonexistent_skill", installed_dir=tmp_path / "x")


def test_install_all_and_status_and_uninstall(tmp_path: Path) -> None:
    dest = tmp_path / "installed"
    install_all(installed_dir=dest)
    installed_names = {s["name"] for s in load_skills(dest)}
    assert installed_names == EXPECTED_SKILLS

    rows = skills_status(installed_dir=dest)
    assert all(row["installed"] for row in rows if row["name"] in EXPECTED_SKILLS)

    uninstall_skill("maps", installed_dir=dest)
    assert "maps" not in {s["name"] for s in load_skills(dest)}
    with pytest.raises(ValueError):
        uninstall_skill("maps", installed_dir=dest)
    with pytest.raises(ValueError):
        uninstall_skill("_google_common", installed_dir=dest)
