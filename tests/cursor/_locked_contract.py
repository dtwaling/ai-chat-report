"""Programmatic validator for the locked structured-report shape.

Implements the section-4.1 contract from MISSION-BRIEF.md. The contract is
required-fields-with-types -- extra keys are tolerated per section 4.5
("additive non-breaking change... does NOT bump version").

Use ``assert_locked_single_chat_conforms(report)`` for single-chat reports,
``assert_locked_diff_conforms(diff)`` for diffs, and
``assert_locked_aggregate_conforms(agg)`` for aggregates.
"""

from __future__ import annotations

from typing import Any


_LOCKED_TOOL_CLASSES = frozenset((
    "broad-sweep-read", "broad-sweep-grep", "broad-sweep-glob",
    "optimus-mcp", "directory-index-read",
    "edit", "write", "bash", "other",
))

_OUTPUT_STATUSES = frozenset(("ok", "error", "denied", "blocked"))
_IPR_CLASSIFICATIONS = frozenset(("informed", "uninformed", "n/a"))
_IDES = frozenset(("cursor", "claude-code"))


def _require_type(value: Any, types: tuple[type, ...], path: str) -> None:
    if not isinstance(value, types):
        raise AssertionError(
            f"{path}: expected one of {[t.__name__ for t in types]}, got {type(value).__name__} ({value!r})"
        )


def _require_keys(d: dict[str, Any], keys: list[str], path: str) -> None:
    missing = [k for k in keys if k not in d]
    if missing:
        raise AssertionError(f"{path}: missing required keys: {missing}")


def _assert_tool_call(tc: dict[str, Any], path: str) -> None:
    _require_type(tc, (dict,), path)
    _require_keys(tc, [
        "call_index", "tool_name", "tool_class",
        "input_summary", "input_payload",
        "output_summary", "output_status",
        "denial_reason", "elapsed_ms",
        "informed_precision_read",
    ], path)
    _require_type(tc["call_index"], (int,), f"{path}.call_index")
    _require_type(tc["tool_name"], (str,), f"{path}.tool_name")
    if tc["tool_class"] not in _LOCKED_TOOL_CLASSES:
        raise AssertionError(
            f"{path}.tool_class: expected one of {sorted(_LOCKED_TOOL_CLASSES)}, "
            f"got {tc['tool_class']!r}"
        )
    _require_type(tc["input_summary"], (str,), f"{path}.input_summary")
    # input_payload: object or string per section 4.1.
    _require_type(tc["input_payload"], (dict, str, list), f"{path}.input_payload")
    _require_type(tc["output_summary"], (str,), f"{path}.output_summary")
    if tc["output_status"] not in _OUTPUT_STATUSES:
        raise AssertionError(
            f"{path}.output_status: expected one of {sorted(_OUTPUT_STATUSES)}, "
            f"got {tc['output_status']!r}"
        )
    if tc["denial_reason"] is not None:
        _require_type(tc["denial_reason"], (str,), f"{path}.denial_reason")
    if tc["elapsed_ms"] is not None:
        _require_type(tc["elapsed_ms"], (int,), f"{path}.elapsed_ms")

    ipr = tc["informed_precision_read"]
    _require_type(ipr, (dict,), f"{path}.informed_precision_read")
    _require_keys(ipr, ["applicable", "classification", "reason"],
                  f"{path}.informed_precision_read")
    _require_type(ipr["applicable"], (bool,), f"{path}.informed_precision_read.applicable")
    if ipr["classification"] not in _IPR_CLASSIFICATIONS:
        raise AssertionError(
            f"{path}.informed_precision_read.classification: "
            f"expected one of {sorted(_IPR_CLASSIFICATIONS)}, got {ipr['classification']!r}"
        )
    _require_type(ipr["reason"], (str,), f"{path}.informed_precision_read.reason")


def _assert_turn(turn: dict[str, Any], path: str) -> None:
    _require_type(turn, (dict,), path)
    _require_keys(turn, ["turn_index", "role", "started_iso", "ended_iso", "tool_calls"], path)
    _require_type(turn["turn_index"], (int,), f"{path}.turn_index")
    if turn["role"] not in ("user", "assistant"):
        raise AssertionError(f"{path}.role: expected 'user' or 'assistant', got {turn['role']!r}")
    _require_type(turn["started_iso"], (str,), f"{path}.started_iso")
    _require_type(turn["ended_iso"], (str,), f"{path}.ended_iso")
    _require_type(turn["tool_calls"], (list,), f"{path}.tool_calls")
    for i, tc in enumerate(turn["tool_calls"]):
        _assert_tool_call(tc, f"{path}.tool_calls[{i}]")


