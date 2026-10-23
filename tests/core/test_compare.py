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
