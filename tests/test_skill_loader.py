from __future__ import annotations

import json
from pathlib import Path

from lisan.tools.skill_loader import load_skill_handlers, load_skills


def test_skill_loader_discovers_valid_skill(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill = skills_dir / "hello_skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Hello Skill\n", encoding="utf-8")
    (skill / "schema.json").write_text(
        json.dumps(
            {
                "description": "Say hello",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            }
        ),
        encoding="utf-8",
    )
    (skill / "tool.py").write_text(
        "from __future__ import annotations\n"
        "def run(args, vault, config):\n"
        "    return f'hello {args[\"name\"]}'\n",
        encoding="utf-8",
    )

    tools = load_skills(skills_dir)
    assert tools[0]["name"] == "hello_skill"
    assert tools[0]["description"] == "Say hello"

    handlers = load_skill_handlers(skills_dir, vault=tmp_path, config={})
    assert handlers["hello_skill"](name="world") == "hello world"


def test_skill_loader_skips_invalid_skill(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    bad_skill = skills_dir / "broken_skill"
    bad_skill.mkdir(parents=True)
    (bad_skill / "schema.json").write_text("{}", encoding="utf-8")

    assert load_skills(skills_dir) == []
    assert load_skill_handlers(skills_dir, vault=tmp_path, config={}) == {}
