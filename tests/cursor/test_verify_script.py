"""Tests for cursor/chat-report-verify.py's offline ``--validate`` mode.

The verify script's primary mode runs chat-report.py against a real Cursor
state DB (requires per-machine config + real chat IDs). For pytest we
exercise a second mode -- ``--validate <path>`` -- which loads a JSON file
and asserts it conforms to the locked-shape contract from
``cursor/_locked_contract.py``. Exit code 0 on conform, non-zero on
violation. This gives CI a way to validate fixture JSON without touching
Cursor's filesystem state.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from . import _fixtures as fx


_VERIFY_SCRIPT = Path(__file__).resolve().parents[2] / "cursor" / "chat-report-verify.py"


def _run_verify(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_VERIFY_SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def _build_locked_single(chat_report, chat_id: str) -> dict[str, Any]:
    """Build a synthetic locked single-chat report using the same helpers as the
    existing locked-shape tests."""
    meta = fx.make_meta(chat_id=chat_id)
    ordered = fx.make_ordered(
        ("u0", fx.make_user_bubble("u0", text="hello")),
        ("a0", fx.make_assistant_text_bubble("a0", text="hi")),
        ("t0", fx.make_tool_call_bubble("t0", tool_name="read_file_v2",
                                        params={"targetFile": "src/foo.py"},
                                        result_text="contents")),
        ("t1", fx.make_tool_call_bubble("t1", tool_name="optimus_search",
                                        params={"query": "foo"},
                                        result_text="hits")),
    )
    rows = chat_report.build_bubble_rows(ordered)
    return chat_report.build_locked_report(chat_id, meta, rows, ordered)


# ---------------------------------------------------------------------------
# Good-fixture path: rc=0
# ---------------------------------------------------------------------------

def test_validate_good_single_chat_returns_zero(chat_report, tmp_path):
    report = _build_locked_single(chat_report, "12345678-1234-1234-1234-123456789abc")
    good = tmp_path / "good-single.json"
    good.write_text(json.dumps(report))

    result = _run_verify("--validate", str(good))

    assert result.returncode == 0, f"stderr={result.stderr!r} stdout={result.stdout!r}"


def test_validate_good_diff_returns_zero(chat_report, tmp_path):
    before = _build_locked_single(chat_report, "11111111-1111-1111-1111-111111111111")
    after = _build_locked_single(chat_report, "22222222-2222-2222-2222-222222222222")
    diff = chat_report.build_locked_diff_report(before, after)
    path = tmp_path / "good-diff.json"
    path.write_text(json.dumps(diff))

    result = _run_verify("--validate", str(path))

    assert result.returncode == 0, f"stderr={result.stderr!r}"


def test_validate_good_aggregate_returns_zero(chat_report, tmp_path):
    r_a = _build_locked_single(chat_report, "11111111-1111-1111-1111-111111111111")
    r_b = _build_locked_single(chat_report, "22222222-2222-2222-2222-222222222222")
    agg = chat_report.build_locked_aggregate_report([r_a, r_b])
    path = tmp_path / "good-agg.json"
    path.write_text(json.dumps(agg))

    result = _run_verify("--validate", str(path))

    assert result.returncode == 0, f"stderr={result.stderr!r}"


# ---------------------------------------------------------------------------
# Bad-fixture path: rc != 0
# ---------------------------------------------------------------------------

def test_validate_bad_tool_class_returns_nonzero(chat_report, tmp_path):
    """A report with an invalid tool_class must fail validation."""
    report = _build_locked_single(chat_report, "12345678-1234-1234-1234-123456789abc")
    # Corrupt the first tool_call's tool_class
    for turn in report["turns"]:
        if turn["tool_calls"]:
            turn["tool_calls"][0]["tool_class"] = "not-a-real-class"
            break
    bad = tmp_path / "bad-single.json"
    bad.write_text(json.dumps(report))

    result = _run_verify("--validate", str(bad))

    assert result.returncode != 0
    combined = result.stderr + result.stdout
    assert "tool_class" in combined


def test_validate_missing_required_key_returns_nonzero(chat_report, tmp_path):
    """A report missing a required top-level field must fail."""
    report = _build_locked_single(chat_report, "12345678-1234-1234-1234-123456789abc")
    report.pop("aggregates")
    bad = tmp_path / "bad-missing-key.json"
    bad.write_text(json.dumps(report))

    result = _run_verify("--validate", str(bad))

    assert result.returncode != 0


def test_validate_unparseable_json_returns_nonzero(tmp_path):
    bad = tmp_path / "garbage.json"
    bad.write_text("{not really json,,,")

    result = _run_verify("--validate", str(bad))

    assert result.returncode != 0


def test_validate_missing_file_returns_nonzero(tmp_path):
    missing = tmp_path / "does-not-exist.json"

    result = _run_verify("--validate", str(missing))

    assert result.returncode != 0
