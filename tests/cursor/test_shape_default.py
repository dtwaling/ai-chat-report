"""Tests for the --shape default flip and legacy deprecation warning.

Per PR-A2 commit 7: the locked shape is the default; ``--shape=legacy``
still works but emits a DeprecationWarning to stderr so consumers get
a single migration nudge.
"""

from __future__ import annotations

import pytest


def test_shape_default_is_locked(chat_report):
    """argparse default for --shape must be 'locked' after PR-A2."""
    ap = chat_report._build_argparser()
    args = ap.parse_args(["some-uuid"])
    assert args.shape == "locked"


def test_shape_legacy_still_accepted(chat_report):
    """Explicit --shape=legacy must still parse cleanly (deprecation, not removal)."""
    ap = chat_report._build_argparser()
    args = ap.parse_args(["--shape", "legacy", "some-uuid"])
    assert args.shape == "legacy"


def test_shape_locked_explicit_still_works(chat_report):
    """Explicit --shape=locked is identical to the new default."""
    ap = chat_report._build_argparser()
    args = ap.parse_args(["--shape", "locked", "some-uuid"])
    assert args.shape == "locked"


# ---------------------------------------------------------------------------
# Deprecation warning is emitted exactly once when --shape=legacy is used
# ---------------------------------------------------------------------------

def test_legacy_shape_emits_deprecation_warning(chat_report, capsys):
    """Calling --shape=legacy must print a DeprecationWarning to stderr."""
    chat_report._warn_if_legacy_shape("legacy")
    captured = capsys.readouterr()
    assert "DeprecationWarning" in captured.err
    assert "--shape=legacy" in captured.err
    assert "removed in a future release" in captured.err


def test_locked_shape_emits_no_warning(chat_report, capsys):
    """Default (locked) path must stay silent."""
    chat_report._warn_if_legacy_shape("locked")
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""
