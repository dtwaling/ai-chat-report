"""Unit tests for claudecode/_report.py.

Covers the builder helpers (timestamps, summarization, status detection,
turn building) and the top-level build_locked_report assembly. End-to-end
contract conformance + CLI tests live in test_contract_conformance.py
and test_cli.py respectively.
"""

from __future__ import annotations

import math

import pytest

from claudecode import _report as R

from . import _fixtures as fx


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_suffix():
    got = R._parse_iso("2026-05-13T01:20:28.612Z")
    assert got is not None
    assert got.year == 2026 and got.month == 5 and got.day == 13


def test_parse_iso_returns_none_on_garbage():
    assert R._parse_iso("not a timestamp") is None
    assert R._parse_iso("") is None
    assert R._parse_iso(None) is None


def test_elapsed_ms_between_basic():
    a = "2026-05-13T01:20:28.000Z"
    b = "2026-05-13T01:20:28.500Z"
    assert R._elapsed_ms_between(a, b) == 500


def test_elapsed_ms_between_negative_returns_none():
    a = "2026-05-13T01:20:30.000Z"
    b = "2026-05-13T01:20:29.000Z"
    assert R._elapsed_ms_between(a, b) is None


def test_elapsed_ms_between_missing_returns_none():
    assert R._elapsed_ms_between(None, "2026-05-13T01:20:00.000Z") is None
    assert R._elapsed_ms_between("", "") is None


# ---------------------------------------------------------------------------
# Input / output summarization
# ---------------------------------------------------------------------------


def test_summarize_input_read():
    assert R._summarize_input("Read", {"file_path": "src/foo.py"}) == "Read src/foo.py"


def test_summarize_input_bash_truncates():
    long_cmd = "echo " + ("x" * 500)
    out = R._summarize_input("Bash", {"command": long_cmd})
    assert out.startswith("Bash: echo ")
    assert out.endswith("...")
    assert len(out) <= R._INPUT_SUMMARY_LIMIT


def test_summarize_input_grep_includes_path_when_present():
    out = R._summarize_input("Grep", {"pattern": "TODO", "path": "src/"})
    assert "TODO" in out and "src/" in out


def test_summarize_input_agent_includes_subagent_type():
    out = R._summarize_input("Agent", {
        "description": "do stuff", "subagent_type": "general-purpose",
    })
    assert "general-purpose" in out and "do stuff" in out


def test_summarize_input_fallback_json_for_unknown_tool():
    out = R._summarize_input("FancyMcpTool", {"a": 1, "b": 2})
    assert "FancyMcpTool" in out
    # Stable / sorted-keys JSON serialization for deterministic snapshots.
    assert '"a": 1' in out and '"b": 2' in out


def test_summarize_output_truncates_long_content():
    block = {"type": "tool_result", "content": "x" * 4096, "is_error": False}
    out = R._summarize_output(block)
    assert len(out) <= R._OUTPUT_SUMMARY_LIMIT
    assert out.endswith("...")


def test_summarize_output_handles_list_content_blocks():
    block = {
        "type": "tool_result",
        "content": [
            {"type": "text", "text": "line one"},
            {"type": "text", "text": "line two"},
        ],
    }
    assert R._summarize_output(block) == "line one\nline two"


def test_summarize_output_empty_when_no_result():
    assert R._summarize_output(None) == ""


# ---------------------------------------------------------------------------
# Output status detection
# ---------------------------------------------------------------------------


def test_detect_output_status_ok():
    block = {"content": "fine", "is_error": False}
    status, reason = R._detect_output_status(block, "fine")
    assert status == "ok"
    assert reason is None


def test_detect_output_status_no_result_is_error_with_reason():
    status, reason = R._detect_output_status(None, "")
    assert status == "error"
    assert "session may have ended" in (reason or "")


def test_detect_output_status_error_is_error():
    block = {"content": "boom", "is_error": True}
    status, reason = R._detect_output_status(block, "boom")
    assert status == "error"
    assert reason is None


def test_detect_output_status_denied_when_marker_present():
    rejection = (
        "The user doesn't want to proceed with this tool use. "
        "The tool use was rejected (eg. if it was a file edit, the new_string "
        "was not written to the file)."
    )
    block = {"content": rejection, "is_error": True}
    status, reason = R._detect_output_status(block, rejection)
    assert status == "denied"
    assert reason and "tool use was rejected" in reason.lower()


# ---------------------------------------------------------------------------
# Tool-call building + turn assembly
# ---------------------------------------------------------------------------


def _basic_read_turn():
    u = fx.user_typed_string("read foo")
    a = fx.assistant_tool_use("Read", tool_input={"file_path": "src/foo.py"},
                               tool_use_id="t1",
                               timestamp="2026-05-13T01:20:28.000Z")
    r = fx.user_tool_result("t1", content="contents", is_error=False,
                            timestamp="2026-05-13T01:20:28.250Z")
    return [u, a, r]


def test_build_locked_report_emits_required_top_level_fields():
    report = R.build_locked_report(_basic_read_turn())
    for k in ("report_version", "report_kind", "ide", "ide_version",
              "session_id", "session_id_normalized",
              "session_start_iso", "session_end_iso", "session_duration_s",
              "turns", "aggregates", "warnings"):
        assert k in report, f"missing {k}"
    assert report["report_version"] == "1.0"
    assert report["report_kind"] == "single-chat"
    assert report["ide"] == "claude-code"


def test_build_locked_report_normalizes_session_id():
    report = R.build_locked_report(_basic_read_turn())
    assert report["session_id_normalized"] == \
        report["session_id"].lower().replace("-", "")


