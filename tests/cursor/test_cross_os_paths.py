"""Tests for per-OS default path resolution.

The cursor variant must resolve the default ``state.vscdb`` and
``ai-code-tracking.db`` paths per host OS (Windows/macOS/Linux), with the
existing ``CURSOR_STATE_DB`` / ``CURSOR_TRACKING_DB`` env-var overrides
still winning when set. Tests monkeypatch ``sys.platform`` plus the
relevant home/APPDATA env vars so resolution is deterministic and never
depends on the actual filesystem.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# state.vscdb defaults per OS
# ---------------------------------------------------------------------------

def test_default_state_db_windows(chat_report, monkeypatch):
    monkeypatch.setattr(chat_report.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\someone\AppData\Roaming")
    monkeypatch.delenv("CURSOR_STATE_DB", raising=False)

    result = chat_report._default_state_db()

    assert result == Path(r"C:\Users\someone\AppData\Roaming\Cursor\User\globalStorage\state.vscdb")


def test_default_state_db_macos(chat_report, monkeypatch):
    monkeypatch.setattr(chat_report.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", "/Users/someone")
    monkeypatch.delenv("CURSOR_STATE_DB", raising=False)

    result = chat_report._default_state_db()

    assert result == Path("/Users/someone/Library/Application Support/Cursor/User/globalStorage/state.vscdb")


def test_default_state_db_linux(chat_report, monkeypatch):
    monkeypatch.setattr(chat_report.sys, "platform", "linux")
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.delenv("CURSOR_STATE_DB", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    result = chat_report._default_state_db()

    assert result == Path("/home/someone/.config/Cursor/User/globalStorage/state.vscdb")


def test_default_state_db_env_override_wins(chat_report, monkeypatch):
    """CURSOR_STATE_DB wins over per-OS resolution on every platform."""
    monkeypatch.setattr(chat_report.sys, "platform", "linux")
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("CURSOR_STATE_DB", "/tmp/explicit.vscdb")

    result = chat_report._default_state_db()

    assert result == Path("/tmp/explicit.vscdb")


# ---------------------------------------------------------------------------
# ai-code-tracking.db defaults per OS
# ---------------------------------------------------------------------------

def test_default_tracking_db_windows(chat_report, monkeypatch):
    monkeypatch.setattr(chat_report.sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\someone")
    monkeypatch.delenv("CURSOR_TRACKING_DB", raising=False)

    result = chat_report._default_tracking_db()

    assert result == Path(r"C:\Users\someone\.cursor\ai-tracking\ai-code-tracking.db")


def test_default_tracking_db_macos(chat_report, monkeypatch):
    monkeypatch.setattr(chat_report.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", "/Users/someone")
    monkeypatch.delenv("CURSOR_TRACKING_DB", raising=False)

    result = chat_report._default_tracking_db()

    assert result == Path("/Users/someone/.cursor/ai-tracking/ai-code-tracking.db")


def test_default_tracking_db_linux(chat_report, monkeypatch):
    monkeypatch.setattr(chat_report.sys, "platform", "linux")
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.delenv("CURSOR_TRACKING_DB", raising=False)

    result = chat_report._default_tracking_db()

    assert result == Path("/home/someone/.cursor/ai-tracking/ai-code-tracking.db")


def test_default_tracking_db_env_override_wins(chat_report, monkeypatch):
    """CURSOR_TRACKING_DB wins over per-OS resolution on every platform."""
    monkeypatch.setattr(chat_report.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", "/Users/someone")
    monkeypatch.setenv("CURSOR_TRACKING_DB", "/tmp/explicit-tracking.db")

    result = chat_report._default_tracking_db()

    assert result == Path("/tmp/explicit-tracking.db")


# ---------------------------------------------------------------------------
# Unknown platform: fall back to Linux convention with a graceful default
# ---------------------------------------------------------------------------

def test_default_state_db_unknown_platform_uses_linux_convention(chat_report, monkeypatch):
    monkeypatch.setattr(chat_report.sys, "platform", "freebsd14")
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.delenv("CURSOR_STATE_DB", raising=False)

    result = chat_report._default_state_db()

    assert result == Path("/home/someone/.config/Cursor/User/globalStorage/state.vscdb")
