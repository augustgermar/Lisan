"""History, rollback, and portability for skills.

Skills are deliberately not version-controlled with the project, and the
learning loop is about to start editing them — including ones the owner wrote.
This module is what makes that safe: every change is preceded by a snapshot,
every snapshot is logged, and rolling back needs nothing but the filesystem (no
model, no healthy agent — the property the self-repair rollback also has).

Layout, inside the skills directory (the loader ignores dot-prefixed entries):

    .history/<skill>/<version_id>/    a full copy of the skill as it was
    .history/<skill>/PINNED           marker: the loop must not touch this skill
    .history/log.jsonl                append-only: what changed, why, who, on what evidence
    .archive/<skill>-<stamp>/         archived skills (never deleted)

A skill's first snapshot is labelled ``v0`` when it has no agent provenance:
that is the owner's own text, and ``diff --since-owner`` measures drift from it.
"""
from __future__ import annotations

import difflib
import fcntl
import hashlib
import io
import json
import re
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

from .skill_format import SKILL_FILE, parse_frontmatter, parse_skill_md

HISTORY_DIR = ".history"
ARCHIVE_DIR = ".archive"
LOG_FILE = "log.jsonl"
PINNED = "PINNED"

# Code that would run on the machine. Never moved by an import unless the owner
# says so, and never touched by the learning loop.
CODE_ENTRIES = ("tool.py", "schema.json", "scripts")

MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_IMPORT_MEMBERS = 500

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SkillHistoryError(ValueError):
    """A refusal with a reason the owner can read."""


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _check_name(name: str) -> str:
    if not _NAME.match(name or ""):
        raise SkillHistoryError(f"{name!r} is not a valid skill name")
    return name


def _skill_dir(skills_dir: Path, name: str) -> Path:
    path = (skills_dir / _check_name(name)).resolve()
    if skills_dir.resolve() not in path.parents:
        raise SkillHistoryError(f"{name!r} resolves outside the skills directory")
    return path


def _history_dir(skills_dir: Path, name: str) -> Path:
    return skills_dir / HISTORY_DIR / _check_name(name)


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _log(skills_dir: Path, entry: dict[str, Any]) -> None:
    path = skills_dir / HISTORY_DIR / LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(line)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_log(skills_dir: Path, name: str | None = None) -> list[dict[str, Any]]:
    path = skills_dir / HISTORY_DIR / LOG_FILE
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if name is None or entry.get("skill") == name:
            entries.append(entry)
    return entries


# ── Provenance ───────────────────────────────────────────────────────────────

def read_provenance(skills_dir: Path, name: str) -> dict[str, Any]:
    """What a skill's frontmatter says about where it came from."""
    path = _skill_dir(skills_dir, name) / SKILL_FILE
    meta: dict[str, Any] = {}
    if path.is_file():
        data, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    sources = meta.get("sources")
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    return {
        "origin": str(meta.get("origin") or "owner"),
        "revised_by": meta.get("revised_by"),
        "status": meta.get("status"),
        "created": meta.get("created"),
        "tainted": bool(meta.get("tainted")) if meta.get("tainted") is not None else None,
        "sources": sources or [],
        "pinned": is_pinned(skills_dir, name),
    }


def is_pinned(skills_dir: Path, name: str) -> bool:
    return (_history_dir(skills_dir, name) / PINNED).exists()


# ── Snapshots ────────────────────────────────────────────────────────────────

def list_history(skills_dir: Path, name: str) -> list[dict[str, Any]]:
    """Snapshots of a skill, oldest first, with their labels from the log."""
    hist = _history_dir(skills_dir, name)
    # Only the entry that CREATED a snapshot describes it. A rollback or apply entry
    # also names a version (the one it restored or replaced); letting it overwrite
    # the label made `v0` vanish after the first rollback to it.
    labels = {e.get("version_id"): e for e in read_log(skills_dir, name)
              if e.get("version_id") and e.get("action") == "snapshot"}
    out = []
    for version in sorted(p for p in hist.iterdir() if p.is_dir()) if hist.is_dir() else []:
        entry = labels.get(version.name, {})
        out.append({
            "version_id": version.name,
            "label": entry.get("label"),
            "action": entry.get("action"),
            "actor": entry.get("actor"),
            "reason": entry.get("reason"),
            "events": entry.get("events") or [],
            "hash": entry.get("hash"),
            "ts": entry.get("ts"),
        })
    return out


