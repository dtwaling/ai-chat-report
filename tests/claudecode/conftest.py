"""Shared pytest fixtures for claudecode-variant tests.

The claudecode variant's main script will be ``claudecode/chat-report.py``
which is not importable by name (hyphen). Tests will load it via
``importlib`` and receive the module as a session-scoped fixture.

Repo root is prepended to ``sys.path`` so the ``claudecode`` and ``common``
packages are importable.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
