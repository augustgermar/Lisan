"""Re-export: the judge's single home is lisan/tools/judge.py (it became
part of the app when self-evaluation moved in-process)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisan.tools.judge import DEFAULT_JUDGE_MODEL, DEFAULT_JUDGE_PROVIDER, aggregate, judge_exchange  # noqa: E402,F401
