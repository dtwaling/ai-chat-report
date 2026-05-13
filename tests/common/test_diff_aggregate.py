"""IDE-agnostic tests for the diff + aggregate shape builders.

These functions live in ``common/_diff_aggregate.py`` (extracted from the
cursor variant in PR-B3B so the claudecode variant can call the same math).
The tests below build minimal locked-shape single-chat dicts by hand --
they do NOT depend on either IDE's record-parsing pipeline. The goal is to
pin the IDE-neutral arithmetic of the diff/aggregate builders.

Cursor-specific behavior is covered by ``tests/cursor/test_locked_diff.py``
and ``tests/cursor/test_locked_aggregate.py``; those exercise the full
pipeline (raw bubbles -> locked report -> diff/aggregate) and assert the
``ide == "cursor"`` invariant. Here we only pin the math.
"""

from __future__ import annotations

import json
import math
from typing import Any

from common import _diff_aggregate as da
from common import _locked_contract as contract
from common._locked_helpers import compute_success_metric_components


_LOCKED_TOOL_CLASSES = (
    "broad-sweep-read", "broad-sweep-grep", "broad-sweep-glob",
    "optimus-mcp", "directory-index-read",
    "edit", "write", "bash", "other",
)


def _zero_by_class() -> dict[str, int]:
    return {k: 0 for k in _LOCKED_TOOL_CLASSES}


