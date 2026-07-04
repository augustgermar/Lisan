"""Kernel mechanics: the identity kernel is enforced, not advisory.

``primer/identity-core.md`` is the invariant identity layer (Phase 2,
docs/phase2_roadmap.md WO-1). Three properties live here:

- **Write-gate.** In-process writes to the kernel path are refused unless
  they run inside a ``ceremony()`` context. Bootstrap (onboarding, eval
  seeding) and ratification are ceremonies; record fan-out, the editor,
  and agent tool calls are not.
- **Content hash.** The kernel carries a ``kernel_hash`` frontmatter line
  covering its own content (excluding the hash line itself). The ceremony
  path stamps it; ``verify_kernel`` checks it. Out-of-process edits — the
  owner's hand-edit (legitimate, v1's only kernel change path) or anything
  else — surface as a recorded drift event, never silently.
- **Voice splice.** The ratified ``## Voice`` section of the kernel body,
  when present, supersedes the authored voice in the conversation prompt,
  so identity is carried by the vault, not the prompt file, and an engine
  swap carries the voice by construction.
"""
from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

KERNEL_FILENAME = "identity-core.md"

_ceremony_active: ContextVar[bool] = ContextVar("kernel_ceremony", default=False)

_HASH_LINE_RE = re.compile(r'^kernel_hash:\s*"?([0-9a-fA-F]{64})"?\s*$', re.MULTILINE)
# Removal form: takes the trailing newline with it, so stamping and
# un-stamping round-trip to the identical canonical text.
_HASH_LINE_STRIP_RE = re.compile(r'^kernel_hash:\s*"?[0-9a-fA-F]{64}"?\s*\n?', re.MULTILINE)
_VOICE_SECTION_RE = re.compile(r"(^##\s+Voice\s*$)(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)


class KernelWriteError(PermissionError):
    """A write reached the identity kernel outside a ceremony."""


def kernel_path(vault: Path) -> Path:
    return vault / "primer" / KERNEL_FILENAME


def is_kernel_path(path: Path | str) -> bool:
    try:
        p = Path(path)
    except (TypeError, ValueError):
        return False
    return p.name == KERNEL_FILENAME and p.parent.name == "primer"


@contextmanager
def ceremony():
    """The only legitimate in-process write path to the kernel."""
    token = _ceremony_active.set(True)
    try:
        yield
    finally:
        _ceremony_active.reset(token)


def ceremony_active() -> bool:
    return _ceremony_active.get()


def guard_kernel_write(path: Path | str) -> None:
    """Refuse kernel writes outside a ceremony. Call before any file write
    that could reach the kernel path; no-op for every other path."""
    if is_kernel_path(path) and not _ceremony_active.get():
        raise KernelWriteError(
            f"{Path(path).name} is the identity kernel — off-limits to automated "
            "rewrite. Kernel changes happen through a ceremony "
            "(lisan.tools.kernel.ceremony) or the owner's own hand-edit."
        )


# ── Content hash ─────────────────────────────────────────────────────────────


def compute_kernel_hash(text: str) -> str:
    """Digest of the kernel content, excluding the ``kernel_hash`` line itself
    so stamping does not invalidate the stamp."""
    canonical = _HASH_LINE_STRIP_RE.sub("", text).strip()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stored_kernel_hash(text: str) -> str | None:
    match = _HASH_LINE_RE.search(text)
    return match.group(1).lower() if match else None


def stamp_kernel_hash(vault: Path) -> str:
    """Recompute and write the kernel content hash. Ceremony-only."""
    path = kernel_path(vault)
    guard_kernel_write(path)
    text = path.read_text(encoding="utf-8")
    digest = compute_kernel_hash(text)
    line = f'kernel_hash: "{digest}"'
    if _HASH_LINE_RE.search(text):
        new_text = _HASH_LINE_RE.sub(line, text, count=1)
    else:
        lines = text.splitlines()
        for idx, raw in enumerate(lines):
            if raw.strip() == "---":
                lines.insert(idx + 1, line)
                break
        else:
            lines = ["---", line, "---", ""] + lines
        new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    path.write_text(new_text, encoding="utf-8")
    return digest


def verify_kernel(vault: Path) -> str:
    """Deterministic tamper/drift detection: ``ok``, ``unstamped``,
    ``drift``, or ``missing``. A drift is recorded loudly (log + report),
    never swallowed — a hand-edit by the owner is legitimate, but it must
    leave a trace and be re-stamped by the next ceremony."""
    path = kernel_path(vault)
    if not path.exists():
        return "missing"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log(vault, "kernel unreadable during verification", exc)
        return "missing"
    stored = stored_kernel_hash(text)
    if stored is None:
        return "unstamped"
    actual = compute_kernel_hash(text)
    if actual == stored:
        return "ok"
    _record_drift(vault, stored=stored, actual=actual)
    return "drift"


def _record_drift(vault: Path, *, stored: str, actual: str) -> None:
    _log(
        vault,
        "kernel drift detected — content no longer matches kernel_hash",
        ValueError(f"stored={stored[:12]}… actual={actual[:12]}…"),
    )
    try:
        reports = vault / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        drift_log = reports / "kernel-drift.md"
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        entry = (
            f"- {stamp} — kernel content changed outside a ceremony "
            f"(stored `{stored[:12]}…`, actual `{actual[:12]}…`). If this was "
            "the owner's hand-edit it is legitimate; re-stamp via the next "
            "ratification ceremony. Otherwise, investigate.\n"
        )
        if drift_log.exists():
            drift_log.write_text(drift_log.read_text(encoding="utf-8") + entry, encoding="utf-8")
        else:
            drift_log.write_text("# Kernel drift events\n\n" + entry, encoding="utf-8")
    except Exception as exc:
        _log(vault, "kernel drift event could not be recorded", exc)


def _log(vault: Path, context: str, exc: Exception) -> None:
    try:
        from .log import log_error

        log_error(vault, f"kernel.{context}", exc)
    except Exception:
        pass


# ── Voice ────────────────────────────────────────────────────────────────────


def kernel_voice_block(vault: Path) -> str:
    """The ratified ``## Voice`` section body from the kernel, or ``""``.

    Verifies the kernel on every load: drift is recorded loudly but the
    voice is still honored — a hand-edited kernel is the owner's kernel.
    """
    path = kernel_path(vault)
    if not path.exists():
        return ""
    verify_kernel(vault)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _VOICE_SECTION_RE.search(text)
    if not match:
        return ""
    return match.group(2).strip()


def splice_voice(prompt_text: str, voice_body: str) -> str:
    """Replace the prompt's ``## Voice`` section body with the kernel's
    ratified voice (appending the section if the prompt has none). The
    prompt file keeps behavioral instructions; identity comes from the
    vault."""
    voice_body = voice_body.strip()
    if not voice_body:
        return prompt_text
    replacement = "\\1\n\n" + voice_body.replace("\\", "\\\\") + "\n\n"
    if _VOICE_SECTION_RE.search(prompt_text):
        return _VOICE_SECTION_RE.sub(replacement, prompt_text, count=1)
    return prompt_text.rstrip() + "\n\n## Voice\n\n" + voice_body + "\n"
