"""Tests for claudecode/chat-report-verify.py's offline ``--validate`` mode.

Mirrors the cursor variant's ``tests/cursor/test_verify_script.py`` shape
test-for-test, adapted for the claudecode build path: synthetic JSONL
records -> ``build_locked_report`` -> JSON on disk -> verify subprocess.

The verify script's primary mode runs chat-report.py against a real
claudecode JSONL store under ``~/.claude/projects/`` (requires real
session UUIDs on the local machine). For pytest we exercise the second
mode -- ``--validate <path>`` -- which loads a JSON file and asserts it
conforms to the locked-shape contract in ``common/_locked_contract.py``.
Exit code 0 on conform, non-zero on violation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from common._diff_aggregate import (
    build_locked_aggregate_report,
    build_locked_diff_report,
)

from . import _fixtures as fx


_VERIFY_SCRIPT = (
    Path(__file__).resolve().parents[2] / "claudecode" / "chat-report-verify.py"
)


def _run_verify(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_VERIFY_SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def _minimal_records() -> list[dict[str, Any]]:
    """Smallest record stream that exercises both read + optimus tool classes."""
    return [
        fx.user_typed_string("explore + search"),
        fx.assistant_tool_use("Read", tool_input={"file_path": "a.py"},
                              tool_use_id="t1"),
        fx.user_tool_result("t1", "contents-a"),
        fx.assistant_tool_use("mcp__optimus__optimus_search",
                              tool_input={"query": "foo"}, tool_use_id="t2"),
        fx.user_tool_result("t2", "hits"),
    ]


def _build_single(claudecode_chat_report, session_id: str) -> dict[str, Any]:
    records = [{**r, "sessionId": session_id} for r in _minimal_records()]
    return claudecode_chat_report.build_locked_report(
        records, session_id_override=session_id,
    )


# ---------------------------------------------------------------------------
# Good-fixture path: rc=0
# ---------------------------------------------------------------------------

def test_validate_good_single_chat_returns_zero(claudecode_chat_report, tmp_path):
    report = _build_single(
        claudecode_chat_report, "12345678-1234-1234-1234-123456789abc",
    )
    good = tmp_path / "good-single.json"
    good.write_text(json.dumps(report))

    result = _run_verify("--validate", str(good))

    assert result.returncode == 0, (
        f"stderr={result.stderr!r} stdout={result.stdout!r}"
    )


def test_validate_good_diff_returns_zero(claudecode_chat_report, tmp_path):
    before = _build_single(
        claudecode_chat_report, "11111111-1111-1111-1111-111111111111",
    )
    after = _build_single(
        claudecode_chat_report, "22222222-2222-2222-2222-222222222222",
    )
    diff = build_locked_diff_report(before, after)
    path = tmp_path / "good-diff.json"
    path.write_text(json.dumps(diff))

    result = _run_verify("--validate", str(path))

    assert result.returncode == 0, f"stderr={result.stderr!r}"


def test_validate_good_aggregate_returns_zero(claudecode_chat_report, tmp_path):
    r_a = _build_single(
        claudecode_chat_report, "11111111-1111-1111-1111-111111111111",
    )
    r_b = _build_single(
        claudecode_chat_report, "22222222-2222-2222-2222-222222222222",
    )
    agg = build_locked_aggregate_report([r_a, r_b])
    path = tmp_path / "good-agg.json"
    path.write_text(json.dumps(agg))

    result = _run_verify("--validate", str(path))

    assert result.returncode == 0, f"stderr={result.stderr!r}"


# ---------------------------------------------------------------------------
# Bad-fixture path: rc != 0
# ---------------------------------------------------------------------------

def test_validate_bad_tool_class_returns_nonzero(claudecode_chat_report, tmp_path):
    """A report with an invalid tool_class must fail validation."""
    report = _build_single(
        claudecode_chat_report, "12345678-1234-1234-1234-123456789abc",
    )
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


def test_validate_missing_required_key_returns_nonzero(
    claudecode_chat_report, tmp_path,
):
    """A report missing a required top-level field must fail."""
    report = _build_single(
        claudecode_chat_report, "12345678-1234-1234-1234-123456789abc",
    )
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


def test_validate_unknown_report_kind_returns_nonzero(tmp_path):
    """A JSON file with no recognized report_kind must fail with a clear error."""
    bad = tmp_path / "unknown-kind.json"
    bad.write_text(json.dumps({"report_kind": "not-a-real-kind"}))

    result = _run_verify("--validate", str(bad))

    assert result.returncode != 0
    combined = result.stderr + result.stdout
    assert "report_kind" in combined


# ---------------------------------------------------------------------------
# Integration mode wiring (subprocess-spawns chat-report.py + validates output)
# ---------------------------------------------------------------------------

def _write_synthetic_session(tmp_path: Path, sid: str) -> Path:
    """Write a minimal claudecode JSONL session and return its path."""
    records = [{**r, "sessionId": sid} for r in _minimal_records()]
    jsonl = tmp_path / f"{sid}.jsonl"
    fx.write_jsonl(jsonl, records)
    return jsonl


def _run_verify_with_config(config_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_VERIFY_SCRIPT), "--config", str(config_path), *args],
        capture_output=True,
        text=True,
    )


def test_integration_no_config_returns_nonzero(tmp_path):
    """Default mode (no --validate) with missing config emits a helpful error."""
    missing_cfg = tmp_path / "does-not-exist.json"

    result = _run_verify_with_config(missing_cfg)

    assert result.returncode != 0
    combined = (result.stderr + result.stdout).lower()
    # Must be the "config not found" error path, not an argparse usage error
    assert "config not found" in combined or "config missing" in combined, combined


def test_integration_good_synthetic_config_passes(tmp_path):
    """End-to-end: synthetic JSONLs + config -> chat-report -> contract checks pass."""
    sid_a = "11111111-1111-1111-1111-111111111111"
    sid_b = "22222222-2222-2222-2222-222222222222"
    jsonl_a = _write_synthetic_session(tmp_path, sid_a)
    jsonl_b = _write_synthetic_session(tmp_path, sid_b)

    cfg = {
        "sessionId": sid_a,
        "sessionJsonl": str(jsonl_a),
        "sessionIdsForAggregate": [sid_a, sid_b],
        "sessionJsonlsForAggregate": [str(jsonl_a), str(jsonl_b)],
    }
    cfg_path = tmp_path / "verify.config.json"
    cfg_path.write_text(json.dumps(cfg))

    result = _run_verify_with_config(cfg_path)

    assert result.returncode == 0, (
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    # Output should report the integration run summary
    combined = result.stderr + result.stdout
    assert "passed" in combined.lower()
    # No "FAIL" lines should appear in the output
    assert "[FAIL]" not in combined, combined


def test_integration_reports_failure_on_corrupted_jsonl(tmp_path):
    """A config pointing at a non-existent JSONL surfaces as a failed check.

    The verify script does not crash; it records the failure and exits non-zero.
    """
    sid = "12345678-1234-1234-1234-123456789abc"
    cfg = {
        "sessionId": sid,
        "sessionJsonl": str(tmp_path / "does-not-exist.jsonl"),
    }
    cfg_path = tmp_path / "verify.config.json"
    cfg_path.write_text(json.dumps(cfg))

    result = _run_verify_with_config(cfg_path)

    assert result.returncode != 0
    combined = result.stderr + result.stdout
    assert "[FAIL]" in combined or "FAIL" in combined
