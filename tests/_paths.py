"""Put the repository root and src/ on sys.path for every test module."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

for import_root in (REPOSITORY_ROOT, REPOSITORY_ROOT / "src"):
    import_root_text = str(import_root)
    if import_root_text not in sys.path:
        sys.path.insert(0, import_root_text)
