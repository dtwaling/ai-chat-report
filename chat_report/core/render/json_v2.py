"""JSON v2 report validation helpers."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from chat_report.core.ir import (
    CANONICAL_TOOL_KINDS,
    HEURISTIC_IDS,
    RESOLVED_SOURCES,
    SEMANTIC_TOOL_CLASSES,
    SEVERITY_LEVELS,
    TOOL_CALL_STATUSES,
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    _assert(set(value) == expected, f"{label} keys differ: {set(value) ^ expected}")


def _int(value: Any, label: str, *, nullable: bool = False) -> None:
    _assert((nullable and value is None) or (isinstance(value, int) and not isinstance(value, bool)), f"{label} must be an int")


def _str(value: Any, label: str, *, nullable: bool = False) -> None:
    _assert((nullable and value is None) or isinstance(value, str), f"{label} must be a string")


def _count_map(value: Any, label: str, keys: set[str] | None = None) -> None:
    _assert(isinstance(value, dict), f"{label} must be an object")
    if keys is not None:
        _keys(value, keys, label)
    for name, count in value.items():
        _str(name, f"{label} key")
        _int(count, f"{label}.{name}")


def _assert_envelope(payload: dict[str, Any], expected_kind: str) -> None:
    for key in ("schema_version", "report_kind", "generated_at_iso", "harness", "harness_version", "warnings"):
        _assert(key in payload, f"missing envelope key {key}")
    _assert(payload["schema_version"] == "2.0", "schema_version must be 2.0")
    _assert(payload["report_kind"] == expected_kind, f"report_kind must be {expected_kind}")
    _str(payload["generated_at_iso"], "generated_at_iso")
    _str(payload["harness"], "harness")
    _str(payload["harness_version"], "harness_version")
    _assert(isinstance(payload["warnings"], list), "warnings must be a list")
    for warning in payload["warnings"]:
        _assert(isinstance(warning, dict), "warning must be an object")
        _keys(warning, {"code", "message"}, "warning")
        _str(warning["code"], "warning.code")
        _str(warning["message"], "warning.message")


def _assert_session_block(session: Any) -> None:
    _assert(isinstance(session, dict), "session must be an object")
    expected = {
        "schema_version", "harness", "harness_version", "session_id", "session_id_normalized",
        "parent_session_id", "is_subagent", "subagent_type", "title", "mode", "status",
        "model_name", "max_mode", "reasoning_effort", "started_at_ms", "ended_at_ms",
        "duration_ms", "total_lines_added", "total_lines_removed", "files_added",
        "files_removed", "stop_hook_loop_count", "orphan_bubble_count", "token_breakdown",
        "customizations", "subagents", "attribution", "resolved_from",
    }
    _keys(session, expected, "session")
    for key in ("schema_version", "harness", "harness_version", "session_id", "session_id_normalized"):
        _str(session[key], f"session.{key}")
    _assert(session["schema_version"] == "2.0", "session.schema_version must be 2.0")
    _assert(session["session_id_normalized"] == session["session_id"].strip().lower(), "session normalized ID mismatch")
    for key in ("parent_session_id", "subagent_type", "title", "mode", "status", "model_name", "reasoning_effort"):
        _str(session[key], f"session.{key}", nullable=True)
    _assert(session["is_subagent"] in (True, False), "session.is_subagent must be bool")
    _assert(session["max_mode"] is None or isinstance(session["max_mode"], bool), "session.max_mode must be bool or null")
    for key in ("started_at_ms", "ended_at_ms", "duration_ms"):
        _int(session[key], f"session.{key}", nullable=True)
    for key in ("total_lines_added", "total_lines_removed", "stop_hook_loop_count", "orphan_bubble_count"):
        _int(session[key], f"session.{key}")
    _assert(isinstance(session["files_added"], list) and all(isinstance(x, str) for x in session["files_added"]), "session.files_added invalid")
    _assert(isinstance(session["files_removed"], list) and all(isinstance(x, str) for x in session["files_removed"]), "session.files_removed invalid")
    token = session["token_breakdown"]
    _keys(token, {"total_context_tokens_used", "context_token_limit", "context_usage_percent", "by_category", "top_context_nodes"}, "token_breakdown")
    for key in ("total_context_tokens_used", "context_token_limit"):
        _int(token[key], f"token_breakdown.{key}", nullable=True)
    _assert(token["context_usage_percent"] is None or isinstance(token["context_usage_percent"], (int, float)), "context_usage_percent invalid")
    _assert(isinstance(token["by_category"], dict), "by_category invalid")
    for category, values in token["by_category"].items():
        _str(category, "category")
        _keys(values, {"tokens", "chars"}, f"by_category.{category}")
        _int(values["tokens"], f"by_category.{category}.tokens")
        _int(values["chars"], f"by_category.{category}.chars")
    _assert(isinstance(token["top_context_nodes"], list), "top_context_nodes invalid")
    _assert(isinstance(session["customizations"], list), "customizations invalid")
    for ref in session["customizations"]:
        _keys(ref, {"kind", "path", "source", "turn_index"}, "customization")
        _assert(ref["kind"] in {"skill", "rule", "agent_doc", "hook"}, "customization kind invalid")
        _assert(ref["source"] in {"context_attached", "explicit_read", "explicit_edit", "workspace_active"}, "customization source invalid")
        _str(ref["path"], "customization.path")
        _int(ref["turn_index"], "customization.turn_index", nullable=True)
    _assert(isinstance(session["subagents"], list), "subagents invalid")
    for subagent in session["subagents"]:
        _keys(subagent, {"session_id", "parent_turn_index", "subagent_type", "name", "model_name", "status", "duration_ms", "turn_count", "tool_call_count", "error_count", "denial_count", "skills_read", "rules_read"}, "subagent")
        _str(subagent["session_id"], "subagent.session_id")
        for key in ("parent_turn_index", "duration_ms"):
            _int(subagent[key], f"subagent.{key}", nullable=True)
        for key in ("turn_count", "tool_call_count", "error_count", "denial_count"):
            _int(subagent[key], f"subagent.{key}")
    attribution = session["attribution"]
    if attribution is not None:
        _keys(attribution, {"composer_code_hashes_count", "human_code_hashes_count", "distinct_files_touched", "scored_commits"}, "attribution")
        _int(attribution["composer_code_hashes_count"], "attribution.composer_code_hashes_count")
        _int(attribution["human_code_hashes_count"], "attribution.human_code_hashes_count")
        _assert(isinstance(attribution["distinct_files_touched"], list), "attribution distinct files invalid")
        _assert(isinstance(attribution["scored_commits"], list), "attribution commits invalid")
    resolved = session["resolved_from"]
    if resolved is not None:
        _keys(resolved, {"session_id", "input_id", "source"}, "resolved_from")
        _assert(resolved["source"] in RESOLVED_SOURCES, "resolved_from source invalid")
        _assert(resolved["session_id"] == session["session_id"], "resolved_from session ID mismatch")


def _assert_aggregates(aggregates: Any, turns: list[dict[str, Any]] | None) -> None:
    _assert(isinstance(aggregates, dict), "aggregates must be an object")
    expected = {"turn_count", "thinking_block_count", "total_thinking_duration_ms", "tool_calls_total", "tool_calls_by_semantic_class", "tool_calls_by_canonical_kind", "mcp_calls_by_namespace", "status_counts", "timing", "customization_summary", "subagent_rollup"}
    _keys(aggregates, expected, "aggregates")
    for key in ("turn_count", "thinking_block_count", "total_thinking_duration_ms", "tool_calls_total"):
        _int(aggregates[key], f"aggregates.{key}")
    if turns is not None:
        _assert(aggregates["turn_count"] == len(turns), "aggregates.turn_count mismatch")
    _count_map(aggregates["tool_calls_by_semantic_class"], "semantic counts")
    _count_map(aggregates["tool_calls_by_canonical_kind"], "kind counts")
    _count_map(aggregates["mcp_calls_by_namespace"], "namespace counts")
    _count_map(aggregates["status_counts"], "status counts", set(TOOL_CALL_STATUSES))
    _assert(sum(aggregates["status_counts"].values()) == aggregates["tool_calls_total"], "status total mismatch")
    _assert(sum(aggregates["tool_calls_by_semantic_class"].values()) == aggregates["tool_calls_total"], "semantic count total mismatch")
    _assert(sum(aggregates["tool_calls_by_canonical_kind"].values()) == aggregates["tool_calls_total"], "canonical kind count total mismatch")
    if turns is not None:
        _assert(aggregates["thinking_block_count"] == sum(len(turn["thinking_blocks"]) for turn in turns), "thinking block count mismatch")
        _assert(aggregates["tool_calls_total"] == sum(len(turn["tool_calls"]) for turn in turns), "tool call total mismatch")
    timing = aggregates["timing"]
    _keys(timing, {"total_session_ms", "total_tool_elapsed_ms", "slowest_tool_calls"}, "timing")
    _int(timing["total_session_ms"], "timing.total_session_ms", nullable=True)
    _int(timing["total_tool_elapsed_ms"], "timing.total_tool_elapsed_ms")
    _assert(isinstance(timing["slowest_tool_calls"], list) and len(timing["slowest_tool_calls"]) <= 5, "slowest tool calls invalid")
    previous: tuple[int, int] | None = None
    for call in timing["slowest_tool_calls"]:
        _keys(call, {"call_index", "turn_index", "raw_tool_name", "semantic_class", "elapsed_ms", "status"}, "slow tool call")
        _int(call["call_index"], "slow tool call.call_index")
        _int(call["turn_index"], "slow tool call.turn_index")
        _str(call["raw_tool_name"], "slow tool call.raw_tool_name")
        _str(call["semantic_class"], "slow tool call.semantic_class")
        _int(call["elapsed_ms"], "slow tool call.elapsed_ms")
        _str(call["status"], "slow tool call.status")
        current = (-call["elapsed_ms"], call["call_index"])
        _assert(previous is None or previous <= current, "slow tool calls not sorted")
        previous = current
    customization = aggregates["customization_summary"]
    _keys(customization, {"skills_read", "rules_read", "attached_rules", "subagents_spawned"}, "customization summary")
    for key in ("skills_read", "rules_read", "attached_rules"):
        _assert(isinstance(customization[key], list) and customization[key] == sorted(set(customization[key])), f"{key} must be sorted distinct paths")
    _int(customization["subagents_spawned"], "subagents_spawned")
    _keys(aggregates["subagent_rollup"], {"count", "by_type", "by_status", "total_tool_calls", "total_errors"}, "subagent rollup")


def _assert_diagnostics(diagnostics: Any) -> None:
    _assert(isinstance(diagnostics, dict), "diagnostics must be an object")
    _keys(diagnostics, {"total_findings", "by_severity", "findings"}, "diagnostics")
    _int(diagnostics["total_findings"], "diagnostics.total_findings")
    _count_map(diagnostics["by_severity"], "severity counts", set(SEVERITY_LEVELS))
    _assert(diagnostics["total_findings"] == len(diagnostics["findings"]) == sum(diagnostics["by_severity"].values()), "diagnostic total mismatch")
    keys: set[str] = set()
    for finding in diagnostics["findings"]:
        required = {"id", "finding_key", "name", "severity", "turn_index", "bubble_id", "summary", "error_message", "thinking_snippet", "evidence", "remediation_hint"}
        _keys(finding, required, "finding")
        _assert(finding["id"] in HEURISTIC_IDS, "unknown finding ID")
        _assert(finding["severity"] in SEVERITY_LEVELS, "unknown finding severity")
        _assert(finding["finding_key"].startswith(f"{finding['id']}:"), "finding key mismatch")
        _assert(finding["finding_key"] not in keys, "diagnostics.findings finding_key must be unique")
        keys.add(finding["finding_key"])
        for key in ("name", "summary", "remediation_hint"):
            _assert(isinstance(finding[key], str) and bool(finding[key]), f"finding.{key} must be a non-empty string")
        for key in ("error_message", "thinking_snippet"):
            _assert(finding[key] is None or isinstance(finding[key], str), f"finding.{key} must be a string or null")
            _assert(finding[key] is None or len(finding[key]) <= 160, f"finding.{key} exceeds 160 characters")
        _assert(isinstance(finding["evidence"], dict), "finding.evidence must be an object")


def _nullable_str(value: Any, label: str) -> None:
    _str(value, label, nullable=True)


def _assert_turn(turn: Any, expected_index: int) -> None:
    _assert(isinstance(turn, dict), f"turn[{expected_index}] must be an object")
    expected = {
        "turn_index", "user_bubble_id", "request_id", "user_text", "model_name",
        "started_at_ms", "completed_at_ms", "duration_ms", "timestamps_synthesized",
        "input_tokens", "output_tokens", "attached_rules", "attached_files",
        "thinking_blocks", "tool_calls", "assistant_texts",
    }
    _keys(turn, expected, f"turn[{expected_index}]")
    _assert(turn["turn_index"] == expected_index, f"turn[{expected_index}].turn_index mismatch")
    _str(turn["user_bubble_id"], f"turn[{expected_index}].user_bubble_id")
    for key in ("request_id", "model_name"):
        _nullable_str(turn[key], f"turn[{expected_index}].{key}")
    _str(turn["user_text"], f"turn[{expected_index}].user_text")
    for key in ("started_at_ms", "completed_at_ms", "duration_ms", "input_tokens", "output_tokens"):
        _int(turn[key], f"turn[{expected_index}].{key}", nullable=True)
    _assert(isinstance(turn["timestamps_synthesized"], bool), f"turn[{expected_index}].timestamps_synthesized must be bool")
    for key in ("attached_rules", "attached_files", "assistant_texts"):
        _assert(isinstance(turn[key], list) and all(isinstance(item, str) for item in turn[key]), f"turn[{expected_index}].{key} must be a string list")
    _assert(isinstance(turn["thinking_blocks"], list), f"turn[{expected_index}].thinking_blocks must be a list")
    for block in turn["thinking_blocks"]:
        _keys(block, {"bubble_id", "text", "duration_ms", "started_at_ms", "completed_at_ms"}, "thinking block")
        _str(block["bubble_id"], "thinking.bubble_id")
        _str(block["text"], "thinking.text")
        for key in ("duration_ms", "started_at_ms", "completed_at_ms"):
            _int(block[key], f"thinking.{key}", nullable=True)
    _assert(isinstance(turn["tool_calls"], list), f"turn[{expected_index}].tool_calls must be a list")
    for call in turn["tool_calls"]:
        _assert_tool_call(call, expected_index)


def _assert_tool_call(call: Any, turn_index: int) -> None:
    _assert(isinstance(call, dict), "tool call must be an object")
    expected = {
        "call_index", "turn_index", "bubble_id", "tool_call_id", "model_call_id",
        "raw_tool_name", "canonical_kind", "semantic_class", "mcp_namespace",
        "mcp_tool_name", "target_path", "input_params", "input_summary",
        "output_summary", "output_char_len", "status", "is_error", "is_denied",
        "denial_reason", "error_message", "started_at_ms", "completed_at_ms",
        "elapsed_ms", "subagent_session_id", "offloaded_before_id",
        "offloaded_after_id",
    }
    _keys(call, expected, "tool call")
    _int(call["call_index"], "tool call.call_index")
    _assert(call["turn_index"] == turn_index, "tool call.turn_index mismatch")
    _str(call["bubble_id"], "tool call.bubble_id")
    for key in ("tool_call_id", "model_call_id", "mcp_namespace", "mcp_tool_name", "target_path", "denial_reason", "error_message", "subagent_session_id", "offloaded_before_id", "offloaded_after_id"):
        _nullable_str(call[key], f"tool call.{key}")
    for key in ("raw_tool_name", "canonical_kind", "semantic_class", "input_summary", "output_summary", "status"):
        _str(call[key], f"tool call.{key}")
    _assert(call["canonical_kind"] in CANONICAL_TOOL_KINDS, "tool call canonical_kind is invalid")
    _assert(call["semantic_class"] in SEMANTIC_TOOL_CLASSES or call["semantic_class"].startswith("mcp-call:"), "tool call semantic_class is invalid")
    _assert(isinstance(call["input_params"], dict), "tool call.input_params must be an object")
    _int(call["output_char_len"], "tool call.output_char_len")
    _assert(call["status"] in TOOL_CALL_STATUSES, "tool call.status is invalid")
    for key in ("is_error", "is_denied"):
        _assert(isinstance(call[key], bool), f"tool call.{key} must be bool")
    for key in ("started_at_ms", "completed_at_ms", "elapsed_ms"):
        _int(call[key], f"tool call.{key}", nullable=True)


def _assert_single(payload: dict[str, Any]) -> None:
    _assert_envelope(payload, "single-session")
    _keys(payload, {"schema_version", "report_kind", "generated_at_iso", "harness", "harness_version", "warnings", "session", "aggregates", "diagnostics", "turns"}, "single report")
    _assert_session_block(payload["session"])
    _assert(isinstance(payload["turns"], list), "turns must be a list")
    for index, turn in enumerate(payload["turns"], start=1):
        _assert_turn(turn, index)
    call_indexes = [call["call_index"] for turn in payload["turns"] for call in turn["tool_calls"]]
    _assert(call_indexes == list(range(1, len(call_indexes) + 1)), "call indexes must be sequential")
    _assert_aggregates(payload["aggregates"], payload["turns"])
    _assert_diagnostics(payload["diagnostics"])


def _counter_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: after.get(key, 0) - before.get(key, 0) for key in sorted(set(before) | set(after))}


def _sum_count_maps(summaries: list[dict[str, Any]], aggregate_key: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for summary in summaries:
        counts.update(summary["aggregates"][aggregate_key])
    return dict(sorted(counts.items()))


def assert_v2_report_conforms(payload: dict[str, Any]) -> None:
    """Raise ValueError when a v2 single, diff, or aggregate report is invalid."""
    _assert(isinstance(payload, dict), "report must be an object")
    kind = payload.get("report_kind")
    if kind == "single-session":
        _assert_single(payload)
        return
    if kind == "diff":
        _assert_envelope(payload, "diff")
        _keys(payload, {"schema_version", "report_kind", "generated_at_iso", "harness", "harness_version", "warnings", "before", "after", "delta"}, "diff report")
        _assert_single(payload["before"])
        _assert_single(payload["after"])
        delta = payload["delta"]
        expected = {"turn_count_delta", "thinking_block_count_delta", "tool_calls_total_delta", "error_count_delta", "denial_count_delta", "duration_ms_delta", "context_tokens_used_delta", "by_semantic_class_delta", "by_canonical_kind_delta", "skills_read_added", "skills_read_removed", "rules_read_added", "rules_read_removed", "diagnostics_delta", "attribution_delta"}
        _keys(delta, expected, "delta")
        before, after = payload["before"], payload["after"]
        _assert(delta["turn_count_delta"] == after["aggregates"]["turn_count"] - before["aggregates"]["turn_count"], "turn delta mismatch")
        _assert(delta["thinking_block_count_delta"] == after["aggregates"]["thinking_block_count"] - before["aggregates"]["thinking_block_count"], "thinking block delta mismatch")
        _assert(delta["tool_calls_total_delta"] == after["aggregates"]["tool_calls_total"] - before["aggregates"]["tool_calls_total"], "tool delta mismatch")
        _assert(delta["error_count_delta"] == after["aggregates"]["status_counts"]["error"] - before["aggregates"]["status_counts"]["error"], "error delta mismatch")
        _assert(delta["denial_count_delta"] == after["aggregates"]["status_counts"]["denied"] - before["aggregates"]["status_counts"]["denied"], "denial delta mismatch")
        before_session, after_session = before["session"], after["session"]
        expected_duration = (
            after_session["duration_ms"] - before_session["duration_ms"]
            if after_session["duration_ms"] is not None and before_session["duration_ms"] is not None
            else None
        )
        _assert(delta["duration_ms_delta"] == expected_duration, "duration delta mismatch")
        before_context = before_session["token_breakdown"]["total_context_tokens_used"]
        after_context = after_session["token_breakdown"]["total_context_tokens_used"]
        expected_context = after_context - before_context if after_context is not None and before_context is not None else None
        _assert(delta["context_tokens_used_delta"] == expected_context, "context token delta mismatch")
        _assert(
            delta["by_semantic_class_delta"] == _counter_delta(
                before["aggregates"]["tool_calls_by_semantic_class"],
                after["aggregates"]["tool_calls_by_semantic_class"],
            ),
            "semantic class delta mismatch",
        )
        _assert(
            delta["by_canonical_kind_delta"] == _counter_delta(
                before["aggregates"]["tool_calls_by_canonical_kind"],
                after["aggregates"]["tool_calls_by_canonical_kind"],
            ),
            "canonical kind delta mismatch",
        )
        before_custom, after_custom = before["aggregates"]["customization_summary"], after["aggregates"]["customization_summary"]
        for key, expected_values in (
            ("skills_read_added", set(after_custom["skills_read"]) - set(before_custom["skills_read"])),
            ("skills_read_removed", set(before_custom["skills_read"]) - set(after_custom["skills_read"])),
            ("rules_read_added", set(after_custom["rules_read"]) - set(before_custom["rules_read"])),
            ("rules_read_removed", set(before_custom["rules_read"]) - set(after_custom["rules_read"])),
        ):
            _assert(delta[key] == sorted(expected_values), f"customization delta {key} mismatch")
        diagnostic_delta = delta["diagnostics_delta"]
        _keys(diagnostic_delta, {"total_findings_delta", "new_finding_ids", "resolved_finding_ids", "persisting_finding_ids", "new_finding_keys", "resolved_finding_keys", "persisting_finding_keys"}, "diagnostics delta")
        before_findings, after_findings = before["diagnostics"]["findings"], after["diagnostics"]["findings"]
        before_ids, after_ids = {item["id"] for item in before_findings}, {item["id"] for item in after_findings}
        before_keys, after_keys = {item["finding_key"] for item in before_findings}, {item["finding_key"] for item in after_findings}
        _assert(diagnostic_delta["total_findings_delta"] == len(after_findings) - len(before_findings), "diagnostic total delta mismatch")
        for key, expected_values in (
            ("new_finding_ids", after_ids - before_ids), ("resolved_finding_ids", before_ids - after_ids), ("persisting_finding_ids", before_ids & after_ids),
            ("new_finding_keys", after_keys - before_keys), ("resolved_finding_keys", before_keys - after_keys), ("persisting_finding_keys", before_keys & after_keys),
        ):
            _assert(diagnostic_delta[key] == sorted(expected_values), f"diagnostics delta {key} mismatch")
        attribution_delta = delta["attribution_delta"]
        _keys(attribution_delta, {"composer_code_hashes_delta", "human_code_hashes_delta", "lines_added_delta", "lines_removed_delta"}, "attribution delta")
        before_attribution, after_attribution = before_session["attribution"], after_session["attribution"]
        _assert(
            attribution_delta["composer_code_hashes_delta"] == (after_attribution or {}).get("composer_code_hashes_count", 0) - (before_attribution or {}).get("composer_code_hashes_count", 0),
            "composer attribution delta mismatch",
        )
        _assert(
            attribution_delta["human_code_hashes_delta"] == (after_attribution or {}).get("human_code_hashes_count", 0) - (before_attribution or {}).get("human_code_hashes_count", 0),
            "human attribution delta mismatch",
        )
        _assert(attribution_delta["lines_added_delta"] == after_session["total_lines_added"] - before_session["total_lines_added"], "lines added delta mismatch")
        _assert(attribution_delta["lines_removed_delta"] == after_session["total_lines_removed"] - before_session["total_lines_removed"], "lines removed delta mismatch")
        return
    if kind == "aggregate":
        _assert_envelope(payload, "aggregate")
        _keys(payload, {"schema_version", "report_kind", "generated_at_iso", "harness", "harness_version", "warnings", "session_count", "sessions", "rollup"}, "aggregate report")
        _int(payload["session_count"], "session_count")
        _assert(payload["session_count"] == len(payload["sessions"]) >= 2, "aggregate session count must be at least two")
        summary_keys = {"session_id", "session_id_normalized", "title", "model_name", "status", "duration_ms", "total_lines_added", "total_lines_removed", "attribution", "aggregates", "diagnostics"}
        for summary in payload["sessions"]:
            _keys(summary, summary_keys, "aggregate session summary")
            _str(summary["session_id"], "aggregate session session_id")
            _str(summary["session_id_normalized"], "aggregate session session_id_normalized")
            for key in ("title", "model_name", "status"):
                _nullable_str(summary[key], f"aggregate session {key}")
            _int(summary["duration_ms"], "aggregate session duration_ms", nullable=True)
            _int(summary["total_lines_added"], "aggregate session total_lines_added")
            _int(summary["total_lines_removed"], "aggregate session total_lines_removed")
            _assert_aggregates(summary["aggregates"], None)
            _assert_diagnostics(summary["diagnostics"])
        rollup = payload["rollup"]
        required = {"total_turns", "total_thinking_blocks", "total_tool_calls", "tool_calls_by_semantic_class", "tool_calls_by_canonical_kind", "mcp_calls_by_namespace", "status_counts", "diagnostics_rollup", "customization_rollup", "attribution_totals"}
        _keys(rollup, required, "aggregate rollup")
        _assert(rollup["total_turns"] == sum(item["aggregates"]["turn_count"] for item in payload["sessions"]), "aggregate turn total mismatch")
        _assert(rollup["total_thinking_blocks"] == sum(item["aggregates"]["thinking_block_count"] for item in payload["sessions"]), "aggregate thinking total mismatch")
        _assert(rollup["total_tool_calls"] == sum(item["aggregates"]["tool_calls_total"] for item in payload["sessions"]), "aggregate tool total mismatch")
        _assert(rollup["diagnostics_rollup"]["total_findings"] == sum(item["diagnostics"]["total_findings"] for item in payload["sessions"]), "aggregate finding total mismatch")
        for key in ("tool_calls_by_semantic_class", "tool_calls_by_canonical_kind", "mcp_calls_by_namespace"):
            _count_map(rollup[key], f"aggregate {key}")
            _assert(rollup[key] == _sum_count_maps(payload["sessions"], key), f"aggregate {key} mismatch")
        _count_map(rollup["status_counts"], "aggregate status counts", set(TOOL_CALL_STATUSES))
        for status in TOOL_CALL_STATUSES:
            _assert(rollup["status_counts"][status] == sum(item["aggregates"]["status_counts"][status] for item in payload["sessions"]), f"aggregate status {status} mismatch")
        diagnostics_rollup = rollup["diagnostics_rollup"]
        _keys(diagnostics_rollup, {"total_findings", "by_severity", "by_heuristic_id"}, "diagnostics rollup")
        _count_map(diagnostics_rollup["by_severity"], "aggregate severity counts", set(SEVERITY_LEVELS))
        for severity in SEVERITY_LEVELS:
            _assert(diagnostics_rollup["by_severity"][severity] == sum(item["diagnostics"]["by_severity"][severity] for item in payload["sessions"]), f"aggregate severity {severity} mismatch")
        expected_heuristics: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "session_ids": set()})
        for summary in payload["sessions"]:
            for finding in summary["diagnostics"]["findings"]:
                expected_heuristics[finding["id"]]["count"] += 1
                expected_heuristics[finding["id"]]["session_ids"].add(summary["session_id"])
        expected_heuristic_values = {
            key: {"count": value["count"], "session_ids": sorted(value["session_ids"])}
            for key, value in sorted(expected_heuristics.items())
        }
        _assert(diagnostics_rollup["by_heuristic_id"] == expected_heuristic_values, "aggregate heuristic rollup mismatch")
        customization_rollup = rollup["customization_rollup"]
        _keys(customization_rollup, {"skills_read_frequency", "rules_read_frequency", "total_subagents_spawned"}, "customization rollup")
        for source, destination in (("skills_read", "skills_read_frequency"), ("rules_read", "rules_read_frequency")):
            expected_frequency: Counter[str] = Counter()
            for summary in payload["sessions"]:
                expected_frequency.update(summary["aggregates"]["customization_summary"][source])
            _count_map(customization_rollup[destination], f"aggregate {destination}")
            _assert(customization_rollup[destination] == dict(sorted(expected_frequency.items())), f"aggregate {destination} mismatch")
        _int(customization_rollup["total_subagents_spawned"], "aggregate total_subagents_spawned")
        _assert(customization_rollup["total_subagents_spawned"] == sum(item["aggregates"]["customization_summary"]["subagents_spawned"] for item in payload["sessions"]), "aggregate total subagents mismatch")
        totals = rollup["attribution_totals"]
        _keys(totals, {"composer_code_hashes_count", "human_code_hashes_count", "total_lines_added", "total_lines_removed", "scored_commit_count"}, "attribution totals")
        for key in totals:
            _int(totals[key], f"attribution totals {key}")
        _assert(totals["total_lines_added"] == sum(item["total_lines_added"] for item in payload["sessions"]), "aggregate attribution lines added mismatch")
        _assert(totals["total_lines_removed"] == sum(item["total_lines_removed"] for item in payload["sessions"]), "aggregate attribution lines removed mismatch")
        _assert(totals["composer_code_hashes_count"] == sum((item["attribution"] or {}).get("composer_code_hashes_count", 0) for item in payload["sessions"]), "aggregate attribution composer mismatch")
        _assert(totals["human_code_hashes_count"] == sum((item["attribution"] or {}).get("human_code_hashes_count", 0) for item in payload["sessions"]), "aggregate attribution human mismatch")
        _assert(totals["scored_commit_count"] == sum(len((item["attribution"] or {}).get("scored_commits", [])) for item in payload["sessions"]), "aggregate scored commit count mismatch")
        return
    raise ValueError("report_kind must be single-session, diff, or aggregate")


def _render_json(report: dict[str, Any], expected_kind: str) -> str:
    _assert(report.get("report_kind") == expected_kind, f"report_kind must be {expected_kind}")
    assert_v2_report_conforms(report)
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def render_single_session_json(report: dict[str, Any]) -> str:
    """Validate and serialize a single-session v2 report."""
    return _render_json(report, "single-session")


def render_diff_json(report: dict[str, Any]) -> str:
    """Validate and serialize a diff v2 report."""
    return _render_json(report, "diff")


def render_aggregate_json(report: dict[str, Any]) -> str:
    """Validate and serialize an aggregate v2 report."""
    return _render_json(report, "aggregate")
