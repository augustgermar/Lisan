"""WO-1: the identity kernel is enforced, not advisory.

Gate: in-process writes to primer/identity-core.md are refused outside a
ceremony. Hash: the kernel content is stamped and drift is detected and
recorded. Voice: a ratified kernel voice supersedes the authored prompt
voice; no kernel voice leaves the prompt untouched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lisan.frontmatter import write_markdown
from lisan.tools.kernel import (
    KernelWriteError,
    ceremony,
    compute_kernel_hash,
    kernel_path,
    kernel_voice_block,
    splice_voice,
    stamp_kernel_hash,
    stored_kernel_hash,
    verify_kernel,
)

_KERNEL_TEXT = '''---
principal:
  name: "Ruth"
  aliases: ["Ruth"]
assistant:
  name: "Vega"
  aliases: ["Vega"]
deixis_frame: |
  I / me / Vega = the assistant.
---

# Identity Core (invariant)

The principal is **Ruth**.

## Voice

- Terse and dry. One clean answer.
- Never flags a joke.
'''


def _seed_kernel(vault: Path, text: str = _KERNEL_TEXT) -> Path:
    path = kernel_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ceremony():
        path.write_text(text, encoding="utf-8")
        stamp_kernel_hash(vault)
    return path


# ── Write gate ───────────────────────────────────────────────────────────────


def test_write_markdown_refuses_kernel_path_outside_ceremony(tmp_path):
    target = tmp_path / "primer" / "identity-core.md"
    with pytest.raises(KernelWriteError):
        write_markdown(target, {"type": "entity"}, "body")
    assert not target.exists()


def test_write_markdown_allows_kernel_path_inside_ceremony(tmp_path):
    target = tmp_path / "primer" / "identity-core.md"
    with ceremony():
        write_markdown(target, {"type": "entity"}, "body")
    assert target.exists()


def test_editor_cannot_reach_the_kernel(tmp_path):
    from lisan.tools.editor import edit_record

    path = _seed_kernel(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(KernelWriteError):
        edit_record(path, set_fields=["status=archived"])
    assert path.read_text(encoding="utf-8") == before


def test_non_kernel_writes_are_untouched(tmp_path):
    target = tmp_path / "entities" / "identity-core.md"  # wrong dir: not the kernel
    write_markdown(target, {"type": "entity"}, "body")
    assert target.exists()
    other = tmp_path / "primer" / "capabilities.md"
    other.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(other, {"type": "report"}, "body")
    assert other.exists()


def test_stamp_refuses_outside_ceremony(tmp_path):
    _seed_kernel(tmp_path)
    with pytest.raises(KernelWriteError):
        stamp_kernel_hash(tmp_path)


# ── Hash and drift ───────────────────────────────────────────────────────────


def test_stamped_kernel_verifies_ok(tmp_path):
    _seed_kernel(tmp_path)
    assert verify_kernel(tmp_path) == "ok"


def test_stamp_is_idempotent_and_self_excluding(tmp_path):
    path = _seed_kernel(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert stored_kernel_hash(text) == compute_kernel_hash(text)
    with ceremony():
        second = stamp_kernel_hash(tmp_path)
    assert stored_kernel_hash(path.read_text(encoding="utf-8")) == second
    assert verify_kernel(tmp_path) == "ok"


def test_hand_edit_is_detected_and_recorded(tmp_path):
    path = _seed_kernel(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace("Terse and dry", "Florid and long"),
        encoding="utf-8",
    )
    assert verify_kernel(tmp_path) == "drift"
    drift_log = tmp_path / "reports" / "kernel-drift.md"
    assert drift_log.exists()
    assert "outside a ceremony" in drift_log.read_text(encoding="utf-8")


def test_unstamped_and_missing_states(tmp_path):
    assert verify_kernel(tmp_path) == "missing"
    path = kernel_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ceremony():
        path.write_text(_KERNEL_TEXT, encoding="utf-8")
    assert verify_kernel(tmp_path) == "unstamped"


def test_restamp_after_owner_edit_clears_drift(tmp_path):
    path = _seed_kernel(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + "\n- Addendum.\n", encoding="utf-8")
    assert verify_kernel(tmp_path) == "drift"
    with ceremony():
        stamp_kernel_hash(tmp_path)
    assert verify_kernel(tmp_path) == "ok"


# ── Voice ────────────────────────────────────────────────────────────────────


def test_voice_block_extracted_from_kernel(tmp_path):
    _seed_kernel(tmp_path)
    voice = kernel_voice_block(tmp_path)
    assert "Terse and dry" in voice
    assert "Identity Core" not in voice


def test_voice_block_empty_when_kernel_has_no_voice_section(tmp_path):
    _seed_kernel(tmp_path, _KERNEL_TEXT.split("## Voice")[0])
    assert kernel_voice_block(tmp_path) == ""


def test_splice_replaces_authored_voice_section():
    prompt = "# P\n\n## Voice\n\n- Plainspoken, warm.\n\n## Acting\n\nRules.\n"
    out = splice_voice(prompt, "- Terse and dry.")
    assert "Terse and dry" in out
    assert "Plainspoken, warm" not in out
    assert "## Acting\n\nRules." in out


def test_splice_appends_when_prompt_has_no_voice_section():
    out = splice_voice("# P\n\n## Acting\n\nRules.\n", "- Terse and dry.")
    assert out.rstrip().endswith("- Terse and dry.")
    assert "## Voice" in out


def test_conversation_prompt_carries_kernel_voice(tmp_path):
    from lisan.agents.conversation import ConversationAgent

    _seed_kernel(tmp_path)
    agent = ConversationAgent(vault=tmp_path)
    rendered = agent.prompt()
    assert "Terse and dry" in rendered
    assert "Plainspoken, warm" not in rendered


def test_conversation_prompt_unchanged_without_kernel_voice(tmp_path):
    from lisan.agents.conversation import ConversationAgent

    agent = ConversationAgent(vault=tmp_path)  # no kernel at all
    rendered = agent.prompt()
    assert "Plainspoken, warm" in rendered


# ── Bootstrap is the founding ceremony ──────────────────────────────────────


def test_onboarding_bootstrap_writes_and_stamps_kernel(tmp_path):
    from lisan.tools.onboarding import _write_identity_core

    path = tmp_path / "primer" / "identity-core.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_identity_core(path, name="Ruth")
    assert path.exists()
    assert verify_kernel(tmp_path) == "ok"


def test_eval_seed_writes_and_stamps_kernel(tmp_path):
    from lisan.tools.eval_seed import seed_eval_primer

    seed_eval_primer(
        tmp_path,
        principal_name="Ruth",
        roster_entries=[{"name": "Feld", "kind": "person"}],
    )
    assert verify_kernel(tmp_path) == "ok"
    text = kernel_path(tmp_path).read_text(encoding="utf-8")
    assert "Feld" in text
