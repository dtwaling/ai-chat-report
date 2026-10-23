"""Session aggregate, diff, and cohort report builders."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from chat_report.core.diagnostics import evaluate_diagnostics
from chat_report.core.ir import DiagnosticFinding, SessionIR, TOOL_CALL_STATUSES


def _finding_dict(finding: DiagnosticFinding) -> dict[str, Any]:
    return asdict(finding)


def _counter_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: after.get(key, 0) - before.get(key, 0) for key in sorted(set(before) | set(after))}


def build_session_aggregates(session: SessionIR) -> dict[str, Any]:
    """Build the canonical arithmetic rollup for a SessionIR."""
    calls = [call for turn in session.turns for call in turn.tool_calls]
    status = {value: 0 for value in TOOL_CALL_STATUSES}
    semantic, kinds, namespaces = Counter(), Counter(), Counter()
    for call in calls:
        normalized_status = call.status if call.status in TOOL_CALL_STATUSES else ("error" if call.is_error else "completed")
        status[normalized_status] += 1
        semantic[call.semantic_class] += 1
        kinds[call.canonical_kind] += 1
        if call.canonical_kind == "mcp_call" and call.mcp_namespace:
            namespaces[call.mcp_namespace] += 1
    customizations = session.customizations
    skills = sorted({x.path for x in customizations if x.kind == "skill" and x.source == "explicit_read"})
    rules = sorted({x.path for x in customizations if x.kind in {"rule", "agent_doc"} and x.source == "explicit_read"})
    attached = sorted({x.path for x in customizations if x.kind in {"rule", "agent_doc"} and x.source == "context_attached"})
    slowest = sorted((call for call in calls if call.elapsed_ms is not None), key=lambda c: (-c.elapsed_ms, c.call_index))[:5]
    return {
        "turn_count": len(session.turns),
        "thinking_block_count": sum(len(turn.thinking_blocks) for turn in session.turns),
        "total_thinking_duration_ms": sum(block.duration_ms or 0 for turn in session.turns for block in turn.thinking_blocks),
        "tool_calls_total": len(calls),
        "tool_calls_by_semantic_class": dict(sorted(semantic.items())),
        "tool_calls_by_canonical_kind": dict(sorted(kinds.items())),
        "mcp_calls_by_namespace": dict(sorted(namespaces.items())),
        "status_counts": status,
        "timing": {
            "total_session_ms": session.duration_ms,
            "total_tool_elapsed_ms": sum(call.elapsed_ms or 0 for call in calls),
            "slowest_tool_calls": [{"call_index": c.call_index, "turn_index": c.turn_index, "raw_tool_name": c.raw_tool_name, "semantic_class": c.semantic_class, "elapsed_ms": c.elapsed_ms, "status": c.status} for c in slowest],
        },
        "customization_summary": {"skills_read": skills, "rules_read": rules, "attached_rules": attached, "subagents_spawned": len(session.subagents)},
        "subagent_rollup": {
            "count": len(session.subagents),
            "by_type": dict(sorted(Counter(x.subagent_type or "unknown" for x in session.subagents).items())),
            "by_status": dict(sorted(Counter(x.status or "unknown" for x in session.subagents).items())),
            "total_tool_calls": sum(x.tool_call_count for x in session.subagents),
            "total_errors": sum(x.error_count for x in session.subagents),
        },
    }


def _single(session: SessionIR, diagnostics: list[DiagnosticFinding] | None = None, workspace: Path | None = None) -> dict[str, Any]:
    findings = diagnostics if diagnostics is not None else evaluate_diagnostics(session, workspace=workspace)
    severity = {key: 0 for key in ("high", "medium", "low", "info")}
    for finding in findings:
        severity[finding.severity] += 1
    return {"session": session, "aggregates": build_session_aggregates(session), "diagnostics": {"total_findings": len(findings), "by_severity": severity, "findings": findings}}


def build_single_session_report(session: SessionIR, *, diagnostics: list[DiagnosticFinding] | None = None, workspace: Path | None = None) -> dict[str, Any]:
    """Build the serialized single-session report embedded by diff reports."""
    single = _single(session, diagnostics, workspace)
    session_data = asdict(session)
    session_data.pop("turns")
    session_data.pop("warnings")
    report = _envelope("single-session", [session])
    report.update({
        "session": session_data,
        "aggregates": single["aggregates"],
        "diagnostics": {
            "total_findings": single["diagnostics"]["total_findings"],
            "by_severity": single["diagnostics"]["by_severity"],
            "findings": [_finding_dict(finding) for finding in single["diagnostics"]["findings"]],
        },
        "turns": [asdict(turn) for turn in session.turns],
    })
    return report


def _session_summary(single: dict[str, Any]) -> dict[str, Any]:
    session: SessionIR = single["session"]
    return {
        "session_id": session.session_id, "session_id_normalized": session.session_id_normalized,
        "title": session.title, "model_name": session.model_name, "status": session.status,
        "duration_ms": session.duration_ms, "total_lines_added": session.total_lines_added,
        "total_lines_removed": session.total_lines_removed,
        "attribution": asdict(session.attribution) if session.attribution else None,
        "aggregates": single["aggregates"],
        "diagnostics": {"total_findings": single["diagnostics"]["total_findings"], "by_severity": single["diagnostics"]["by_severity"], "findings": [_finding_dict(x) for x in single["diagnostics"]["findings"]]},
    }


def _envelope(kind: str, sessions: Iterable[SessionIR]) -> dict[str, Any]:
    values = list(sessions)
    harnesses, versions = {s.harness for s in values}, {s.harness_version for s in values}
    warnings: list[dict[str, str]] = []
    for session in values:
        for warning in session.warnings:
            if warning not in warnings:
                warnings.append(warning)
    return {"schema_version": "2.0", "report_kind": kind, "generated_at_iso": datetime.now(timezone.utc).isoformat(), "harness": next(iter(harnesses)) if len(harnesses) == 1 else "mixed", "harness_version": next(iter(versions)) if len(versions) == 1 else "mixed", "warnings": warnings}


def build_diff_report(before_session: SessionIR, after_session: SessionIR, *, before_diagnostics: list[DiagnosticFinding] | None = None, after_diagnostics: list[DiagnosticFinding] | None = None, workspace: Path | None = None) -> dict[str, Any]:
    """Build a v2 diff report from two sessions."""
    before, after = _single(before_session, before_diagnostics, workspace), _single(after_session, after_diagnostics, workspace)
    ba, aa = before["aggregates"], after["aggregates"]
    bf, af = before["diagnostics"]["findings"], after["diagnostics"]["findings"]
    before_keys, after_keys = {x.finding_key for x in bf}, {x.finding_key for x in af}
    before_ids, after_ids = {x.id for x in bf}, {x.id for x in af}
    report = _envelope("diff", [before_session, after_session])
    report.update({"before": build_single_session_report(before_session, diagnostics=bf, workspace=workspace), "after": build_single_session_report(after_session, diagnostics=af, workspace=workspace), "delta": {
        "turn_count_delta": aa["turn_count"] - ba["turn_count"], "thinking_block_count_delta": aa["thinking_block_count"] - ba["thinking_block_count"],
        "tool_calls_total_delta": aa["tool_calls_total"] - ba["tool_calls_total"], "error_count_delta": aa["status_counts"]["error"] - ba["status_counts"]["error"],
        "denial_count_delta": aa["status_counts"]["denied"] - ba["status_counts"]["denied"],
        "duration_ms_delta": after_session.duration_ms - before_session.duration_ms if after_session.duration_ms is not None and before_session.duration_ms is not None else None,
        "context_tokens_used_delta": after_session.token_breakdown.total_context_tokens_used - before_session.token_breakdown.total_context_tokens_used if after_session.token_breakdown.total_context_tokens_used is not None and before_session.token_breakdown.total_context_tokens_used is not None else None,
        "by_semantic_class_delta": _counter_delta(ba["tool_calls_by_semantic_class"], aa["tool_calls_by_semantic_class"]),
        "by_canonical_kind_delta": _counter_delta(ba["tool_calls_by_canonical_kind"], aa["tool_calls_by_canonical_kind"]),
        "skills_read_added": sorted(set(aa["customization_summary"]["skills_read"]) - set(ba["customization_summary"]["skills_read"])),
        "skills_read_removed": sorted(set(ba["customization_summary"]["skills_read"]) - set(aa["customization_summary"]["skills_read"])),
        "rules_read_added": sorted(set(aa["customization_summary"]["rules_read"]) - set(ba["customization_summary"]["rules_read"])),
        "rules_read_removed": sorted(set(ba["customization_summary"]["rules_read"]) - set(aa["customization_summary"]["rules_read"])),
        "diagnostics_delta": {"total_findings_delta": len(af) - len(bf), "new_finding_ids": sorted(after_ids - before_ids), "resolved_finding_ids": sorted(before_ids - after_ids), "persisting_finding_ids": sorted(before_ids & after_ids), "new_finding_keys": sorted(after_keys - before_keys), "resolved_finding_keys": sorted(before_keys - after_keys), "persisting_finding_keys": sorted(before_keys & after_keys)},
        "attribution_delta": {"composer_code_hashes_delta": (after_session.attribution.composer_code_hashes_count if after_session.attribution else 0) - (before_session.attribution.composer_code_hashes_count if before_session.attribution else 0), "human_code_hashes_delta": (after_session.attribution.human_code_hashes_count if after_session.attribution else 0) - (before_session.attribution.human_code_hashes_count if before_session.attribution else 0), "lines_added_delta": after_session.total_lines_added - before_session.total_lines_added, "lines_removed_delta": after_session.total_lines_removed - before_session.total_lines_removed}}})
    return report


def build_aggregate_report(sessions: list[SessionIR], *, diagnostics_by_session: dict[str, list[DiagnosticFinding]] | None = None, workspace: Path | None = None) -> dict[str, Any]:
    """Build a cohort rollup preserving per-session summary data."""
    singles = [_single(session, (diagnostics_by_session or {}).get(session.session_id), workspace) for session in sessions]
    report = _envelope("aggregate", sessions)
    rollup: dict[str, Any] = {"total_turns": 0, "total_thinking_blocks": 0, "total_tool_calls": 0, "tool_calls_by_semantic_class": Counter(), "tool_calls_by_canonical_kind": Counter(), "mcp_calls_by_namespace": Counter(), "status_counts": {key: 0 for key in TOOL_CALL_STATUSES}, "diagnostics_rollup": {"total_findings": 0, "by_severity": {key: 0 for key in ("high", "medium", "low", "info")}, "by_heuristic_id": defaultdict(lambda: {"count": 0, "session_ids": []})}, "customization_rollup": {"skills_read_frequency": Counter(), "rules_read_frequency": Counter(), "total_subagents_spawned": 0}, "attribution_totals": {"composer_code_hashes_count": 0, "human_code_hashes_count": 0, "total_lines_added": 0, "total_lines_removed": 0, "scored_commit_count": 0}}
    for single in singles:
        session, aggregates, diagnostics = single["session"], single["aggregates"], single["diagnostics"]
        rollup["total_turns"] += aggregates["turn_count"]; rollup["total_thinking_blocks"] += aggregates["thinking_block_count"]; rollup["total_tool_calls"] += aggregates["tool_calls_total"]
        for key in ("tool_calls_by_semantic_class", "tool_calls_by_canonical_kind", "mcp_calls_by_namespace", "status_counts"):
            for name, count in aggregates[key].items():
                rollup[key][name] = rollup[key].get(name, 0) + count
        for path in aggregates["customization_summary"]["skills_read"]: rollup["customization_rollup"]["skills_read_frequency"][path] += 1
        for path in aggregates["customization_summary"]["rules_read"]: rollup["customization_rollup"]["rules_read_frequency"][path] += 1
        rollup["customization_rollup"]["total_subagents_spawned"] += aggregates["customization_summary"]["subagents_spawned"]
        rollup["diagnostics_rollup"]["total_findings"] += diagnostics["total_findings"]
        for severity, count in diagnostics["by_severity"].items():
            rollup["diagnostics_rollup"]["by_severity"][severity] += count
        for finding in diagnostics["findings"]:
            item = rollup["diagnostics_rollup"]["by_heuristic_id"][finding.id]; item["count"] += 1; item["session_ids"].append(session.session_id)
        attribution = session.attribution
        rollup["attribution_totals"]["total_lines_added"] += session.total_lines_added; rollup["attribution_totals"]["total_lines_removed"] += session.total_lines_removed
        if attribution:
            rollup["attribution_totals"]["composer_code_hashes_count"] += attribution.composer_code_hashes_count; rollup["attribution_totals"]["human_code_hashes_count"] += attribution.human_code_hashes_count; rollup["attribution_totals"]["scored_commit_count"] += len(attribution.scored_commits)
    for key in ("tool_calls_by_semantic_class", "tool_calls_by_canonical_kind", "mcp_calls_by_namespace", "status_counts"):
        rollup[key] = dict(sorted(rollup[key].items()))
    rollup["diagnostics_rollup"]["by_severity"] = dict(sorted(rollup["diagnostics_rollup"]["by_severity"].items()))
    for item in rollup["diagnostics_rollup"]["by_heuristic_id"].values():
        item["session_ids"] = sorted(set(item["session_ids"]))
    rollup["diagnostics_rollup"]["by_heuristic_id"] = dict(sorted(rollup["diagnostics_rollup"]["by_heuristic_id"].items()))
    rollup["customization_rollup"]["skills_read_frequency"] = dict(sorted(rollup["customization_rollup"]["skills_read_frequency"].items()))
    rollup["customization_rollup"]["rules_read_frequency"] = dict(sorted(rollup["customization_rollup"]["rules_read_frequency"].items()))
    report.update({"session_count": len(sessions), "sessions": [_session_summary(x) for x in singles], "rollup": rollup})
    return report
