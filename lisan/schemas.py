from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .paths import schemas_dir


@lru_cache(maxsize=1)
def load_schemas(base: Path | None = None) -> dict[str, dict[str, Any]]:
    root = base or schemas_dir()
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.schema.json")):
        schemas[path.stem.replace(".schema", "")] = json.loads(path.read_text(encoding="utf-8"))
    return schemas