def _minimal_single_chat_report(
    *,
    session_id: str,
    ide: str,
    by_class: dict[str, int] | None = None,
    informed: int = 0,
    uninformed: int = 0,
    total_tool_calls: int | None = None,
    denials: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    ide_version: str = "1.0.0",
) -> dict[str, Any]:
    """Build a minimal validator-conformant single-chat report by hand.

    Used as the input to diff/aggregate builders below. We bypass any IDE
    pipeline because the diff/aggregate math is shape-only.
    """
    bc = _zero_by_class()
    if by_class:
        for k, v in by_class.items():
            bc[k] = v
    smc = compute_success_metric_components(bc, informed, uninformed)
    total = total_tool_calls if total_tool_calls is not None else sum(bc.values())
    return {
        "report_version": "1.0",
        "report_kind": "single-chat",
        "ide": ide,
        "ide_version": ide_version,
        "session_id": session_id,
        "session_id_normalized": session_id.replace("-", "").lower(),
        "session_start_iso": "2026-05-13T00:00:00+00:00",
        "session_end_iso": "2026-05-13T00:10:00+00:00",
        "session_duration_s": 600,
        "turns": [],
        "aggregates": {
            "total_tool_calls": total,
            "by_class": bc,
            "success_metric_components": smc,
            "denials": denials or [],
            "errors": errors or [],
        },
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# _safe_ratio_delta
# ---------------------------------------------------------------------------


def test_safe_ratio_delta_finite():
    assert da._safe_ratio_delta(1.0, 2.0) == 1.0
    assert da._safe_ratio_delta(2.0, 1.0) == -1.0
    assert da._safe_ratio_delta(0.5, 0.5) == 0.0


def test_safe_ratio_delta_inf_inf_is_zero():
    inf = float("inf")
    assert da._safe_ratio_delta(inf, inf) == 0.0


def test_safe_ratio_delta_finite_to_inf_propagates():
    inf = float("inf")
    assert math.isinf(da._safe_ratio_delta(1.0, inf))
    assert math.isinf(da._safe_ratio_delta(inf, 1.0))  # inf - 1 still inf


# ---------------------------------------------------------------------------
# build_locked_diff_report
# ---------------------------------------------------------------------------


def test_diff_basic_delta_arithmetic():
    """delta = after - before per by_class entry + per Component A/B count."""
    before = _minimal_single_chat_report(
        session_id="11111111-1111-1111-1111-111111111111",
        ide="cursor",
        by_class={"broad-sweep-read": 5, "optimus-mcp": 1},
        uninformed=5,
    )
    after = _minimal_single_chat_report(
        session_id="22222222-2222-2222-2222-222222222222",
        ide="cursor",
        by_class={"broad-sweep-read": 3, "optimus-mcp": 4, "edit": 2},
        informed=2,
        uninformed=1,
    )
    diff = da.build_locked_diff_report(before, after)

    assert diff["report_kind"] == "diff"
    assert diff["report_version"] == "1.0"
    assert diff["ide"] == "cursor"
    d = diff["delta"]
    assert d["total_tool_calls"] == (3 + 4 + 2) - (5 + 1)
    assert d["by_class"]["broad-sweep-read"] == -2
    assert d["by_class"]["optimus-mcp"] == 3
    assert d["by_class"]["edit"] == 2
    assert d["component_a"]["optimus_count_delta"] == 3
    assert d["component_a"]["broad_sweep_count_delta"] == -2
    assert d["component_b"]["informed_count_delta"] == 2
    assert d["component_b"]["uninformed_count_delta"] == -4
    contract.assert_locked_diff_conforms(diff)


def test_diff_pass_before_after_flags():
    before = _minimal_single_chat_report(
        session_id="aa", ide="cursor",
        by_class={"broad-sweep-read": 5},
    )
    after = _minimal_single_chat_report(
        session_id="bb", ide="cursor",
        by_class={"broad-sweep-read": 5, "optimus-mcp": 5},
    )
    diff = da.build_locked_diff_report(before, after)
    assert diff["delta"]["component_a"]["pass_before"] is False
    assert diff["delta"]["component_a"]["pass_after"] is True


def test_diff_zero_to_zero_delta_is_zero():
    z = _minimal_single_chat_report(session_id="00", ide="cursor")
    diff = da.build_locked_diff_report(z, z)
    assert diff["delta"]["total_tool_calls"] == 0
    assert all(v == 0 for v in diff["delta"]["by_class"].values())
    assert diff["delta"]["component_a"]["ratio_delta"] == 0.0
    assert diff["delta"]["component_b"]["ratio_delta"] == 0.0
    contract.assert_locked_diff_conforms(diff)


def test_diff_ide_mixed_when_inputs_differ():
    a = _minimal_single_chat_report(session_id="x", ide="cursor",
                                    ide_version="0.50.0")
    b = _minimal_single_chat_report(session_id="y", ide="claude-code",
                                    ide_version="2.10.5")
    diff = da.build_locked_diff_report(a, b)
    # The validator requires `ide` in {cursor, claude-code}; the diff carries
    # the BEFORE side's ide (which the contract permits). The mixed signal
    # surfaces on ide_version, mirroring the cursor variant's behavior.
    assert diff["ide"] in {"cursor", "claude-code"}
    assert diff["ide_version"] == "mixed"


def test_diff_inf_ratio_handled():
    """When Component A goes from inf (no broad-sweep) to a finite ratio,
    the delta math doesn't crash. JSON serialization with allow_nan=True
    survives inf passing through."""
    before = _minimal_single_chat_report(
        session_id="z", ide="cursor",
        by_class={"optimus-mcp": 3},  # broad_sweep=0 => ratio=inf
    )
    after = _minimal_single_chat_report(
        session_id="z", ide="cursor",
        by_class={"optimus-mcp": 3, "broad-sweep-read": 1},
    )
    diff = da.build_locked_diff_report(before, after)
    # before A ratio = inf, after = 3.0 -> finite delta = -inf (3 - inf)
    assert math.isinf(diff["delta"]["component_a"]["ratio_delta"])
    # JSON round-trip with allow_nan tolerated
    encoded = json.dumps(diff, allow_nan=True)
    decoded = json.loads(encoded)
    assert decoded["report_kind"] == "diff"


# ---------------------------------------------------------------------------
# build_locked_aggregate_report
# ---------------------------------------------------------------------------


def test_aggregate_sums_by_class_across_sessions():
    s1 = _minimal_single_chat_report(
        session_id="s1", ide="cursor",
        by_class={"broad-sweep-read": 2, "optimus-mcp": 1},
    )
    s2 = _minimal_single_chat_report(
        session_id="s2", ide="cursor",
        by_class={"broad-sweep-read": 1, "optimus-mcp": 2, "bash": 1},
        uninformed=1,
    )
    s3 = _minimal_single_chat_report(
        session_id="s3", ide="cursor",
        by_class={"optimus-mcp": 3},
    )
    agg = da.build_locked_aggregate_report([s1, s2, s3])
    assert agg["report_kind"] == "aggregate"
    assert agg["session_count"] == 3
    assert len(agg["sessions"]) == 3
    by_class = agg["aggregates"]["by_class"]
    assert by_class["broad-sweep-read"] == 3
    assert by_class["optimus-mcp"] == 6
    assert by_class["bash"] == 1
    contract.assert_locked_aggregate_conforms(agg)


def test_aggregate_empty_list_is_degenerate_pass():
    """Zero sessions: zero counts, trivial pass per success-metric edge case."""
    agg = da.build_locked_aggregate_report([])
    assert agg["session_count"] == 0
    assert agg["sessions"] == []
    assert agg["aggregates"]["total_tool_calls"] == 0
    smc = agg["aggregates"]["success_metric_components"]
    assert smc["component_a"]["pass"] is True
    assert smc["component_b"]["pass"] is True
    assert smc["overall"]["pass"] is True
    # Default ide on empty list is "unknown" (not validator-conformant on
    # purpose: zero-session aggregate is a degenerate input, not a real
    # report. Real callers always pass >= 1 report.)
    assert agg["ide"] == "unknown"


def test_aggregate_ide_taken_from_inputs():
    """When all inputs are one IDE, the aggregate adopts it."""
    cc = _minimal_single_chat_report(session_id="x", ide="claude-code")
    agg = da.build_locked_aggregate_report([cc, cc])
    assert agg["ide"] == "claude-code"


def test_aggregate_ide_version_unique_else_mixed():
    s_v1 = _minimal_single_chat_report(session_id="s1", ide="cursor",
                                       ide_version="0.50.0")
    s_v2 = _minimal_single_chat_report(session_id="s2", ide="cursor",
                                       ide_version="0.51.0")
    agg_uniform = da.build_locked_aggregate_report([s_v1, s_v1])
    assert agg_uniform["ide_version"] == "0.50.0"
    agg_mixed = da.build_locked_aggregate_report([s_v1, s_v2])
    assert agg_mixed["ide_version"] == "mixed"


def test_aggregate_denials_and_errors_flatten_with_session_id():
    s1 = _minimal_single_chat_report(
        session_id="11111111-1111-1111-1111-111111111111",
        ide="cursor",
        denials=[{"turn_index": 0, "call_index": 0, "tool_name": "bash",
                  "denial_reason": "no"}],
    )
    s2 = _minimal_single_chat_report(
        session_id="22222222-2222-2222-2222-222222222222",
        ide="cursor",
        errors=[{"turn_index": 1, "call_index": 0, "tool_name": "bash",
                 "error_excerpt": "oops"}],
    )
    agg = da.build_locked_aggregate_report([s1, s2])
    d = agg["aggregates"]["denials"]
    e = agg["aggregates"]["errors"]
    assert len(d) == 1
    assert d[0]["session_id"] == s1["session_id"]
    assert d[0]["tool_name"] == "bash"
    assert len(e) == 1
    assert e[0]["session_id"] == s2["session_id"]


def test_aggregate_recomputes_success_metric_from_summed_counts():
    """If session totals don't pass individually but the aggregate does, the
    aggregate's success metric reflects the summed counts -- not the per-
    session pass/fail."""
    s1 = _minimal_single_chat_report(
        session_id="s1", ide="cursor",
        by_class={"broad-sweep-read": 4},  # A fails individually
    )
    s2 = _minimal_single_chat_report(
        session_id="s2", ide="cursor",
        by_class={"optimus-mcp": 5},  # A passes individually
    )
    agg = da.build_locked_aggregate_report([s1, s2])
    smc = agg["aggregates"]["success_metric_components"]
    # summed: 5 optimus, 4 broad_sweep -> A passes
    assert smc["component_a"]["pass"] is True


def test_aggregate_json_serializable():
    s = _minimal_single_chat_report(session_id="z", ide="cursor",
                                    by_class={"optimus-mcp": 1})
    agg = da.build_locked_aggregate_report([s, s])
    encoded = json.dumps(agg, allow_nan=True)
    decoded = json.loads(encoded)
    assert decoded["report_kind"] == "aggregate"
    assert decoded["session_count"] == 2
