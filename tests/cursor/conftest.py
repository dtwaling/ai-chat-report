"""Shared pytest fixtures for cursor-variant tests.

The cursor variant's main script is ``cursor/chat-report.py`` which is not
importable by name (hyphen). Tests load it via ``importlib`` and receive the
module as a session-scoped fixture.

The repo root is prepended to ``sys.path`` so the ``cursor`` package itself
is importable -- this exposes underscore-named members like
``cursor._locked_contract`` to tests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHAT_REPORT_PATH = _REPO_ROOT / "cursor" / "chat-report.py"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(scope="session")
def chat_report():
    """Load ``cursor/chat-report.py`` as a module."""
    spec = importlib.util.spec_from_file_location("chat_report", _CHAT_REPORT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load chat-report.py at {_CHAT_REPORT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["chat_report"] = module
    spec.loader.exec_module(module)
    return module
