"""Locked-shape report builder for the Claude Code variant.

Consumes a stream of records (already parsed by ``_jsonl.iter_records``)
and emits a dict conforming to MISSION-BRIEF section 4.1 (single-chat
report).

Diff and aggregate modes are deferred to PR-B3B; the public API hooks
``build_locked_diff_report`` and ``build_locked_aggregate_report`` are NOT
present in this module yet -- the CLI raises NotImplementedError on those
paths.

Public surface:

- ``build_locked_report(records, *, session_id_override=None,
   ide_version_override=None) -> dict``
- ``write_locked_md_report(path, report) -> None``
- ``write_locked_json_report(path, report) -> None``
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from common._locked_helpers import (
    classify_informed_precision_read,
    compute_success_metric_components,
    normalize_session_id,
)

from claudecode._classify import classify_tool_class_claudecode
from claudecode._jsonl import (
    PairedToolCall,
    Turn,
    extract_user_prompt_text,
    pair_tool_calls,
    partition_into_turns,
    unknown_record_types,
)


REPORT_VERSION = "1.0"
IDE = "claude-code"

# Truncation budgets per MISSION-BRIEF section 4.6 ("No tool output payloads
# larger than a configurable byte budget"). 1024 chars is the default.
_OUTPUT_SUMMARY_LIMIT = 1024
_INPUT_SUMMARY_LIMIT = 200

# Markers indicating a user-rejected tool use vs a generic tool error.
# Empirically observed in Claude Code rejection messages (DISCOVERY.md risk
# #5). Matching is case-insensitive on the marker substring.
_DENIAL_MARKERS: tuple[str, ...] = (
    "tool use was rejected",
    "user doesn't want to proceed",
    "user doesn't want to take this action",
)


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from a Claude Code record.

    The store uses ``2026-05-13T01:20:28.612Z`` style strings (Z suffix for
    UTC). Returns None on any parse failure so callers can fall back.
    """
    if not isinstance(ts, str) or not ts:
        return None
    # datetime.fromisoformat in Python 3.11+ handles 'Z' suffix; older
    # versions need it normalized.
    s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _ts_or_empty(record: dict[str, Any] | None) -> str:
    if record is None:
        return ""
    ts = record.get("timestamp")
    return ts if isinstance(ts, str) else ""


def _elapsed_ms_between(start_ts: str | None, end_ts: str | None) -> int | None:
    """Compute the elapsed milliseconds between two ISO timestamps.

    Returns None if either timestamp is missing or unparseable, or if the
    computed value would be negative (clock skew / out-of-order records).
    """
    if not start_ts or not end_ts:
        return None
    a = _parse_iso(start_ts)
    b = _parse_iso(end_ts)
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds() * 1000.0
    if delta < 0:
        return None
    return int(delta)


