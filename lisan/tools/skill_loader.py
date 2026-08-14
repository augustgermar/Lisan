from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


def load_skills(skills_dir: Path) -> list[dict[str, Any]]:
    """Every skill in a directory, in either supported format.

    A skill needs **only** ``SKILL.md``. That is the whole change from the
    original loader, which required schema.json *and* tool.py *and* SKILL.md
    and skipped anything else without a word — so a skill written to the
    industry format, instructions plus a ``references/`` folder and no Python
    entry point, was invisible. Silence on a skill the owner installed
    deliberately is the same failure this codebase keeps finding elsewhere.

    Two kinds come back, and callers care about the difference:

    - **executable** (``schema.json`` + ``tool.py``): a callable tool with
      JSON-Schema parameters, exposed to the model as a function.
    - **instructional** (SKILL.md alone): loaded on demand through the ``skill``
      tool, which is what progressive disclosure means in practice.

    A skill can be both. Frontmatter is the source of truth for name and
    description; schema.json fills either gap for skills written before the
    format existed, so nothing that worked yesterday stops working.
    """
    from .skill_format import SKILL_FILE, parse_skill_md

    skills: list[dict[str, Any]] = []
    if not skills_dir.exists():
        return skills

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith((".", "_", "__")):
            # Leading underscore marks a shared-code package (_google_common),
            # not a skill; it has no SKILL.md and never did.
            continue
        skill_doc = skill_dir / SKILL_FILE
        if not skill_doc.is_file():
            continue

        manifest = parse_skill_md(skill_doc)
        schema: dict[str, Any] = {}
        schema_path = skill_dir / "schema.json"
        if schema_path.is_file():
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
            except Exception as exc:
                manifest.errors.append(f"schema.json is not valid JSON: {exc}")

        tool_path = skill_dir / "tool.py"
        executable = bool(schema) and tool_path.is_file()

        description = manifest.description or str(schema.get("description") or "")
        if not description:
            description = f"(no description) skill in {skill_dir.name}"

        skills.append(
            {
                "name": manifest.name or skill_dir.name,
                "description": description,
                "parameters": schema.get("parameters", {}),
                "requires_approval": bool(schema.get("requires_approval", False)),
                "handler_path": str(tool_path) if executable else "",
                "skill_dir": str(skill_dir),
                "executable": executable,
                "version": manifest.version,
                "allowed_tools": manifest.allowed_tools,
                "user_invocable": manifest.user_invocable,
                "model_invocable": manifest.model_invocable,
                "has_frontmatter": bool(manifest.description or manifest.version or manifest.allowed_tools),
                "errors": list(manifest.errors),
            }
        )
    return skills


def load_skill_manifest(skills_dir: Path, name: str) -> dict[str, Any] | None:
    """One skill by name, or None. Used by the on-demand ``skill`` tool."""
    for skill in load_skills(skills_dir):
        if skill["name"] == name or Path(skill["skill_dir"]).name == name:
            return skill
    return None


def render_skill_body(skill: dict[str, Any]) -> str:
    """The instructions for an invoked skill, plus what else it ships."""
    from .skill_format import parse_skill_md, render_skill

    skill_dir = Path(skill["skill_dir"])
    manifest = parse_skill_md(skill_dir / "SKILL.md")
    return render_skill(manifest, skill_dir)


def load_skill_handlers(
    skills_dir: Path,
    *,
    vault: Path,
    config: dict[str, Any],
    approval_fn: Callable[[str, dict[str, Any]], bool] | None = None,
) -> dict[str, Callable[..., str]]:
    handlers: dict[str, Callable[..., str]] = {}
    for skill in load_skills(skills_dir):
        if not skill.get("executable"):
            # Instructional skills have no callable entry point; they are
            # reached through the `skill` tool, not registered as functions.
            continue
        path = Path(str(skill["handler_path"]))
        name = str(skill["name"])
        try:
            module = _import_module_from_path(path, f"lisan_skill_{name}")
        except Exception:
            continue
        run = getattr(module, "run", None)
        if not callable(run):
            continue

        # A skill declares `"requires_approval": true` in schema.json when its
        # action leaves the machine (send a message, post, delete). The gate
        # runs at call time with the resolved arguments, same contract as
        # run_codex: approval_fn(tool_name, args) -> bool.
        gated = bool(skill.get("requires_approval"))

        def _handler(*, _run=run, _name=name, _gated=gated, **args: Any) -> str:
            if _gated:
                if approval_fn is None:
                    return (
                        f"Approval required to run {_name}, but no approval channel is "
                        "available in this context, so I did not run it."
                    )
                summary = json.dumps(args, ensure_ascii=True, sort_keys=True)
                if not approval_fn(_name, {"task": f"{_name} {summary}", **args}):
                    return (
                        f"Approval was not granted, so I did not run {_name}. This is the "
                        "approval gate — not a permissions or system error."
                    )
            return str(_run(args, vault, config))

        handlers[name] = _handler
    return handlers


def _import_module_from_path(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load skill module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
