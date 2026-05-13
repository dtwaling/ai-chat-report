"""IDE-agnostic builders for the locked diff + aggregate report shapes.

Both functions are pure shape transformations over locked single-chat
reports (MISSION-BRIEF section 4.1). They have no IDE-specific behavior:
``ide`` is taken from the input reports rather than hardcoded, and the
math operates on the ``aggregates.by_class`` + ``success_metric_components``
blocks that every IDE variant emits identically.

Extracted from ``cursor/chat-report.py`` in PR-B3B so the claudecode
variant can share the implementation. Cursor's CLI re-imports from here;
claudecode's CLI imports from here directly.

Versioning policy: any shape-affecting change here MUST bump
``_REPORT_VERSION`` per MISSION-BRIEF section 4.5. Additive fields on the
``delta`` / ``aggregates`` sub-blocks are non-breaking; structural changes
to ``report_kind == "diff"`` or ``report_kind == "aggregate"`` are
breaking.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from common._locked_helpers import compute_success_metric_components


_REPORT_VERSION = "1.0"

_LOCKED_TOOL_CLASSES: tuple[str, ...] = (
    "broad-sweep-read", "broad-sweep-grep", "broad-sweep-glob",
    "optimus-mcp", "directory-index-read",
    "edit", "write", "bash", "other",
)


def _safe_ratio_delta(before: float, after: float) -> float:
    """Compute ``after - before`` for ratios that may be inf.

    ``inf - inf`` -> 0 (no change); finite cases pass through. Always
    ``allow_nan=True``-serializable.
    """
    if math.isinf(before) and math.isinf(after):
        return 0.0
    return after - before


def build_locked_diff_report(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Build a section-4.4-conformant diff report from two locked single-chat reports.

    ``before`` and ``after`` are dicts conforming to the locked single-chat
    shape (validator: ``common._locked_contract.assert_locked_single_chat_conforms``).
    The diff is ``after - before`` per aggregate counter and per Component A/B
    ratio. Same versioning as the single-chat shape.
    """
    bef_aggs = before.get("aggregates", {})
    aft_aggs = after.get("aggregates", {})
    bef_by_class = bef_aggs.get("by_class", {})
    aft_by_class = aft_aggs.get("by_class", {})
    by_class_delta = {
        k: int(aft_by_class.get(k, 0)) - int(bef_by_class.get(k, 0))
        for k in _LOCKED_TOOL_CLASSES
    }

    bef_a = bef_aggs.get("success_metric_components", {}).get("component_a", {})
    aft_a = aft_aggs.get("success_metric_components", {}).get("component_a", {})
    bef_b = bef_aggs.get("success_metric_components", {}).get("component_b", {})
    aft_b = aft_aggs.get("success_metric_components", {}).get("component_b", {})

    delta = {
        "total_tool_calls": int(aft_aggs.get("total_tool_calls", 0))
                          - int(bef_aggs.get("total_tool_calls", 0)),
        "by_class": by_class_delta,
        "component_a": {
            "optimus_count_delta":
                int(aft_a.get("optimus_count", 0)) - int(bef_a.get("optimus_count", 0)),
            "broad_sweep_count_delta":
                int(aft_a.get("broad_sweep_count", 0)) - int(bef_a.get("broad_sweep_count", 0)),
            "ratio_delta": _safe_ratio_delta(
                float(bef_a.get("ratio", 0.0)), float(aft_a.get("ratio", 0.0)),
            ),
            "pass_before": bool(bef_a.get("pass", False)),
            "pass_after": bool(aft_a.get("pass", False)),
        },
        "component_b": {
            "informed_count_delta":
                int(aft_b.get("informed_count", 0)) - int(bef_b.get("informed_count", 0)),
            "uninformed_count_delta":
                int(aft_b.get("uninformed_count", 0)) - int(bef_b.get("uninformed_count", 0)),
            "ratio_delta": _safe_ratio_delta(
                float(bef_b.get("ratio", 0.0)), float(aft_b.get("ratio", 0.0)),
            ),
            "pass_before": bool(bef_b.get("pass", False)),
            "pass_after": bool(aft_b.get("pass", False)),
        },
    }

    ide_version = before.get("ide_version") or after.get("ide_version") or "unknown"
    if before.get("ide_version") and after.get("ide_version") and \
       before["ide_version"] != after["ide_version"]:
        ide_version = "mixed"
    return {
        "report_version": _REPORT_VERSION,
        "report_kind": "diff",
        "ide": before.get("ide", after.get("ide", "unknown")),
        "ide_version": ide_version,
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "before": before,
        "after": after,
        "delta": delta,
    }


def build_locked_aggregate_report(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a section-4.3-conformant aggregate from N locked single-chat reports.

    Sums ``aggregates.by_class`` and ``total_tool_calls`` across all input
    sessions, recomputes ``success_metric_components`` against the summed
    counts, flattens denials and errors with their originating ``session_id``,
    and embeds per-session snippets so consumers can drill into specifics.

    Empty list returns a degenerate zero-session aggregate with
    ``ide = "unknown"``; not validator-conformant on purpose (real callers
    always pass >= 1 report).
    """
    by_class: dict[str, int] = {k: 0 for k in _LOCKED_TOOL_CLASSES}
    informed_total = 0
    uninformed_total = 0
    denials: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    total_tool_calls = 0
    ide = "unknown"

    for rpt in reports:
        ide = rpt.get("ide", ide)
        aggs = rpt.get("aggregates", {})
        total_tool_calls += int(aggs.get("total_tool_calls", 0))
        for k, v in aggs.get("by_class", {}).items():
            by_class[k] = by_class.get(k, 0) + int(v)
        smc_b = aggs.get("success_metric_components", {}).get("component_b", {})
        informed_total += int(smc_b.get("informed_count", 0))
        uninformed_total += int(smc_b.get("uninformed_count", 0))
        sess_id = rpt.get("session_id", "")
        for d in aggs.get("denials", []) or []:
            d2 = dict(d)
            d2["session_id"] = sess_id
            denials.append(d2)
        for e in aggs.get("errors", []) or []:
            e2 = dict(e)
            e2["session_id"] = sess_id
            errors.append(e2)
        sessions.append({
            "session_id": sess_id,
            "session_id_normalized": rpt.get("session_id_normalized", ""),
            "aggregates": aggs,
        })

    success_metrics = compute_success_metric_components(
        by_class, informed_total, uninformed_total,
    )

    # ide_version: unique value if all sessions agree, else "mixed", else "unknown".
    versions = {r.get("ide_version", "") for r in reports if r.get("ide_version")}
    if len(versions) == 1:
        ide_version = next(iter(versions))
    elif len(versions) > 1:
        ide_version = "mixed"
    else:
        ide_version = "unknown"

    return {
        "report_version": _REPORT_VERSION,
        "report_kind": "aggregate",
        "ide": ide,
        "ide_version": ide_version,
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "session_count": len(reports),
        "sessions": sessions,
        "aggregates": {
            "total_tool_calls": total_tool_calls,
            "by_class": by_class,
            "success_metric_components": success_metrics,
            "denials": denials,
            "errors": errors,
        },
    }
