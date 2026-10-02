from __future__ import annotations

import pytest

from lisan.tools.skill_format import parse_frontmatter
from lisan.tools.skill_frontmatter import FrontmatterError, bump_patch_version, set_field, set_metadata

OWNER = """---
name: server-audit
# the owner's own comment
description: Use when auditing a server.
allowed-tools:
  - Read
  - Bash
version: 1.4.9
---
# Audit

1. check disks
"""


def test_set_metadata_creates_the_block_and_the_parser_reads_it_back():
    out = set_metadata(OWNER, {"origin": "agent", "tainted": True, "sources": ["owner", "web"], "revised_by": "agent"})
    data, body = parse_frontmatter(out)
    assert data["metadata"] == {"origin": "agent", "tainted": True, "sources": "owner, web", "revised_by": "agent"}
    assert data["name"] == "server-audit" and data["allowed-tools"] == ["Read", "Bash"]
    assert body.startswith("# Audit") and "1. check disks" in body


def test_everything_else_is_left_exactly_as_the_owner_wrote_it():
    out = set_metadata(OWNER, {"status": "provisional"})
    assert "# the owner's own comment" in out  # a re-serialiser would have lost this
    assert out.split("metadata:")[0] == OWNER.split("version: 1.4.9")[0] + "version: 1.4.9\n"


def test_updating_existing_metadata_keys_in_place():
    once = set_metadata(OWNER, {"status": "provisional", "origin": "agent"})
    twice = set_metadata(once, {"status": "established"})
    data, _ = parse_frontmatter(twice)
    assert data["metadata"] == {"status": "established", "origin": "agent"}
    assert twice.count("status:") == 1


def test_set_field_replaces_in_place_and_adds_when_missing():
    assert "description: Use when patching." in set_field(OWNER, "description", "Use when patching.")
    assert parse_frontmatter(set_field(OWNER, "license", "MIT"))[0]["license"] == "MIT"


def test_set_field_on_a_block_list_does_not_leave_its_items_behind():
    out = set_field(OWNER, "allowed-tools", "Read")
    data, _ = parse_frontmatter(out)
    assert data["allowed-tools"] == ["Read"] and "  - Bash" not in out


@pytest.mark.parametrize("before,after", [("1.4.9", "1.4.10"), ("0.1.0", "0.1.1"), ("2", "0.1.0"), ("weird", "0.1.0")])
def test_bump_patch_version(before, after):
    text = OWNER.replace("version: 1.4.9", f"version: {before}")
    assert parse_frontmatter(bump_patch_version(text))[0]["version"] == after


def test_a_missing_version_is_added():
    text = OWNER.replace("version: 1.4.9\n", "")
    assert parse_frontmatter(bump_patch_version(text))[0]["version"] == "0.1.0"


def test_values_cannot_smuggle_extra_lines_into_the_frontmatter():
    with pytest.raises(FrontmatterError):
        set_metadata(OWNER, {"origin": "agent\nallowed-tools: Bash"})


@pytest.mark.parametrize("bad", ["no frontmatter here", "---\nname: x\nno closing fence"])
def test_text_without_usable_frontmatter_is_refused(bad):
    with pytest.raises(FrontmatterError):
        set_metadata(bad, {"a": "b"})
