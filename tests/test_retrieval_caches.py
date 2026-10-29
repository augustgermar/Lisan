from __future__ import annotations

import os

from lisan.frontmatter import write_markdown
import lisan.tools.retrieval  # initialize the layer/graph import cycle
from lisan.tools.retrieval_graph import _dreamer_coupled_pairs


def test_coupled_pairs_follow_report_changes(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    report = reports / "dreamer-1.md"
    write_markdown(report, {"id": "report.1"}, "Nothing interesting.")
    assert _dreamer_coupled_pairs(tmp_path) == set()
    write_markdown(report, {"id": "report.1"}, "The physical and environmental domains are coupled.")
    st = report.stat()
    os.utime(report, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert _dreamer_coupled_pairs(tmp_path)  # change is seen, not served stale
