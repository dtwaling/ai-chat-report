"""Claude Code variant of the chat-report tool.

Emits the locked structured-report shape (MISSION-BRIEF section 4) from
Claude Code's on-disk JSONL chat-history store. Counterpart to the cursor
variant under ``cursor/``.

The main entrypoint (``chat-report.py``) keeps its hyphenated name for CLI
ergonomics and is loaded via ``importlib`` from tests. Underscore-named
modules are normal importable members of this package.
"""
