"""Cursor-variant package marker.

The main entrypoints (`chat-report.py`, `chat-report-verify.py`) keep their
hyphenated names for CLI ergonomics and are loaded via ``importlib`` where
needed. The locked-shape validator (`_locked_contract.py`) lives in the
shared ``common`` package since it is IDE-agnostic.
"""
