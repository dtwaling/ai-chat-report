"""Cursor-variant package marker.

The main entrypoints (`chat-report.py`, `chat-report-verify.py`) keep their
hyphenated names for CLI ergonomics and are loaded via ``importlib`` where
needed. Underscore-named modules (`_locked_contract.py`) are normal
importable members of this package.
"""