def snapshot_skill(
    skills_dir: Path,
    name: str,
    *,
    reason: str,
    actor: str = "owner",
    events: list[str] | None = None,
    label: str | None = None,
    action: str = "snapshot",
) -> str:
    """Copy a skill into history before it changes. Returns the version id.

    Identical to the newest snapshot → returns that id and records nothing new,
    so a no-op edit does not litter the history. The first snapshot of a skill
    with no agent provenance is labelled ``v0``: the owner's own text.
    """
    source = _skill_dir(skills_dir, name)
    if not source.is_dir():
        raise SkillHistoryError(f"no skill named {name!r}")
    digest = _tree_hash(source)
    existing = list_history(skills_dir, name)
    if existing and existing[-1].get("hash") == digest:
        return str(existing[-1]["version_id"])

    if label is None and not existing and read_provenance(skills_dir, name)["origin"] != "agent":
        label = "v0"
    hist = _history_dir(skills_dir, name)
    hist.mkdir(parents=True, exist_ok=True)
    version_id = _stamp()
    n = 1
    while (hist / version_id).exists():  # two snapshots in one second
        n += 1
        version_id = f"{_stamp()}-{n}"
    shutil.copytree(source, hist / version_id, symlinks=False)
    _log(skills_dir, {
        "skill": name, "action": action, "version_id": version_id, "label": label, "actor": actor,
        "reason": reason, "events": list(events or []), "hash": digest,
    })
    return version_id


def _resolve_version(skills_dir: Path, name: str, version: str) -> Path:
    path = _history_dir(skills_dir, name) / version
    if not path.is_dir():
        # allow the label too ("v0")
        for item in list_history(skills_dir, name):
            if item.get("label") == version:
                return _history_dir(skills_dir, name) / str(item["version_id"])
        raise SkillHistoryError(f"{name!r} has no version {version!r} (see `lisan skills history {name}`)")
    return path


