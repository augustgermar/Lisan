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

# The bundled catalogue is gitignored as of 2026-08-14: a downloader gets the
# skills *machinery*, not the owner's personal set. So these tests validate
# whatever is present and skip when nothing is — asserting a fixed roster would
# make them fail on every clean checkout, which is the "green only on the
# developer's machine" failure this suite has already been bitten by twice.
requires_bundled = pytest.mark.skipif(
    not bundled_skills_root().is_dir()
    or not [d for d in bundled_skills_root().iterdir()
            if d.is_dir() and not d.name.startswith(("_", "."))],
    reason="no bundled skills present (they are gitignored; this is a clean checkout)",
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


@requires_bundled
def test_bundled_skills_discovered() -> None:
    """Every present skill is discoverable and names itself.

    Not an exact roster: the set is the owner's, not the project's.
    """
    skills = load_skills(bundled_skills_root())
    assert skills, "a non-empty skills dir produced no skills"
    for skill in skills:
        assert skill["name"], skill["skill_dir"]
        assert skill["description"], f"{skill['name']}: no description"


@requires_bundled
def test_bundled_schemas_are_well_formed() -> None:
    for skill_dir in sorted(bundled_skills_root().iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("_"):
            continue
        if not (skill_dir / "schema.json").is_file():
            # Instruction-only skills are valid under the Agent Skills format
            # and carry no schema; SKILL.md alone is enough.
            assert (skill_dir / "SKILL.md").is_file(), f"{skill_dir.name}: no SKILL.md and no schema"
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


@requires_bundled
def test_bundled_send_skills_require_approval() -> None:
    """Anything that leaves the machine stays gated — for whichever of those
    skills this install actually has."""
    by_name = {s["name"]: s for s in load_skills(bundled_skills_root())}
    for name in APPROVAL_GATED & set(by_name):
        assert by_name[name]["requires_approval"] is True, name
    if "gmail_search" in by_name:
        assert by_name["gmail_search"]["requires_approval"] is False


@requires_bundled
def test_every_bundled_tool_module_loads(tmp_path: Path) -> None:
    """load_skill_handlers imports every tool.py; a skill that fails to
    import is silently skipped, so handler coverage proves import health."""
    skills = load_skills(bundled_skills_root())
    executable = {s["name"] for s in skills if s["executable"]}
    handlers = load_skill_handlers(bundled_skills_root(), vault=tmp_path, config={})
    assert set(handlers) == executable


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



def _fake_catalogue(root: Path) -> Path:
    """A self-contained bundled catalogue, so install tests do not depend on
    the owner's skills existing.

    The install machinery is project code and deserves coverage on every
    checkout; the catalogue it happens to copy is the owner's and is gitignored.
    Building the fixture here tests the mechanism deterministically and made
    three tests stop failing on a clean checkout.
    """
    shared = root / "_demo_common"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    for name, deps in (("demo_search", ["_demo_common"]), ("demo_maps", [])):
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Demo skill {name}.\n---\n\nDo the demo.\n",
            encoding="utf-8")
        (d / "schema.json").write_text(json.dumps({
            "description": f"Demo skill {name}.",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "shared": deps,
        }), encoding="utf-8")
        (d / "tool.py").write_text("def run(args, vault, config):\n    return 'ok'\n", encoding="utf-8")
    return root


def test_install_skill_copies_shared_deps(tmp_path: Path) -> None:
    src = _fake_catalogue(tmp_path / "bundled")
    dest = tmp_path / "installed"
    written = install_skill("demo_search", bundled_dir=src, installed_dir=dest)
    assert (dest / "demo_search" / "tool.py").exists()
    assert (dest / "_demo_common" / "helper.py").exists()
    assert len(written) == 2
    handlers = load_skill_handlers(dest, vault=tmp_path, config={})
    assert "demo_search" in handlers


def test_install_refuses_overwrite_without_force(tmp_path: Path) -> None:
    src = _fake_catalogue(tmp_path / "bundled")
    dest = tmp_path / "installed"
    install_skill("demo_maps", bundled_dir=src, installed_dir=dest)
    with pytest.raises(FileExistsError):
        install_skill("demo_maps", bundled_dir=src, installed_dir=dest)
    install_skill("demo_maps", bundled_dir=src, installed_dir=dest, force=True)


def test_install_unknown_skill_raises(tmp_path: Path) -> None:
    src = _fake_catalogue(tmp_path / "bundled")
    with pytest.raises(ValueError):
        install_skill("nonexistent_skill", bundled_dir=src, installed_dir=tmp_path / "x")


def test_install_instructional_skill(tmp_path: Path) -> None:
    src = tmp_path / "bundled"
    skill = src / "research"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: research\ndescription: Research the web.\n---\n\nUse direct HTTP first.\n",
        encoding="utf-8",
    )
    dest = tmp_path / "installed"
    written = install_skill("research", bundled_dir=src, installed_dir=dest)
    assert written == [str(dest / "research")]
    assert (dest / "research" / "SKILL.md").exists()
    assert load_skills(dest)[0]["executable"] is False


def test_install_all_and_status_and_uninstall(tmp_path: Path) -> None:
    src = _fake_catalogue(tmp_path / "bundled")
    dest = tmp_path / "installed"
    install_all(bundled_dir=src, installed_dir=dest)
    installed_names = {s["name"] for s in load_skills(dest)}
    assert installed_names == {"demo_search", "demo_maps"}

    rows = skills_status(bundled_dir=src, installed_dir=dest)
    assert all(row["installed"] for row in rows)

    uninstall_skill("demo_maps", installed_dir=dest)
    assert "demo_maps" not in {s["name"] for s in load_skills(dest)}
    with pytest.raises(ValueError):
        uninstall_skill("demo_maps", installed_dir=dest)
    with pytest.raises(ValueError):
        uninstall_skill("_google_common", installed_dir=dest)
