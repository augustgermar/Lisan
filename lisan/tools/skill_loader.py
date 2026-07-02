from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


def load_skills(skills_dir: Path) -> list[dict[str, Any]]:
    """Return tool definitions for valid skill directories."""
    tools: list[dict[str, Any]] = []
    if not skills_dir.exists():
        return tools
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        schema_path = skill_dir / "schema.json"
        tool_path = skill_dir / "tool.py"
        skill_doc = skill_dir / "SKILL.md"
        if not schema_path.exists() or not tool_path.exists() or not skill_doc.exists():
            continue
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        tools.append(
            {
                "name": skill_dir.name,
                "description": schema.get("description", ""),
                "parameters": schema.get("parameters", {}),
                "handler_path": str(tool_path),
                "skill_dir": str(skill_dir),
            }
        )
    return tools


def load_skill_handlers(skills_dir: Path, *, vault: Path, config: dict[str, Any]) -> dict[str, Callable[..., str]]:
    handlers: dict[str, Callable[..., str]] = {}
    for skill in load_skills(skills_dir):
        path = Path(str(skill["handler_path"]))
        name = str(skill["name"])
        try:
            module = _import_module_from_path(path, f"lisan_skill_{name}")
        except Exception:
            continue
        run = getattr(module, "run", None)
        if not callable(run):
            continue

        def _handler(*, _run=run, **args: Any) -> str:
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