def _files(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _diff_trees(old_root: Path, new_root: Path, old_label: str, new_label: str) -> str:
    old, new = _files(old_root), _files(new_root)
    chunks: list[str] = []
    for rel in sorted(set(old) | set(new)):
        a = old.get(rel, "").splitlines(keepends=True)
        b = new.get(rel, "").splitlines(keepends=True)
        if a == b:
            continue
        chunks.append("".join(difflib.unified_diff(
            a, b, fromfile=f"{old_label}/{rel}" if rel in old else "/dev/null",
            tofile=f"{new_label}/{rel}" if rel in new else "/dev/null",
        )))
    return "".join(chunks)


def diff_skill(skills_dir: Path, name: str, *, since: str | None = None) -> str:
    """Unified diff of the current skill against a snapshot.

    ``since=None`` → the newest snapshot; ``"owner"`` → ``v0``, the owner's own
    text (how far the loop has drifted from what you wrote); otherwise a
    version id or label. Empty string means no difference."""
    current = _skill_dir(skills_dir, name)
    if not current.is_dir():
        raise SkillHistoryError(f"no skill named {name!r}")
    history = list_history(skills_dir, name)
    if since == "owner":
        v0 = next((h for h in history if h.get("label") == "v0"), None)
        if v0 is None:
            return ""  # never edited by the loop: nothing to drift from
        base = _history_dir(skills_dir, name) / str(v0["version_id"])
        old_label = f"{name}@v0"
    elif since:
        base = _resolve_version(skills_dir, name, since)
        old_label = f"{name}@{base.name}"
    else:
        if not history:
            raise SkillHistoryError(f"{name!r} has no history yet")
        base = _history_dir(skills_dir, name) / str(history[-1]["version_id"])
        old_label = f"{name}@{base.name}"
    return _diff_trees(base, current, old_label, f"{name}@current")


def rollback_skill(skills_dir: Path, name: str, version: str, *, actor: str = "owner", reason: str = "") -> str:
    """Restore a skill to an earlier snapshot. The state being replaced is
    snapshotted first, so a rollback can itself be undone. Returns the id of
    the snapshot taken of the replaced state."""
    target = _resolve_version(skills_dir, name, version)
    current = _skill_dir(skills_dir, name)
    saved = snapshot_skill(
        skills_dir, name, reason=f"before rollback to {target.name}", actor=actor, label="pre-rollback", action="snapshot",
    ) if current.is_dir() else ""
    staging = Path(tempfile.mkdtemp(prefix=".rollback-", dir=skills_dir))
    try:
        shutil.copytree(target, staging / name)
        if current.exists():
            shutil.rmtree(current)
        (staging / name).rename(current)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _log(skills_dir, {
        "skill": name, "action": "rollback", "restored_version": target.name, "actor": actor,
        "reason": reason or f"rolled back to {target.name}", "replaced_snapshot": saved,
    })
    return saved


def archive_skill(skills_dir: Path, name: str, *, reason: str = "", actor: str = "owner") -> Path:
    """Take a skill out of service without destroying it."""
    source = _skill_dir(skills_dir, name)
    if not source.is_dir():
        raise SkillHistoryError(f"no skill named {name!r}")
    snapshot_skill(skills_dir, name, reason=reason or "archived", actor=actor, action="snapshot")
    dest_root = skills_dir / ARCHIVE_DIR
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / f"{name}-{_stamp()}"
    shutil.move(str(source), str(dest))
    _log(skills_dir, {"skill": name, "action": "archive", "actor": actor, "reason": reason or "archived", "archive": dest.name})
    return dest


def pin_skill(skills_dir: Path, name: str, *, pinned: bool = True, actor: str = "owner") -> None:
    """A pinned skill is one the learning loop must not change."""
    if not _skill_dir(skills_dir, name).is_dir():
        raise SkillHistoryError(f"no skill named {name!r}")
    marker = _history_dir(skills_dir, name) / PINNED
    if pinned:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("pinned\n", encoding="utf-8")
    else:
        marker.unlink(missing_ok=True)
    _log(skills_dir, {"skill": name, "action": "pin" if pinned else "unpin", "actor": actor})


# ── Export and import ────────────────────────────────────────────────────────

def export_skill(skills_dir: Path, name: str, dest: Path, *, with_history: bool = True) -> Path:
    """Pack a skill (and, by default, its history) into one .tar.gz. A skill is
    a standard directory, so this is the portable form; moving one between
    machines is an explicit act, not a background sync."""
    source = _skill_dir(skills_dir, name)
    if not source.is_dir():
        raise SkillHistoryError(f"no skill named {name!r}")
    dest = Path(dest)
    if dest.is_dir():
        dest = dest / f"{name}.skill.tar.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(source, arcname=f"{name}", filter=_plain_ownership)
        hist = _history_dir(skills_dir, name)
        if with_history and hist.is_dir():
            tar.add(hist, arcname=f"{HISTORY_DIR}/{name}")
            entries = [e for e in read_log(skills_dir, name)]
            data = ("\n".join(json.dumps(e, sort_keys=True) for e in entries) + "\n").encode()
            info = tarfile.TarInfo(f"{HISTORY_DIR}/{name}.log.jsonl")
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return dest


def _plain_ownership(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    # exported archives carry no user or group names of the machine they came from
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def import_skill(
    skills_dir: Path,
    archive: Path,
    *,
    replace: bool = False,
    allow_code: bool = False,
    actor: str = "owner",
) -> dict[str, Any]:
    """Unpack an exported skill into the skills directory.

    Hardened: members are checked before anything is extracted (no absolute
    paths, no `..`, no links or devices, bounded size and count); the skill must
    parse as a valid Agent Skill; **code never comes along unless `allow_code`**
    (an imported `tool.py` would run on this machine, so the owner must ask for
    it); and an existing skill is only replaced on request, after a snapshot.
    """
    archive = Path(archive)
    if not archive.is_file():
        raise SkillHistoryError(f"no such archive: {archive}")
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        if len(members) > MAX_IMPORT_MEMBERS:
            raise SkillHistoryError(f"archive has {len(members)} entries; limit is {MAX_IMPORT_MEMBERS}")
        if sum(m.size for m in members) > MAX_IMPORT_BYTES:
            raise SkillHistoryError("archive is larger than the import limit")
        for m in members:
            parts = Path(m.name).parts
            if m.name.startswith("/") or ".." in parts or not parts:
                raise SkillHistoryError(f"unsafe path in archive: {m.name!r}")
            if not (m.isfile() or m.isdir()):
                raise SkillHistoryError(f"archive entry {m.name!r} is a link or special file; refusing")
        tops = {Path(m.name).parts[0] for m in members}
        skill_tops = sorted(t for t in tops if t != HISTORY_DIR)
        if len(skill_tops) != 1:
            raise SkillHistoryError("archive must contain exactly one skill directory")
        name = _check_name(skill_tops[0])

        with tempfile.TemporaryDirectory(prefix="lisan-import-") as tmp:
            tmp_root = Path(tmp)
            tar.extractall(tmp_root, members=members, filter="data")
            extracted = tmp_root / name
            manifest = parse_skill_md(extracted / SKILL_FILE) if (extracted / SKILL_FILE).is_file() else None
            if manifest is None:
                raise SkillHistoryError(f"{name!r} has no {SKILL_FILE}")
            if manifest.errors:
                raise SkillHistoryError("not a valid skill: " + "; ".join(str(e) for e in manifest.errors))

            stripped: list[str] = []
            if not allow_code:
                for entry in CODE_ENTRIES:
                    target = extracted / entry
                    if target.is_dir():
                        shutil.rmtree(target)
                        stripped.append(entry + "/")
                    elif target.exists():
                        target.unlink()
                        stripped.append(entry)

            destination = _skill_dir(skills_dir, name)
            skills_dir.mkdir(parents=True, exist_ok=True)
            replaced = destination.exists()
            if destination.exists():
                if not replace:
                    raise SkillHistoryError(f"{name!r} is already installed; use --replace to overwrite it (it is snapshotted first)")
                snapshot_skill(skills_dir, name, reason="before import replaced it", actor=actor, label="pre-import")
                shutil.rmtree(destination)
            shutil.copytree(extracted, destination)

            carried = 0
            hist_src = tmp_root / HISTORY_DIR / name
            if hist_src.is_dir():
                hist_dst = _history_dir(skills_dir, name)
                hist_dst.mkdir(parents=True, exist_ok=True)
                carried_ids: set[str] = set()
                for version in hist_src.iterdir():
                    if version.is_dir() and not (hist_dst / version.name).exists():
                        shutil.copytree(version, hist_dst / version.name)
                        carried_ids.add(version.name)
                        carried += 1
                # keep the log entries that explain those versions (their labels,
                # reasons and evidence), original timestamps intact
                log_src = tmp_root / HISTORY_DIR / f"{name}.log.jsonl"
                if log_src.is_file():
                    for line in log_src.read_text(encoding="utf-8").splitlines():
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        if entry.get("version_id") in carried_ids:
                            _log(skills_dir, {**entry, "imported": True})
            _log(skills_dir, {
                "skill": name, "action": "import", "actor": actor, "reason": f"imported from {archive.name}",
                "stripped_code": stripped, "history_versions_carried": carried,
            })
    return {"name": name, "stripped_code": stripped, "history_versions": carried, "replaced": replaced}
