from __future__ import annotations

import os

from lisan.frontmatter import load_frontmatter_cached, write_markdown


def test_cached_frontmatter_tracks_edits_and_is_not_shared(tmp_path):
    path = tmp_path / "r.md"
    write_markdown(path, {"id": "x.1", "tags": ["a"]}, "body")
    first = load_frontmatter_cached(path)
    first["tags"].append("mutated")
    assert load_frontmatter_cached(path)["tags"] == ["a"]
    write_markdown(path, {"id": "x.2", "tags": ["a"]}, "body")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert load_frontmatter_cached(path)["id"] == "x.2"
