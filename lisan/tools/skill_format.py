"""The Agent Skills format: ``SKILL.md`` with YAML frontmatter.

The convention every agentic system converged on — Claude Code, Codex, and the
rest — is a directory holding a ``SKILL.md`` whose YAML frontmatter carries at
minimum a ``name`` and a ``description``, followed by markdown instructions,
beside optional supporting files (``references/``, ``scripts/``, ``examples/``).
Measured against the 32 skills installed on this machine: ``name`` and
``description`` appear in all 32, then ``version`` (13), ``allowed-tools`` (8),
``user-invocable`` (7), ``license`` (1).

The load-bearing idea is **progressive disclosure**, and it is the reason a
skill is not simply a tool definition. Only ``name`` and ``description`` sit in
context permanently — enough for the model to decide a skill is relevant. The
body loads when the skill is actually invoked, and anything under
``references/`` loads only if the body sends the reader there. A hundred
installed skills therefore cost a hundred one-line descriptions rather than a
hundred full documents, which is what makes a large catalogue affordable at
all.

Lisan's older format put the description and JSON-Schema parameters in a
``schema.json`` beside a ``tool.py``, and the loader *required* all three — so
a skill written to the standard, with instructions and no Python entry point,
was skipped in silence. Both are supported now: frontmatter wins where present,
schema.json fills gaps for un-migrated skills, and a skill needs only SKILL.md
to exist.

The parser is a deliberate YAML subset, hand-rolled against the stdlib. PyYAML
is not a declared dependency — ``ingest.py`` reaches for it behind a try/except
with the note "present in most installs via transitive deps" — and skill
discovery is a core path that must not degrade because an optional package is
missing. Frontmatter here is flat keys, short scalars, and the occasional list;
anything more exotic belongs in the body.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKILL_FILE = "SKILL.md"

# Fields the standard treats as lists, whether written inline ("a, b") or as a
# YAML block sequence. Everything else stays the scalar it was written as.
_LIST_FIELDS = {"allowed-tools", "allowed_tools", "tools", "keywords"}
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}

_NAME_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")


@dataclass
class SkillManifest:
    """What a SKILL.md declares, plus where it came from."""

    name: str
    description: str
    body: str = ""
    version: str | None = None
    license: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    user_invocable: bool = True
    model_invocable: bool = True
    argument_hint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def _coerce(value: str) -> Any:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return text


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``---`` frontmatter from the body. Returns ({}, text) when absent.

    Tolerant on purpose: a skill with malformed frontmatter should surface as a
    validation error the owner can read, not as an exception during discovery
    that takes the whole catalogue down with it.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return {}, text

    data: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[Any] | None = None
    nested_key: str | None = None

    for raw in lines[1:end]:
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        stripped = line.strip()
        indented = line[:1].isspace()

        if stripped.startswith("- ") and current_list is not None:
            current_list.append(_coerce(stripped[2:]))
            continue

        if ":" not in stripped:
            continue
        key, _, rest = stripped.partition(":")
        key = key.strip()
        rest = rest.strip()

        if indented and nested_key:
            # one level of nesting, which is all `metadata:` ever needs
            bucket = data.setdefault(nested_key, {})
            if isinstance(bucket, dict):
                bucket[key] = _coerce(rest)
            continue

        current_key, current_list, nested_key = key, None, None
        if rest == "":
            # Either a block list or a nested mapping; decided by what follows.
            data[key] = []
            current_list = data[key]
            nested_key = key
            continue
        value = _coerce(rest)
        if key in _LIST_FIELDS and isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        data[key] = value

    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return data, body


def parse_skill_md(path: Path) -> SkillManifest:
    """Read one SKILL.md into a manifest, collecting errors rather than raising."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return SkillManifest(name=path.parent.name, description="", path=path,
                             errors=[f"unreadable: {exc}"])

    data, body = parse_frontmatter(text)
    errors: list[str] = []

    name = str(data.get("name") or "").strip()
    if not name:
        # A skill whose frontmatter omits the name still loads under its
        # directory name — the directory is how every caller already refers to
        # it — but the omission is reported.
        name = path.parent.name
        errors.append("frontmatter is missing 'name'")
    elif not _NAME_RE.match(name):
        errors.append(
            f"name {name!r} should be lowercase words joined by - or _"
        )

    description = str(data.get("description") or "").strip()
    if not description:
        errors.append("frontmatter is missing 'description' (this is what the model reads to decide relevance)")

    allowed = data.get("allowed-tools", data.get("allowed_tools", data.get("tools", [])))
    if isinstance(allowed, str):
        allowed = [part.strip() for part in allowed.split(",") if part.strip()]
    if not isinstance(allowed, list):
        allowed = []

    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    disabled = data.get("disable-model-invocation", data.get("disable_model_invocation", False))

    return SkillManifest(
        name=name,
        description=description,
        body=body.strip(),
        version=(str(data["version"]) if data.get("version") is not None else None),
        license=(str(data["license"]) if data.get("license") is not None else None),
        allowed_tools=[str(t) for t in allowed],
        user_invocable=bool(data.get("user-invocable", data.get("user_invocable", True))),
        model_invocable=not bool(disabled),
        argument_hint=(str(data["argument-hint"]) if data.get("argument-hint") is not None else None),
        metadata=metadata,
        path=path,
        errors=errors,
    )


def supporting_files(skill_dir: Path, *, limit: int = 60) -> list[str]:
    """Relative paths of a skill's supporting files, for progressive disclosure.

    Listed rather than loaded: the point of the format is that the body decides
    what is worth reading, and the reader fetches it only then.
    """
    out: list[str] = []
    if not skill_dir.is_dir():
        return out
    for candidate in sorted(skill_dir.rglob("*")):
        if not candidate.is_file():
            continue
        rel = candidate.relative_to(skill_dir)
        if rel.name == SKILL_FILE or any(part.startswith((".", "__")) for part in rel.parts):
            continue
        out.append(str(rel))
        if len(out) >= limit:
            break
    return out


def render_skill(manifest: SkillManifest, skill_dir: Path) -> str:
    """The text handed back when a skill is actually invoked."""
    header = f"# Skill: {manifest.name}"
    if manifest.version:
        header += f"  (v{manifest.version})"
    parts = [header, "", manifest.description, ""]
    if manifest.body:
        parts += [manifest.body, ""]
    files = supporting_files(skill_dir)
    if files:
        parts.append(f"Supporting files in {skill_dir} — read only what you need:")
        parts += [f"  - {name}" for name in files]
    return "\n".join(parts).strip()
