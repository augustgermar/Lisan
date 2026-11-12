"""`lisan edit`: per-type mutability rules for hand edits to records."""
from __future__ import annotations

from pathlib import Path

import pytest

from lisan.frontmatter import load_markdown, write_markdown
from lisan.tools.editor import _split_assignment, edit_record, parse_scalar
from lisan.tools.kernel import KernelWriteError


def _record(tmp_path: Path, rel: str, body: str = "Body.\n", **fm: object) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(path, dict(fm), body)
    return path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("null", None),
        ("NULL", None),
        ("true", True),
        ("False", False),
        ("42", 42),
        ("-7", -7),
        (" 3 ", 3),
        ("1.5", 1.5),
        ('["a", "b"]', ["a", "b"]),
        ('{"k": 1}', {"k": 1}),
        ('"quoted"', "quoted"),
        ("plain text", "plain text"),
        ("2026-01-02", "2026-01-02"),
        ("-", "-"),
    ],
)
def test_parse_scalar(raw: str, expected: object) -> None:
    assert parse_scalar(raw) == expected


def test_split_assignment() -> None:
    assert _split_assignment("summary=a=b") == ("summary", "a=b")
    assert _split_assignment(" key =value") == ("key", "value")
    with pytest.raises(ValueError, match="Expected key=value"):
        _split_assignment("no-equals")
    with pytest.raises(ValueError, match="Empty field name"):
        _split_assignment("=value")


def test_set_add_and_append_on_an_entity(tmp_path: Path) -> None:
    path = _record(tmp_path, "entities/people/mara.md", type="entity", aliases=["M"], summary="old")

    assert edit_record(
        path,
        set_fields=["summary=new summary", "epoch=2", "archived=false"],
        add_fields=["aliases=Mara K", "tags=friend"],
        append_body="  Addendum.  ",
    ) == path

    doc = load_markdown(path)
    assert doc.frontmatter["summary"] == "new summary"
    assert doc.frontmatter["epoch"] == 2
    assert doc.frontmatter["archived"] is False
    assert doc.frontmatter["aliases"] == ["M", "Mara K"]
    assert doc.frontmatter["tags"] == ["friend"]
    assert doc.body.strip() == "Body.\n\nAddendum."


def test_no_changes_leaves_the_record_intact(tmp_path: Path) -> None:
    path = _record(tmp_path, "state/work.md", type="state", summary="steady")
    edit_record(path)
    doc = load_markdown(path)
    assert doc.frontmatter == {"type": "state", "summary": "steady"}
    assert doc.body.strip() == "Body."


def test_adding_to_a_non_list_field_is_refused(tmp_path: Path) -> None:
    path = _record(tmp_path, "knowledge/k.md", type="knowledge", summary="scalar")
    with pytest.raises(ValueError, match="Field summary is not a list"):
        edit_record(path, add_fields=["summary=more"])


def test_episode_allows_only_append_safe_fields(tmp_path: Path) -> None:
    path = _record(tmp_path, "episodes/2026/ep.md", type="episode", summary="old", created="2026-01-01")

    edit_record(path, set_fields=["summary=corrected", "confidence=medium"], append_body="Addendum.")
    doc = load_markdown(path)
    assert doc.frontmatter["summary"] == "corrected"
    assert doc.frontmatter["confidence"] == "medium"
    assert doc.body.strip().endswith("Addendum.")

    with pytest.raises(ValueError, match="append-safe fields; got created"):
        edit_record(path, set_fields=["created=2020-01-01"])
    with pytest.raises(ValueError, match="do not support list append"):
        edit_record(path, add_fields=["entities=person.mara"])
    assert load_markdown(path).frontmatter["created"] == "2026-01-01"


def test_evidence_metadata_is_immutable_but_body_can_be_appended(tmp_path: Path) -> None:
    path = _record(tmp_path, "evidence/notes/e.md", type="evidence", source="email")

    with pytest.raises(ValueError, match="append-corrected"):
        edit_record(path, set_fields=["source=sms"])
    with pytest.raises(ValueError, match="append-corrected"):
        edit_record(path, add_fields=["tags=x"])

    edit_record(path, append_body="Correction note.")
    doc = load_markdown(path)
    assert doc.frontmatter["source"] == "email"
    assert doc.body.strip().endswith("Correction note.")


def test_evidence_artifacts_are_fully_immutable(tmp_path: Path) -> None:
    path = _record(tmp_path, "evidence/artifacts/scan.md", type="entity", summary="s")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="Evidence artifacts are immutable"):
        edit_record(path, append_body="tamper")
    assert path.read_text(encoding="utf-8") == before


def test_unsupported_record_type_is_refused(tmp_path: Path) -> None:
    path = _record(tmp_path, "misc/p.md", type="pattern")
    with pytest.raises(ValueError, match="Unsupported record type: pattern"):
        edit_record(path, set_fields=["summary=x"])


def test_untyped_record_is_editable(tmp_path: Path) -> None:
    path = _record(tmp_path, "notes/n.md", title="t")
    edit_record(path, set_fields=["title=renamed"])
    assert load_markdown(path).frontmatter["title"] == "renamed"


def test_identity_kernel_is_off_limits(tmp_path: Path) -> None:
    kernel = tmp_path / "primer" / "identity-core.md"
    kernel.parent.mkdir(parents=True)
    kernel.write_text("---\n{}\n---\n\nkernel\n", encoding="utf-8")
    with pytest.raises(KernelWriteError):
        edit_record(kernel, append_body="rewrite")
    assert kernel.read_text(encoding="utf-8") == "---\n{}\n---\n\nkernel\n"