def _assert_aggregates_block(aggs: dict[str, Any], path: str) -> None:
    _require_type(aggs, (dict,), path)
    _require_keys(aggs, [
        "total_tool_calls", "by_class",
        "success_metric_components", "denials", "errors",
    ], path)
    _require_type(aggs["total_tool_calls"], (int,), f"{path}.total_tool_calls")
    _require_type(aggs["by_class"], (dict,), f"{path}.by_class")
    # Every class enum must be present (zero-fill required).
    missing_classes = _LOCKED_TOOL_CLASSES - set(aggs["by_class"].keys())
    if missing_classes:
        raise AssertionError(f"{path}.by_class: missing tool_class entries: {sorted(missing_classes)}")
    for cls, count in aggs["by_class"].items():
        _require_type(count, (int,), f"{path}.by_class[{cls!r}]")

    smc = aggs["success_metric_components"]
    _require_type(smc, (dict,), f"{path}.success_metric_components")
    _require_keys(smc, ["component_a", "component_b", "overall"],
                  f"{path}.success_metric_components")
    a = smc["component_a"]
    _require_keys(a, ["definition", "optimus_count", "broad_sweep_count", "ratio", "pass"],
                  f"{path}.success_metric_components.component_a")
    _require_type(a["pass"], (bool,), f"{path}.success_metric_components.component_a.pass")
    b = smc["component_b"]
    _require_keys(b, ["definition", "informed_count", "uninformed_count", "ratio", "pass",
                      "heuristic_failure_modes_flagged"],
                  f"{path}.success_metric_components.component_b")
    _require_type(b["heuristic_failure_modes_flagged"], (list,),
                  f"{path}.success_metric_components.component_b.heuristic_failure_modes_flagged")
    o = smc["overall"]
    _require_keys(o, ["pass", "partial_pass"], f"{path}.success_metric_components.overall")
    _require_type(o["pass"], (bool,), f"{path}.success_metric_components.overall.pass")
    _require_type(o["partial_pass"], (bool,), f"{path}.success_metric_components.overall.partial_pass")

    _require_type(aggs["denials"], (list,), f"{path}.denials")
    _require_type(aggs["errors"], (list,), f"{path}.errors")


def _assert_top_meta(report: dict[str, Any], path: str = "report") -> None:
    """Top-level fields shared across all report_kinds: version, ide, etc."""
    _require_type(report, (dict,), path)
    _require_keys(report, ["report_version", "report_kind", "ide", "ide_version"], path)
    _require_type(report["report_version"], (str,), f"{path}.report_version")
    if report["report_version"] != "1.0":
        raise AssertionError(f"{path}.report_version: expected '1.0', got {report['report_version']!r}")
    if report["ide"] not in _IDES:
        raise AssertionError(f"{path}.ide: expected one of {sorted(_IDES)}, got {report['ide']!r}")
    _require_type(report["ide_version"], (str,), f"{path}.ide_version")


def assert_locked_single_chat_conforms(report: dict[str, Any]) -> None:
    """Validate a locked single-chat report against MISSION-BRIEF section 4.1."""
    _assert_top_meta(report)
    if report.get("report_kind") != "single-chat":
        raise AssertionError(f"report.report_kind: expected 'single-chat', got {report.get('report_kind')!r}")
    _require_keys(report, [
        "session_id", "session_id_normalized",
        "session_start_iso", "session_end_iso", "session_duration_s",
        "turns", "aggregates", "warnings",
    ], "report")
    _require_type(report["session_id"], (str,), "report.session_id")
    _require_type(report["session_id_normalized"], (str,), "report.session_id_normalized")
    _require_type(report["session_start_iso"], (str,), "report.session_start_iso")
    _require_type(report["session_end_iso"], (str,), "report.session_end_iso")
    _require_type(report["session_duration_s"], (int,), "report.session_duration_s")
    _require_type(report["turns"], (list,), "report.turns")
    for i, turn in enumerate(report["turns"]):
        _assert_turn(turn, f"report.turns[{i}]")
    _assert_aggregates_block(report["aggregates"], "report.aggregates")
    _require_type(report["warnings"], (list,), "report.warnings")
    for i, w in enumerate(report["warnings"]):
        _require_type(w, (dict,), f"report.warnings[{i}]")
        _require_keys(w, ["code", "message"], f"report.warnings[{i}]")


def assert_locked_diff_conforms(diff: dict[str, Any]) -> None:
    """Validate a locked diff report against MISSION-BRIEF section 4.4."""
    _assert_top_meta(diff, "diff")
    if diff.get("report_kind") != "diff":
        raise AssertionError(f"diff.report_kind: expected 'diff', got {diff.get('report_kind')!r}")
    _require_keys(diff, ["before", "after", "delta", "generated_at_iso"], "diff")
    assert_locked_single_chat_conforms(diff["before"])
    assert_locked_single_chat_conforms(diff["after"])
    d = diff["delta"]
    _require_type(d, (dict,), "diff.delta")
    _require_keys(d, ["total_tool_calls", "by_class", "component_a", "component_b"], "diff.delta")
    _require_type(d["total_tool_calls"], (int,), "diff.delta.total_tool_calls")
    _require_type(d["by_class"], (dict,), "diff.delta.by_class")
    missing = _LOCKED_TOOL_CLASSES - set(d["by_class"].keys())
    if missing:
        raise AssertionError(f"diff.delta.by_class missing classes: {sorted(missing)}")


def assert_locked_aggregate_conforms(agg: dict[str, Any]) -> None:
    """Validate a locked aggregate report against MISSION-BRIEF section 4.3."""
    _assert_top_meta(agg, "agg")
    if agg.get("report_kind") != "aggregate":
        raise AssertionError(f"agg.report_kind: expected 'aggregate', got {agg.get('report_kind')!r}")
    _require_keys(agg, ["session_count", "sessions", "aggregates", "generated_at_iso"], "agg")
    _require_type(agg["session_count"], (int,), "agg.session_count")
    _require_type(agg["sessions"], (list,), "agg.sessions")
    if len(agg["sessions"]) != agg["session_count"]:
        raise AssertionError(
            f"agg.session_count ({agg['session_count']}) does not match "
            f"len(agg.sessions) ({len(agg['sessions'])})"
        )
    for i, s in enumerate(agg["sessions"]):
        _require_type(s, (dict,), f"agg.sessions[{i}]")
        _require_keys(s, ["session_id", "session_id_normalized", "aggregates"],
                      f"agg.sessions[{i}]")
        _assert_aggregates_block(s["aggregates"], f"agg.sessions[{i}].aggregates")
    _assert_aggregates_block(agg["aggregates"], "agg.aggregates")
