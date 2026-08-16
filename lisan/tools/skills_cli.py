from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..paths import repo_root, skills_root
from .skill_loader import load_skills


def bundled_skills_root(base: Path | None = None) -> Path:
    """Skills shipped with the repo, installable into the user's skills dir."""
    return (base or repo_root()) / "skills"


def _shared_deps(skill_dir: Path) -> list[str]:
    """A skill declares shared library directories (siblings starting with
    an underscore) via a `"shared"` list in schema.json."""
    try:
        schema = json.loads((skill_dir / "schema.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    shared = schema.get("shared", [])
    if not isinstance(shared, list):
        return []
    return [str(s) for s in shared if str(s).startswith("_")]


def list_bundled_skills(bundled_dir: Path | None = None) -> list[dict[str, Any]]:
    return load_skills(bundled_dir if bundled_dir is not None else bundled_skills_root())


def list_installed_skills(installed_dir: Path | None = None) -> list[dict[str, Any]]:
    return load_skills(installed_dir if installed_dir is not None else skills_root())


def skills_status(
    *, bundled_dir: Path | None = None, installed_dir: Path | None = None
) -> list[dict[str, Any]]:
    """One row per known skill: bundled, installed, or both."""
    bundled = {s["name"]: s for s in list_bundled_skills(bundled_dir)}
    installed = {s["name"]: s for s in list_installed_skills(installed_dir)}
    rows: list[dict[str, Any]] = []
    for name in sorted(set(bundled) | set(installed)):
        skill = installed.get(name) or bundled[name]
        rows.append(
            {
                "name": name,
                "description": str(skill.get("description") or ""),
                "requires_approval": bool(skill.get("requires_approval")),
                "bundled": name in bundled,
                "installed": name in installed,
            }
        )
    return rows


def install_skill(
    name: str,
    *,
    bundled_dir: Path | None = None,
    installed_dir: Path | None = None,
    force: bool = False,
) -> list[str]:
    """Copy a bundled skill (and its shared library dirs) into the active
    skills directory. Returns the list of directories written."""
    src_root = bundled_dir if bundled_dir is not None else bundled_skills_root()
    dst_root = installed_dir if installed_dir is not None else skills_root()
    src = src_root / name
    # Both supported skill kinds are installable: instructional skills need
    # only SKILL.md, while executable skills also carry schema.json/tool.py.
    if not (src / "SKILL.md").is_file():
        raise ValueError(f"no bundled skill named {name!r} in {src_root}")
    written: list[str] = []
    targets = [name] + _shared_deps(src)
    dst_root.mkdir(parents=True, exist_ok=True)
    for target in targets:
        target_src = src_root / target
        target_dst = dst_root / target
        if not target_src.is_dir():
            raise ValueError(f"skill {name!r} declares missing shared dir {target!r}")
        if target_dst.exists():
            if not force and not target.startswith("_"):
                raise FileExistsError(
                    f"{target_dst} already exists; pass --force to overwrite"
                )
            shutil.rmtree(target_dst)
        shutil.copytree(target_src, target_dst, ignore=shutil.ignore_patterns("__pycache__"))
        written.append(str(target_dst))
    return written


def install_all(
    *,
    bundled_dir: Path | None = None,
    installed_dir: Path | None = None,
    force: bool = False,
) -> list[str]:
    written: list[str] = []
    for skill in list_bundled_skills(bundled_dir):
        written.extend(
            install_skill(
                str(skill["name"]),
                bundled_dir=bundled_dir,
                installed_dir=installed_dir,
                force=force,
            )
        )
    return written


def uninstall_skill(name: str, *, installed_dir: Path | None = None) -> str:
    """Remove an installed skill. Shared `_lib` dirs are left in place because
    other skills may still import them; they are harmless when orphaned (the
    loader ignores directories without schema.json)."""
    dst_root = installed_dir if installed_dir is not None else skills_root()
    target = dst_root / name
    if name.startswith("_") or not (target / "SKILL.md").is_file():
        raise ValueError(f"no installed skill named {name!r} in {dst_root}")
    shutil.rmtree(target)
    return str(target)


def setup_skill(
    name: str, forwarded_args: list[str], *, installed_dir: Path | None = None
) -> int:
    """Run a skill's setup script (credential onboarding), forwarding CLI args.

    Looks for setup.py in the installed skill dir first, then in any shared
    dirs the skill declares. The script runs in a subprocess with the same
    interpreter, so its exit code and output flow straight back to the user.
    """
    dst_root = installed_dir if installed_dir is not None else skills_root()
    skill_dir = dst_root / name
    if not skill_dir.is_dir():
        raise ValueError(
            f"skill {name!r} is not installed; run `lisan skills install {name}` first"
        )
    candidates = [skill_dir / "setup.py"]
    for shared in _shared_deps(skill_dir):
        candidates.append(dst_root / shared / "setup.py")
    script = next((c for c in candidates if c.exists()), None)
    if script is None:
        raise ValueError(f"skill {name!r} has no setup script")
    result = subprocess.run([sys.executable, str(script), *forwarded_args])
    return int(result.returncode)


# ── format compliance (2026-08-14) ────────────────────────────────────────────

def validate_skills(skills_dir: Path | None = None) -> list[dict[str, Any]]:
    """Check every skill in a directory against the Agent Skills format.

    Returns one row per skill with its errors. The point is that a
    non-compliant skill is *reported* rather than silently skipped, which is
    how the old loader handled anything it did not recognise.
    """
    from .skill_format import SKILL_FILE, parse_skill_md

    root = skills_dir if skills_dir is not None else skills_root()
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for skill_dir in sorted(root.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith((".", "_", "__")):
            continue
        doc = skill_dir / SKILL_FILE
        if not doc.is_file():
            rows.append({"name": skill_dir.name, "errors": [f"no {SKILL_FILE}"],
                         "kind": "unrecognised", "path": str(skill_dir)})
            continue
        manifest = parse_skill_md(doc)
        executable = (skill_dir / "schema.json").is_file() and (skill_dir / "tool.py").is_file()
        errors = list(manifest.errors)
        if not manifest.description and executable:
            # schema.json can still supply it, so this is a migration nudge
            # rather than a fault.
            errors = [e for e in errors if "description" not in e]
            errors.append("description lives in schema.json, not SKILL.md frontmatter (run `lisan skills migrate`)")
        rows.append({
            "name": manifest.name,
            "errors": errors,
            "kind": "executable" if executable else "instructional",
            "version": manifest.version,
            "path": str(skill_dir),
        })
    return rows


def migrate_skill_frontmatter(skills_dir: Path | None = None, *, apply: bool = False) -> list[dict[str, Any]]:
    """Give old-format skills the frontmatter the standard requires.

    Lisan's original layout kept the description in schema.json and left
    SKILL.md as bare prose. The format puts name and description in the
    document itself, because that pair is what stays in context — so a skill
    whose description lives elsewhere cannot participate in progressive
    disclosure. This copies it into frontmatter without touching the prose or
    the schema, so the skill keeps working exactly as before either way.
    """
    from .skill_format import SKILL_FILE, parse_frontmatter

    root = skills_dir if skills_dir is not None else skills_root()
    changed: list[dict[str, Any]] = []
    if not root.exists():
        return changed

    for skill_dir in sorted(root.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith((".", "_", "__")):
            continue
        doc = skill_dir / SKILL_FILE
        if not doc.is_file():
            continue
        text = doc.read_text(encoding="utf-8")
        data, _ = parse_frontmatter(text)
        if data.get("name") and data.get("description"):
            continue

        description = str(data.get("description") or "")
        if not description:
            try:
                schema = json.loads((skill_dir / "schema.json").read_text(encoding="utf-8"))
                description = str(schema.get("description") or "")
            except Exception:
                description = ""
        if not description:
            changed.append({"name": skill_dir.name, "action": "skipped",
                            "reason": "no description in SKILL.md or schema.json"})
            continue

        # Single-line, quoted: descriptions routinely contain colons, which
        # would otherwise reparse as a mapping.
        safe = description.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
        front = f'---\nname: {skill_dir.name}\ndescription: "{safe}"\n---\n\n'
        if apply:
            doc.write_text(front + text.lstrip("\n"), encoding="utf-8")
        changed.append({"name": skill_dir.name, "action": "migrated" if apply else "would migrate"})
    return changed
