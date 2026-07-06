"""Re-export: the rubric's single home is lisan/tools/rubric.py (it became
part of the app when self-evaluation moved in-process)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisan.tools.rubric import GLOBAL_DIMENSIONS, rubric_from_kernel  # noqa: E402,F401
