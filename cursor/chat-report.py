"""Backward-compatible CLI shim for AI Chat Report v2.0."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv: list[str] | None = None) -> int:
    """Forward CLI execution to chat_report.cli.main."""
    return importlib.import_module("chat_report.cli").main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(importlib.import_module("chat_report.cli").main(sys.argv[1:]))
