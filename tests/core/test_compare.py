from __future__ import annotations

from chat_report.core.compare import build_aggregate_report, build_diff_report, build_session_aggregates, build_single_session_report
from chat_report.core.ir import SessionIR, TokenBreakdownIR, ToolCallIR, TurnIR


def _session(session_id: str, *, status: str = "completed") -> SessionIR:
    call = ToolCallIR(1, 1, "b", None, None, "Read", "file_read", "native-read", None, None, "a.py", {}, "", "", 0, status, status == "error", False, None, None)
    turn = TurnIR(1, "u", None, "", None, None, None, None, False, None, None, tool_calls=[call])
    return SessionIR("2.0", "cursor", "x", session_id, session_id, None, False, None, None, None, None, None, None, 10, None, None, None, 1, 0, [], [], 0, 0, TokenBreakdownIR(10, 100, 10), turns=[turn])


def test_session_aggregate_and_diff_arithmetic() -> None:
    before, after = _session("before"), _session("after", status="error")
    aggregates = build_session_aggregates(before)
    assert aggregates["tool_calls_total"] == 1
    assert aggregates["status_counts"]["completed"] == 1
    diff = build_diff_report(before, after)
    assert diff["delta"]["error_count_delta"] == 1
    assert diff["delta"]["turn_count_delta"] == 0
    assert diff["before"]["report_kind"] == "single-session"
    assert "turns" in diff["before"] and "warnings" not in diff["before"]["session"]
    assert build_single_session_report(before)["schema_version"] == "2.0"


def test_aggregate_rollup_sums_session_counts() -> None:
    report = build_aggregate_report([_session("one"), _session("two", status="error")])
    assert report["session_count"] == 2
    assert report["rollup"]["total_tool_calls"] == 2
    assert report["rollup"]["status_counts"] == {"completed": 1, "error": 1, "denied": 0, "cancelled": 0, "loading": 0}
    assert report["rollup"]["diagnostics_rollup"]["by_severity"] == {"high": 0, "medium": 0, "low": 0, "info": 0}


def test_reports_preserve_deduplicated_session_warnings() -> None:
    before, after = _session("before"), _session("after")
    warning = {"code": "test-warning", "message": "kept"}
    before.warnings = [warning]
    after.warnings = [warning, {"code": "second", "message": "also kept"}]
    assert build_single_session_report(before)["warnings"] == [warning]
    assert build_diff_report(before, after)["warnings"] == [warning, {"code": "second", "message": "also kept"}]
    assert build_aggregate_report([before, after])["warnings"] == [warning, {"code": "second", "message": "also kept"}]


def test_captured_at_ms_syncs_single_and_embedded_diff_generated_at_iso() -> None:
    before, after = _session("before"), _session("after")
    before.captured_at_ms = 1767225600000
    after.captured_at_ms = 1767229200000
    after.in_flight = True
    after.status = None
    after.duration_ms = None
    after.ended_at_ms = None
    single = build_single_session_report(before)
    assert single["generated_at_iso"] == "2026-01-01T00:00:00+00:00"
    diff = build_diff_report(before, after)
    assert diff["before"]["generated_at_iso"] == "2026-01-01T00:00:00+00:00"
    assert diff["after"]["generated_at_iso"] == "2026-01-01T01:00:00+00:00"
    assert diff["generated_at_iso"] != diff["before"]["generated_at_iso"]
    agg = build_aggregate_report([before, after])
    assert agg["sessions"][0]["captured_at_ms"] == 1767225600000
    assert agg["sessions"][0]["in_flight"] is False
    assert agg["sessions"][1]["captured_at_ms"] == 1767229200000
    assert agg["sessions"][1]["in_flight"] is True
