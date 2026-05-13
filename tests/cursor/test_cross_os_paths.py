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


# ---------------------------------------------------------------------------
# XDG_CONFIG_HOME honoring on Linux
# ---------------------------------------------------------------------------

def test_default_state_db_linux_honors_xdg_config_home(chat_report, monkeypatch):
    """When XDG_CONFIG_HOME is set on Linux, the state DB roots under it
    rather than under ``$HOME/.config``."""
    monkeypatch.setattr(chat_report.sys, "platform", "linux")
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/xdg")
    monkeypatch.delenv("CURSOR_STATE_DB", raising=False)

    result = chat_report._default_state_db()

    assert result == Path("/custom/xdg/Cursor/User/globalStorage/state.vscdb")


def test_default_state_db_linux_empty_xdg_falls_back_to_home(chat_report, monkeypatch):
    """An empty XDG_CONFIG_HOME (set but empty string) must NOT root the path
    at filesystem root; fall back to ``$HOME/.config`` instead."""
    monkeypatch.setattr(chat_report.sys, "platform", "linux")
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.delenv("CURSOR_STATE_DB", raising=False)

    result = chat_report._default_state_db()

    assert result == Path("/home/someone/.config/Cursor/User/globalStorage/state.vscdb")


# ---------------------------------------------------------------------------
# Missing home env var: clear, actionable error rather than CWD fallback
# ---------------------------------------------------------------------------

def test_home_dir_raises_when_userprofile_missing_on_windows(chat_report, monkeypatch):
    """Production sanity: USERPROFILE is OS-reserved on Windows and should
    always be set, but if it ever isn't, fail loud rather than silently
    rooting paths at CWD."""
    monkeypatch.setattr(chat_report.sys, "platform", "win32")
    monkeypatch.delenv("USERPROFILE", raising=False)

    with pytest.raises(ValueError) as exc_info:
        chat_report._home_dir()

    msg = str(exc_info.value)
    assert "USERPROFILE" in msg
    # Error message must point at the escape hatches.
    assert "CURSOR_STATE_DB" in msg or "--state-db" in msg


def test_home_dir_raises_when_home_missing_on_linux(chat_report, monkeypatch):
    """HOME can legitimately be unset in some sandboxed environments
    (e.g., Docker without an associated user). Fail loud with guidance."""
    monkeypatch.setattr(chat_report.sys, "platform", "linux")
    monkeypatch.delenv("HOME", raising=False)

    with pytest.raises(ValueError) as exc_info:
        chat_report._home_dir()

    msg = str(exc_info.value)
    assert "HOME" in msg
    assert "CURSOR_STATE_DB" in msg or "--state-db" in msg


def test_home_dir_raises_when_home_missing_on_macos(chat_report, monkeypatch):
    monkeypatch.setattr(chat_report.sys, "platform", "darwin")
    monkeypatch.delenv("HOME", raising=False)

    with pytest.raises(ValueError):
        chat_report._home_dir()


def test_default_state_db_with_explicit_override_does_not_need_home(chat_report, monkeypatch):
    """CURSOR_STATE_DB override path must work even without HOME being set,
    so the escape-hatch suggested in the error message actually works."""
    monkeypatch.setattr(chat_report.sys, "platform", "linux")
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("CURSOR_STATE_DB", "/tmp/explicit.vscdb")

    result = chat_report._default_state_db()

    assert result == Path("/tmp/explicit.vscdb")


def test_default_tracking_db_with_explicit_override_does_not_need_home(chat_report, monkeypatch):
    monkeypatch.setattr(chat_report.sys, "platform", "linux")
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("CURSOR_TRACKING_DB", "/tmp/explicit-tracking.db")

    result = chat_report._default_tracking_db()

    assert result == Path("/tmp/explicit-tracking.db")