def test_build_locked_report_emits_two_turns_per_user_prompt():
    """One user-typed prompt -> 1 user turn + 1 assistant turn = 2 turns."""
    report = R.build_locked_report(_basic_read_turn())
    assert len(report["turns"]) == 2
    assert report["turns"][0]["role"] == "user"
    assert report["turns"][1]["role"] == "assistant"
    assert report["turns"][1]["turn_index"] == 1
    assert report["turns"][0]["tool_calls"] == []


def test_build_locked_report_tool_call_carries_elapsed_ms():
    report = R.build_locked_report(_basic_read_turn())
    tc = report["turns"][1]["tool_calls"][0]
    assert tc["elapsed_ms"] == 250
    # And no "synthesized timestamps" warning -- claudecode has real ones.
    assert all(w["code"] != "cursor-per-turn-timestamps-synthesized"
               for w in report["warnings"])


def test_build_locked_report_aggregates_count_tool_calls():
    report = R.build_locked_report(_basic_read_turn())
    aggs = report["aggregates"]
    assert aggs["total_tool_calls"] == 1
    assert aggs["by_class"]["broad-sweep-read"] == 1


def test_build_locked_report_zero_calls_when_no_assistant():
    """User prompt with no assistant response yields aggregates total=0."""
    u = fx.user_typed_string("hi")
    report = R.build_locked_report([u])
    assert report["aggregates"]["total_tool_calls"] == 0
    # Two turns still emitted: user + empty assistant placeholder.
    assert {t["role"] for t in report["turns"]} == {"user", "assistant"}


def test_build_locked_report_ide_version_single_value():
    records = _basic_read_turn()
    for r in records:
        r["version"] = "2.1.140"
    report = R.build_locked_report(records)
    assert report["ide_version"] == "2.1.140"


def test_build_locked_report_ide_version_mixed_emits_warning():
    records = _basic_read_turn()
    records[0]["version"] = "2.1.128"
    records[1]["version"] = "2.1.140"
    records[2]["version"] = "2.1.140"
    report = R.build_locked_report(records)
    assert report["ide_version"] == "mixed"
    codes = [w["code"] for w in report["warnings"]]
    assert "claude-code-version-spans-upgrade" in codes


def test_build_locked_report_unknown_record_types_surface_as_warning():
    records = _basic_read_turn()
    records.append({"type": "future-record-type-from-cc-3", "uuid": "x"})
    report = R.build_locked_report(records)
    codes = [w["code"] for w in report["warnings"]]
    assert "unknown-record-types-encountered" in codes


def test_build_locked_report_session_id_override_wins():
    records = _basic_read_turn()
    report = R.build_locked_report(records, session_id_override="custom-sid")
    assert report["session_id"] == "custom-sid"


def test_build_locked_report_informed_precision_classification():
    u = fx.user_typed_string("read after dir-index")
    a1 = fx.assistant_tool_use("Read",
                                tool_input={"file_path": "DIRECTORY_INDEX.md"},
                                tool_use_id="t1")
    r1 = fx.user_tool_result("t1", content="index", is_error=False)
    a2 = fx.assistant_tool_use("Read",
                                tool_input={"file_path": "src/foo.py"},
                                tool_use_id="t2")
    r2 = fx.user_tool_result("t2", content="data", is_error=False)
    report = R.build_locked_report([u, a1, r1, a2, r2])
    assistant_turn = next(t for t in report["turns"] if t["role"] == "assistant")
    # First call is the dir-index read; not applicable.
    tc1, tc2 = assistant_turn["tool_calls"]
    assert tc1["tool_class"] == "directory-index-read"
    assert tc1["informed_precision_read"]["applicable"] is False
    # Second call is a regular Read preceded by dir-index in same turn.
    assert tc2["tool_class"] == "broad-sweep-read"
    assert tc2["informed_precision_read"]["classification"] == "informed"


def test_build_locked_report_denial_lands_in_aggregates_denials():
    u = fx.user_typed_string("write something")
    a = fx.assistant_tool_use("Write",
                               tool_input={"file_path": "foo", "content": "x"},
                               tool_use_id="t1")
    rejection = ("The user doesn't want to proceed with this tool use. "
                 "The tool use was rejected.")
    r = fx.user_tool_result("t1", content=rejection, is_error=True)
    report = R.build_locked_report([u, a, r])
    assert len(report["aggregates"]["denials"]) == 1
    assert report["aggregates"]["denials"][0]["tool_name"] == "Write"
    assert len(report["aggregates"]["errors"]) == 0


def test_build_locked_report_generic_error_lands_in_errors():
    u = fx.user_typed_string("read missing")
    a = fx.assistant_tool_use("Read", tool_input={"file_path": "nope"},
                               tool_use_id="t1")
    r = fx.user_tool_result("t1", content="File not found: nope",
                            is_error=True)
    report = R.build_locked_report([u, a, r])
    assert len(report["aggregates"]["errors"]) == 1
    assert report["aggregates"]["errors"][0]["tool_name"] == "Read"


def test_build_locked_report_session_time_span_uses_min_max_timestamps():
    records = _basic_read_turn()
    records[0]["timestamp"] = "2026-05-13T01:00:00.000Z"
    records[1]["timestamp"] = "2026-05-13T01:02:00.000Z"
    records[2]["timestamp"] = "2026-05-13T01:05:30.000Z"
    report = R.build_locked_report(records)
    assert report["session_start_iso"] == "2026-05-13T01:00:00.000Z"
    assert report["session_end_iso"] == "2026-05-13T01:05:30.000Z"
    assert report["session_duration_s"] == 5 * 60 + 30