# ---------------------------------------------------------------------------
# Input / output summarization
# ---------------------------------------------------------------------------


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _summarize_input(tool_name: str, tool_input: Any) -> str:
    """Produce a short, deterministic representation of the tool input.

    Per MISSION-BRIEF section 4.1: input_summary is "short, deterministic".
    The full payload lives in input_payload alongside.
    """
    if not isinstance(tool_input, dict):
        if isinstance(tool_input, str):
            return _truncate(f"{tool_name}: {tool_input}", _INPUT_SUMMARY_LIMIT)
        return f"{tool_name}"

    name = (tool_name or "").strip()
    if name == "Read":
        fp = tool_input.get("file_path", "")
        return _truncate(f"Read {fp}", _INPUT_SUMMARY_LIMIT)
    if name == "Bash":
        cmd = tool_input.get("command", "")
        return _truncate(f"Bash: {cmd}", _INPUT_SUMMARY_LIMIT)
    if name == "Grep":
        pat = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        suffix = f" in {path}" if path else ""
        return _truncate(f"Grep {pat!r}{suffix}", _INPUT_SUMMARY_LIMIT)
    if name == "Glob":
        return _truncate(f"Glob {tool_input.get('pattern', '')}", _INPUT_SUMMARY_LIMIT)
    if name in ("Edit", "MultiEdit", "Write"):
        fp = tool_input.get("file_path", "")
        return _truncate(f"{name} {fp}", _INPUT_SUMMARY_LIMIT)
    if name == "Agent":
        desc = tool_input.get("description", "")
        sub = tool_input.get("subagent_type", "")
        suffix = f" [{sub}]" if sub else ""
        return _truncate(f"Agent: {desc}{suffix}", _INPUT_SUMMARY_LIMIT)
    # Fallback: name plus a stringified payload.
    try:
        payload_str = json.dumps(tool_input, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload_str = repr(tool_input)
    return _truncate(f"{name}: {payload_str}", _INPUT_SUMMARY_LIMIT)


def _extract_result_text(result_block: dict[str, Any] | None) -> str:
    if result_block is None:
        return ""
    content = result_block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _summarize_output(result_block: dict[str, Any] | None) -> str:
    """Truncate the tool_result content to the per-result budget."""
    text = _extract_result_text(result_block)
    return _truncate(text, _OUTPUT_SUMMARY_LIMIT)


def _detect_output_status(
    result_block: dict[str, Any] | None,
    result_text: str,
) -> tuple[str, str | None]:
    """Return (output_status, denial_reason) for a tool_result.

    Status is one of "ok" / "error" / "denied" / "blocked" per the locked
    shape. "denied" is reserved for user-rejected tool uses; "error" for
    everything else with ``is_error: true``; "ok" otherwise. "blocked" is
    NOT emitted by this variant yet (no empirical hook-block evidence).
    """
    if result_block is None:
        # No matching result observed -- session interrupted mid-call.
        return "error", "no tool_result observed (session may have ended mid-call)"
    is_error = result_block.get("is_error")
    if not is_error:
        return "ok", None
    # Errored. Check whether the content carries a denial marker.
    haystack = result_text.lower()
    for marker in _DENIAL_MARKERS:
        if marker in haystack:
            # Use the result_text directly as the denial reason (truncated).
            return "denied", _truncate(result_text.strip(), _INPUT_SUMMARY_LIMIT)
    return "error", None


# ---------------------------------------------------------------------------
# Tool-call building
# ---------------------------------------------------------------------------


def _build_locked_tool_call(
    paired: PairedToolCall,
    call_index: int,
    prior_in_turn: list[dict[str, Any]],
) -> dict[str, Any]:
    ublock = paired.tool_use_block
    rblock = paired.tool_result_block
    tool_name = ublock.get("name") or ""
    tool_input = ublock.get("input")

    tool_class = classify_tool_class_claudecode(tool_name, tool_input)

    result_text = _extract_result_text(rblock)
    output_status, denial_reason = _detect_output_status(rblock, result_text)

    elapsed_ms = _elapsed_ms_between(
        _ts_or_empty(paired.assistant_record),
        _ts_or_empty(paired.result_record),
    )

    tc: dict[str, Any] = {
        "call_index": call_index,
        "tool_name": tool_name,
        "tool_class": tool_class,
        "input_summary": _summarize_input(tool_name, tool_input),
        "input_payload": tool_input if isinstance(tool_input, (dict, list, str)) else {},
        "output_summary": _summarize_output(rblock),
        "output_status": output_status,
        "denial_reason": denial_reason,
        "elapsed_ms": elapsed_ms,
        "informed_precision_read": classify_informed_precision_read(
            {"tool_class": tool_class},
            prior_in_turn=prior_in_turn,
        ),
    }
    # Additive metadata that downstream consumers may want; per shape 4.5
    # additive keys are non-breaking and not required to be present.
    tuid = ublock.get("id")
    if isinstance(tuid, str):
        tc["tool_use_id"] = tuid
    return tc


# ---------------------------------------------------------------------------
# Turn building (parser Turn -> locked-shape turns)
# ---------------------------------------------------------------------------


def _user_turn(turn: Turn, turn_index: int) -> dict[str, Any] | None:
    """Emit a user-role locked-shape turn (or None for an orphan turn)."""
    if turn.user_record is None:
        return None
    ts = _ts_or_empty(turn.user_record)
    return {
        "turn_index": turn_index,
        "role": "user",
        "started_iso": ts,
        "ended_iso": ts,
        "tool_calls": [],
        # Additive: surface the user's prompt text so downstream consumers
        # don't have to re-parse the JSONL.
        "user_prompt_text": extract_user_prompt_text(turn.user_record),
    }


def _assistant_turn(turn: Turn, turn_index: int) -> dict[str, Any]:
    """Emit an assistant-role locked-shape turn with tool_calls[]."""
    paired = pair_tool_calls(turn)
    tool_calls: list[dict[str, Any]] = []
    for i, pc in enumerate(paired):
        tc = _build_locked_tool_call(pc, i, prior_in_turn=tool_calls)
        tool_calls.append(tc)

    # Turn span: first assistant timestamp through last result (or last
    # assistant) timestamp. Fall back to the user record's timestamp if
    # there are no assistant records (interrupted session).
    start_record: dict[str, Any] | None = (
        turn.assistant_records[0] if turn.assistant_records
        else turn.user_record
    )
    end_record: dict[str, Any] | None
    if turn.tool_result_records:
        end_record = turn.tool_result_records[-1]
    elif turn.assistant_records:
        end_record = turn.assistant_records[-1]
    else:
        end_record = turn.user_record

    return {
        "turn_index": turn_index,
        "role": "assistant",
        "started_iso": _ts_or_empty(start_record),
        "ended_iso": _ts_or_empty(end_record),
        "tool_calls": tool_calls,
    }


# ---------------------------------------------------------------------------
# Session-level metadata + aggregates
# ---------------------------------------------------------------------------


def _session_id_from_records(records: list[dict[str, Any]]) -> str:
    """Return the session-id observed on the first record that carries one."""
    for r in records:
        sid = r.get("sessionId")
        if isinstance(sid, str) and sid:
            return sid
    return ""


def _ide_version_from_records(records: list[dict[str, Any]]) -> tuple[str, bool]:
    """Return (ide_version, spans_upgrade).

    ``spans_upgrade`` is True when more than one distinct ``version`` value
    appears across records -- a single long-lived session that survived a
    Claude Code upgrade. In that case the returned version is "mixed".
    """
    versions: set[str] = set()
    for r in records:
        v = r.get("version")
        if isinstance(v, str) and v:
            versions.add(v)
    if not versions:
        return "unknown", False
    if len(versions) == 1:
        return next(iter(versions)), False
    return "mixed", True


def _session_time_span(records: list[dict[str, Any]]) -> tuple[str, str, int]:
    """Return (start_iso, end_iso, duration_seconds) over the record stream."""
    timestamps: list[str] = []
    for r in records:
        t = r.get("timestamp")
        if isinstance(t, str):
            timestamps.append(t)
    if not timestamps:
        return "", "", 0
    start = min(timestamps)
    end = max(timestamps)
    a = _parse_iso(start)
    b = _parse_iso(end)
    if a is None or b is None:
        return start, end, 0
    return start, end, int((b - a).total_seconds())


def _build_aggregates(
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate counters + Components A/B from the emitted locked-shape turns."""
    by_class: dict[str, int] = {
        "broad-sweep-read": 0, "broad-sweep-grep": 0, "broad-sweep-glob": 0,
        "optimus-mcp": 0, "directory-index-read": 0,
        "edit": 0, "write": 0, "bash": 0, "other": 0,
    }
    total = 0
    informed = 0
    uninformed = 0
    denials: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for turn in turns:
        for tc in turn["tool_calls"]:
            total += 1
            by_class[tc["tool_class"]] = by_class.get(tc["tool_class"], 0) + 1
            ipr = tc["informed_precision_read"]
            if ipr["applicable"]:
                if ipr["classification"] == "informed":
                    informed += 1
                elif ipr["classification"] == "uninformed":
                    uninformed += 1
            entry = {
                "tool_name": tc["tool_name"],
                "turn_index": turn["turn_index"],
                "call_index": tc["call_index"],
            }
            if tc["output_status"] == "denied":
                denials.append({**entry, "denial_reason": tc.get("denial_reason") or ""})
            elif tc["output_status"] == "error":
                errors.append({**entry, "error_excerpt": _truncate(tc["output_summary"], 200)})

    return {
        "total_tool_calls": total,
        "by_class": by_class,
        "success_metric_components": compute_success_metric_components(
            by_class, informed, uninformed,
        ),
        "denials": denials,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def build_locked_report(
    records: Iterable[dict[str, Any]],
    *,
    session_id_override: str | None = None,
    ide_version_override: str | None = None,
) -> dict[str, Any]:
    """Build a locked-shape single-chat report from a record stream.

    The iterable is materialized to a list before processing: the builder
    needs multiple passes (turn partitioning, session-id / version /
    time-span extraction, unknown-record-type scanning) so a true single-
    pass stream is not currently feasible. For large sessions, peak memory
    is bounded by the full record set in dict form -- ~857 KB for the
    sample 297-record session in DISCOVERY.md, ~MB scale for hour-long
    sessions. If single-pass streaming becomes necessary, restructure as
    one combined visitor.
    """
    records = list(records)

    parser_turns = partition_into_turns(records)
    locked_turns: list[dict[str, Any]] = []
    next_idx = 0
    for pt in parser_turns:
        u = _user_turn(pt, next_idx)
        if u is not None:
            locked_turns.append(u)
            next_idx += 1
        # Only emit the assistant turn if there's any assistant activity
        # OR the user record exists (an empty-but-present user turn at the
        # tail of an orphan-records-only sequence still produces a 0-call
        # assistant turn for shape conformance).
        if pt.assistant_records or pt.tool_result_records:
            locked_turns.append(_assistant_turn(pt, next_idx))
            next_idx += 1
        elif pt.user_record is not None and not locked_turns[-1]["tool_calls"]:
            # No assistant work after this user prompt yet. We still emit
            # the assistant placeholder so the role-alternation pattern
            # holds for downstream consumers; tool_calls stays empty.
            locked_turns.append(_assistant_turn(pt, next_idx))
            next_idx += 1

    sid = session_id_override or _session_id_from_records(records)
    ide_version, spans_upgrade = _ide_version_from_records(records)
    if ide_version_override:
        ide_version = ide_version_override

    start, end, duration = _session_time_span(records)
    aggregates = _build_aggregates(locked_turns)

    warnings: list[dict[str, str]] = []
    if spans_upgrade:
        warnings.append({
            "code": "claude-code-version-spans-upgrade",
            "message": "Records carry multiple distinct version values; ide_version=mixed.",
        })
    unknown = unknown_record_types(records)
    if unknown:
        warnings.append({
            "code": "unknown-record-types-encountered",
            "message": "Unknown record type(s) ignored: " + ", ".join(unknown),
        })

    return {
        "report_version": REPORT_VERSION,
        "report_kind": "single-chat",
        "ide": IDE,
        "ide_version": ide_version,
        "session_id": sid,
        "session_id_normalized": normalize_session_id(sid),
        "session_start_iso": start,
        "session_end_iso": end,
        "session_duration_s": duration,
        "turns": locked_turns,
        "aggregates": aggregates,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# JSON + markdown writers
# ---------------------------------------------------------------------------


def write_locked_json_report(path: Path, report: dict[str, Any]) -> None:
    """Write the locked-shape report as JSON.

    ``allow_nan=True`` so ratio=Infinity (zero-denominator passes) round-trips.
    """
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=True),
        encoding="utf-8",
    )


def _md_pass_glyph(b: Any) -> str:
    return "PASS" if b else "FAIL"


def _md_format_ratio(r: Any) -> str:
    if r is None:
        return "n/a"
    try:
        if r != r:  # NaN
            return "NaN"
    except TypeError:
        return str(r)
    if r == float("inf"):
        return "infinity"
    if r == float("-inf"):
        return "-infinity"
    try:
        return f"{float(r):.3f}"
    except (TypeError, ValueError):
        return str(r)


def write_locked_md_report(path: Path, report: dict[str, Any]) -> None:
    """Write the locked-shape single-chat report as markdown.

    Sections per MISSION-BRIEF section 4.2: header / summary table /
    success-metric snapshot / turn-by-turn trajectory / denials / errors /
    warnings.
    """
    lines: list[str] = []
    lines.append(f"# chat-report ({report['ide']})")
    lines.append("")
    lines.append(f"- **Session:** `{report['session_id']}`")
    lines.append(f"- **IDE version:** `{report['ide_version']}`")
    lines.append(f"- **Started:** {report['session_start_iso']}")
    lines.append(f"- **Ended:** {report['session_end_iso']}")
    lines.append(f"- **Duration (s):** {report['session_duration_s']}")
    lines.append(f"- **Report version:** {report['report_version']}")
    lines.append("")

    aggs = report["aggregates"]

    lines.append("## Summary by tool class")
    lines.append("")
    lines.append("| tool_class | count |")
    lines.append("|------------|------:|")
    for cls, count in aggs["by_class"].items():
        lines.append(f"| `{cls}` | {count} |")
    lines.append(f"| **total** | **{aggs['total_tool_calls']}** |")
    lines.append("")

    smc = aggs["success_metric_components"]
    a = smc["component_a"]
    b = smc["component_b"]
    o = smc["overall"]
    lines.append("## Success metric snapshot")
    lines.append("")
    lines.append(f"- **Component A:** optimus={a['optimus_count']} vs "
                 f"broad-sweep={a['broad_sweep_count']} "
                 f"(ratio={_md_format_ratio(a['ratio'])}) -- "
                 f"**{_md_pass_glyph(a['pass'])}**")
    lines.append(f"- **Component B:** informed={b['informed_count']} vs "
                 f"uninformed={b['uninformed_count']} "
                 f"(ratio={_md_format_ratio(b['ratio'])}) -- "
                 f"**{_md_pass_glyph(b['pass'])}**")
    lines.append(f"- **Overall:** **{_md_pass_glyph(o['pass'])}**"
                 + (" (partial pass)" if o["partial_pass"] else ""))
    lines.append("")

    lines.append("## Turn-by-turn trajectory")
    lines.append("")
    for turn in report["turns"]:
        lines.append(f"### Turn {turn['turn_index']} -- {turn['role']}")
        lines.append("")
        if turn["role"] == "user":
            prompt = turn.get("user_prompt_text", "").strip()
            if prompt:
                lines.append("> " + prompt.replace("\n", "\n> "))
                lines.append("")
            continue
        if not turn["tool_calls"]:
            lines.append("_(no tool calls)_")
            lines.append("")
            continue
        for tc in turn["tool_calls"]:
            ipr = tc["informed_precision_read"]
            ipr_tag = (
                f" [IPR: {ipr['classification']}]"
                if ipr["applicable"] else ""
            )
            elapsed = (
                f" ({tc['elapsed_ms']} ms)"
                if tc["elapsed_ms"] is not None else ""
            )
            lines.append(
                f"{tc['call_index']}. **`{tc['tool_name']}`** "
                f"[{tc['tool_class']}] -- {tc['output_status']}{elapsed}{ipr_tag}"
            )
            lines.append(f"   - input: {tc['input_summary']}")
            if tc["output_summary"]:
                out_excerpt = tc["output_summary"].splitlines()[0]
                lines.append(f"   - output: {_truncate(out_excerpt, 200)}")
            if tc["denial_reason"]:
                lines.append(f"   - denial: {tc['denial_reason']}")
        lines.append("")

    if aggs["denials"]:
        lines.append("## Denials")
        lines.append("")
        for d in aggs["denials"]:
            lines.append(f"- turn {d['turn_index']} call {d['call_index']} "
                         f"(`{d['tool_name']}`): {d.get('denial_reason', '')}")
        lines.append("")

    if aggs["errors"]:
        lines.append("## Errors")
        lines.append("")
        for e in aggs["errors"]:
            lines.append(f"- turn {e['turn_index']} call {e['call_index']} "
                         f"(`{e['tool_name']}`): {e.get('error_excerpt', '')}")
        lines.append("")

    if report["warnings"]:
        lines.append("## Warnings")
        lines.append("")
        for w in report["warnings"]:
            lines.append(f"- **`{w['code']}`** -- {w['message']}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Diff markdown writer
# ---------------------------------------------------------------------------


_TOOL_CLASS_ORDER: tuple[str, ...] = (
    "broad-sweep-read", "broad-sweep-grep", "broad-sweep-glob",
    "optimus-mcp", "directory-index-read",
    "edit", "write", "bash", "other",
)


def write_locked_md_diff(path: Path, diff: dict[str, Any]) -> None:
    """Write the locked-shape diff report as markdown.

    Layout mirrors ``cursor/chat-report.py::write_locked_md_diff`` for
    cross-IDE parity but uses the claudecode-local helpers
    (``_md_pass_glyph`` / ``_md_format_ratio``) for visual consistency
    within this variant. Diff is a section-4.4 shape; the math itself
    lives in ``common/_diff_aggregate.py``.
    """
    before = diff.get("before", {})
    after = diff.get("after", {})
    delta = diff.get("delta", {})
    bef_aggs = before.get("aggregates", {})
    aft_aggs = after.get("aggregates", {})

    lines: list[str] = []
    lines.append(
        f"# Diff report ({diff.get('ide', '?')}): "
        f"`{(before.get('session_id') or '?')[:12]}` -> "
        f"`{(after.get('session_id') or '?')[:12]}`"
    )
    lines.append("")
    lines.append(f"- **IDE version:** `{diff.get('ide_version', '?')}`")
    lines.append(f"- **Generated:** {diff.get('generated_at_iso', '?')}")
    lines.append(f"- **Report version:** {diff.get('report_version', '?')}")
    lines.append("")

    lines.append("## Before vs After -- Summary")
    lines.append("")
    lines.append("| | Before | After | Delta |")
    lines.append("|---|------:|-----:|-----:|")
    lines.append(
        f"| **total_tool_calls** | {bef_aggs.get('total_tool_calls', 0)} "
        f"| {aft_aggs.get('total_tool_calls', 0)} "
        f"| {delta.get('total_tool_calls', 0):+d} |"
    )
    for cls in _TOOL_CLASS_ORDER:
        bef_v = bef_aggs.get("by_class", {}).get(cls, 0)
        aft_v = aft_aggs.get("by_class", {}).get(cls, 0)
        d_v = delta.get("by_class", {}).get(cls, 0)
        lines.append(f"| `{cls}` | {bef_v} | {aft_v} | {d_v:+d} |")
    lines.append("")

    lines.append("## Success-metric deltas")
    lines.append("")
    da = delta.get("component_a", {})
    db = delta.get("component_b", {})
    lines.append(
        f"- **Component A:** optimus={da.get('optimus_count_delta', 0):+d} / "
        f"broad-sweep={da.get('broad_sweep_count_delta', 0):+d} -- "
        f"ratio_delta={_md_format_ratio(da.get('ratio_delta'))} -- "
        f"pass {_md_pass_glyph(da.get('pass_before'))} -> "
        f"{_md_pass_glyph(da.get('pass_after'))}"
    )
    lines.append(
        f"- **Component B:** informed={db.get('informed_count_delta', 0):+d} / "
        f"uninformed={db.get('uninformed_count_delta', 0):+d} -- "
        f"ratio_delta={_md_format_ratio(db.get('ratio_delta'))} -- "
        f"pass {_md_pass_glyph(db.get('pass_before'))} -> "
        f"{_md_pass_glyph(db.get('pass_after'))}"
    )
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Aggregate markdown writer
# ---------------------------------------------------------------------------


def write_locked_md_aggregate(path: Path, agg: dict[str, Any]) -> None:
    """Write the locked-shape aggregate report as markdown.

    Layout mirrors ``cursor/chat-report.py::write_locked_md_aggregate``
    for cross-IDE parity. Sections: per-session inventory table, summed
    by-class block, summed success-metric snapshot.
    """
    aggs = agg.get("aggregates", {})
    smc = aggs.get("success_metric_components", {})
    a = smc.get("component_a", {})
    b = smc.get("component_b", {})
    o = smc.get("overall", {})

    lines: list[str] = []
    lines.append(
        f"# Aggregate report ({agg.get('ide', '?')}): "
        f"{agg.get('session_count', 0)} sessions"
    )
    lines.append("")
    lines.append(f"- **IDE version:** `{agg.get('ide_version', '?')}`")
    lines.append(f"- **Generated:** {agg.get('generated_at_iso', '?')}")
    lines.append(f"- **Report version:** {agg.get('report_version', '?')}")
    lines.append("")

    lines.append("## Per-session inventory")
    lines.append("")
    lines.append("| session_id | total_tool_calls | optimus | broad-sweep | denials | errors |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for s in agg.get("sessions", []):
        s_aggs = s.get("aggregates", {})
        s_smc = s_aggs.get("success_metric_components", {})
        bc = s_aggs.get("by_class", {})
        broad = (
            bc.get("broad-sweep-read", 0)
            + bc.get("broad-sweep-grep", 0)
            + bc.get("broad-sweep-glob", 0)
        )
        sid = s.get("session_id", "?") or "?"
        lines.append(
            f"| `{sid[:12]}` "
            f"| {s_aggs.get('total_tool_calls', 0)} "
            f"| {s_smc.get('component_a', {}).get('optimus_count', 0)} "
            f"| {broad} "
            f"| {len(s_aggs.get('denials') or [])} "
            f"| {len(s_aggs.get('errors') or [])} |"
        )
    lines.append("")

    lines.append("## Summed across all sessions -- counts by tool_class")
    lines.append("")
    lines.append("| tool_class | count |")
    lines.append("|------------|------:|")
    for cls in _TOOL_CLASS_ORDER:
        lines.append(f"| `{cls}` | {aggs.get('by_class', {}).get(cls, 0)} |")
    lines.append(f"| **total** | **{aggs.get('total_tool_calls', 0)}** |")
    lines.append("")

    lines.append("## Summed success-metric snapshot")
    lines.append("")
    lines.append(
        f"- **Component A:** optimus={a.get('optimus_count', 0)} vs "
        f"broad-sweep={a.get('broad_sweep_count', 0)} "
        f"(ratio={_md_format_ratio(a.get('ratio'))}) -- "
        f"**{_md_pass_glyph(a.get('pass'))}**"
    )
    lines.append(
        f"- **Component B:** informed={b.get('informed_count', 0)} vs "
        f"uninformed={b.get('uninformed_count', 0)} "
        f"(ratio={_md_format_ratio(b.get('ratio'))}) -- "
        f"**{_md_pass_glyph(b.get('pass'))}**"
    )
    lines.append(
        f"- **Overall:** **{_md_pass_glyph(o.get('pass'))}**"
        + (" (partial pass)" if o.get("partial_pass") else "")
    )
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
