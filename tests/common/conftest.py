"""sys.path bootstrap for tests/common/ -- mirrors tests/cursor/conftest.py.

Adds the repo root to ``sys.path`` so ``common._locked_contract`` is importable.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
