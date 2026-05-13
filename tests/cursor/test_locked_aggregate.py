"""Tests for the locked-shape aggregate emitter (Cursor variant).

MISSION-BRIEF section 4.3 defines the aggregate shape: top-level ``aggregates``
block summed across input sessions plus per-session ``aggregates`` snippets.
Same versioning as the single-chat shape.
"""

from __future__ import annotations

from typing import Any

import pytest

from . import _fixtures as fx


def _build_locked(chat_report, chat_id: str, bubbles: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    meta = fx.make_meta(chat_id=chat_id)
    ordered = fx.make_ordered(*bubbles)
    rows = chat_report.build_bubble_rows(ordered)
    return chat_report.build_locked_report(chat_id, meta, rows, ordered)


def _three_session_pack(chat_report) -> list[dict[str, Any]]:
    """Build three locked reports with known counts for clean delta arithmetic.

    s1: 2 reads, 1 grep, 0 optimus
    s2: 1 read,  0 greps, 2 optimus, 1 bash, 1 edit
    s3: 0 reads, 0 greps, 3 optimus
    Summed: by_class = {read: 3, grep: 1, optimus: 5, bash: 1, edit: 1}
    """
    s1 = _build_locked(chat_report, "11111111-1111-1111-1111-111111111111", [
        ("u", fx.make_user_bubble("u")),
        ("r0", fx.make_tool_call_bubble("r0", tool_name="read_file_v2",
                                        params={"targetFile": "src/a.py"})),
        ("r1", fx.make_tool_call_bubble("r1", tool_name="read_file_v2",
                                        params={"targetFile": "src/b.py"})),
        ("g0", fx.make_tool_call_bubble("g0", tool_name="ripgrep_raw_search",
                                        params={"pattern": "TODO"})),
    ])
    s2 = _build_locked(chat_report, "22222222-2222-2222-2222-222222222222", [
        ("u", fx.make_user_bubble("u")),
        ("r0", fx.make_tool_call_bubble("r0", tool_name="read_file_v2",
                                        params={"targetFile": "src/c.py"})),
        ("o0", fx.make_tool_call_bubble("o0", tool_name="optimus_search",
                                        params={"query": "x"})),
        ("o1", fx.make_tool_call_bubble("o1", tool_name="optimus_grep",
                                        params={"pattern": "y"})),
        ("b0", fx.make_tool_call_bubble("b0", tool_name="run_terminal_command_v2",
                                        params={"command": "ls"})),
        ("e0", fx.make_tool_call_bubble("e0", tool_name="edit_file_v2",
                                        params={"targetFile": "src/c.py"})),
    ])
    s3 = _build_locked(chat_report, "33333333-3333-3333-3333-333333333333", [
        ("u", fx.make_user_bubble("u")),
        ("o0", fx.make_tool_call_bubble("o0", tool_name="optimus_search",
                                        params={"query": "z"})),
        ("o1", fx.make_tool_call_bubble("o1", tool_name="optimus_resolve",
                                        params={"path": "w"})),
        ("o2", fx.make_tool_call_bubble("o2", tool_name="optimus_prune",
                                        params={})),
    ])
    return [s1, s2, s3]


def test_aggregate_top_level_fields(chat_report):
    pack = _three_session_pack(chat_report)
    agg = chat_report.build_locked_aggregate_report(pack)

    assert agg["report_version"] == "1.0"
    assert agg["report_kind"] == "aggregate"
    assert agg["ide"] == "cursor"
    assert "generated_at_iso" in agg
    assert agg["session_count"] == 3
    assert "sessions" in agg
    assert "aggregates" in agg


def test_aggregate_session_count_matches(chat_report):
    pack = _three_session_pack(chat_report)
    agg = chat_report.build_locked_aggregate_report(pack)
    assert agg["session_count"] == len(pack)
    assert len(agg["sessions"]) == len(pack)


def test_aggregate_per_session_snippets_carry_session_id_and_aggregates(chat_report):
    pack = _three_session_pack(chat_report)
    agg = chat_report.build_locked_aggregate_report(pack)

    for entry, src in zip(agg["sessions"], pack):
        assert entry["session_id"] == src["session_id"]
        assert entry["session_id_normalized"] == src["session_id_normalized"]
        assert entry["aggregates"] == src["aggregates"]


def test_aggregate_by_class_sums_across_sessions(chat_report):
    pack = _three_session_pack(chat_report)
    agg = chat_report.build_locked_aggregate_report(pack)
    bc = agg["aggregates"]["by_class"]

    # Pack composition: s1 (3 tools) + s2 (5 tools) + s3 (3 tools) = 11 tools.
    assert set(bc.keys()) == {
        "broad-sweep-read", "broad-sweep-grep", "broad-sweep-glob",
        "optimus-mcp", "directory-index-read",
        "edit", "write", "bash", "other",
    }
    assert bc["broad-sweep-read"] == 3   # s1:2 + s2:1 + s3:0
    assert bc["broad-sweep-grep"] == 1   # s1:1
    assert bc["optimus-mcp"] == 5        # s2:2 + s3:3
    assert bc["bash"] == 1               # s2:1
    assert bc["edit"] == 1               # s2:1


def test_aggregate_total_tool_calls_sum(chat_report):
    pack = _three_session_pack(chat_report)
    agg = chat_report.build_locked_aggregate_report(pack)
    assert agg["aggregates"]["total_tool_calls"] == 11


def test_aggregate_success_metric_recomputed_against_summed_counts(chat_report):
    pack = _three_session_pack(chat_report)
    agg = chat_report.build_locked_aggregate_report(pack)
    smc = agg["aggregates"]["success_metric_components"]

    # Summed: optimus 5, broad-sweep 3+1+0 = 4 => 5/4 = 1.25 PASS
    assert smc["component_a"]["optimus_count"] == 5
    assert smc["component_a"]["broad_sweep_count"] == 4
    assert smc["component_a"]["ratio"] == pytest.approx(1.25)
    assert smc["component_a"]["pass"] is True

    # No DIRECTORY_INDEX.md reads anywhere -> 0 informed, 3 uninformed (s1 has 2,
    # s2 has 1) -> FAIL
    assert smc["component_b"]["informed_count"] == 0
    assert smc["component_b"]["uninformed_count"] == 3
    assert smc["component_b"]["pass"] is False

    assert smc["overall"]["pass"] is False
    assert smc["overall"]["partial_pass"] is True


def test_aggregate_denials_and_errors_flattened(chat_report):
    # s1 has a denial; s2 has an error; s3 is clean.
    s1 = _build_locked(chat_report, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", [
        ("u", fx.make_user_bubble("u")),
        ("g", fx.make_tool_call_bubble(
            "g", tool_name="ripgrep_raw_search",
            params={"pattern": "p"},
            result_text="hook",
            is_denial_marker="This workspace routes all search through Optimus MCP",
        )),
    ])
    s2 = _build_locked(chat_report, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", [
        ("u", fx.make_user_bubble("u")),
        ("b", fx.make_tool_call_bubble(
            "b", tool_name="run_terminal_command_v2",
            params={"command": "false"},
            result_text="boom", status="failed",
        )),
    ])
    s3 = _build_locked(chat_report, "cccccccc-cccc-cccc-cccc-cccccccccccc", [
        ("u", fx.make_user_bubble("u")),
        ("o", fx.make_tool_call_bubble("o", tool_name="optimus_search",
                                       params={"query": "z"})),
    ])
    agg = chat_report.build_locked_aggregate_report([s1, s2, s3])
    aggs = agg["aggregates"]
    assert len(aggs["denials"]) == 1
    assert aggs["denials"][0]["tool_name"] == "ripgrep_raw_search"
    assert aggs["denials"][0]["session_id"] == s1["session_id"]
    assert len(aggs["errors"]) == 1
    assert aggs["errors"][0]["tool_name"] == "run_terminal_command_v2"
    assert aggs["errors"][0]["session_id"] == s2["session_id"]


def test_aggregate_empty_list_handled(chat_report):
    """A zero-session aggregate is degenerate but the shape must still validate."""
    agg = chat_report.build_locked_aggregate_report([])
    assert agg["session_count"] == 0
    assert agg["sessions"] == []
    assert agg["aggregates"]["total_tool_calls"] == 0
    # Zero-zero ratios trivially pass.
    smc = agg["aggregates"]["success_metric_components"]
    assert smc["component_a"]["pass"] is True
    assert smc["component_b"]["pass"] is True
    assert smc["overall"]["pass"] is True


def test_aggregate_dict_is_json_serializable(chat_report):
    import json
    pack = _three_session_pack(chat_report)
    agg = chat_report.build_locked_aggregate_report(pack)
    encoded = json.dumps(agg, allow_nan=True)
    decoded = json.loads(encoded)
    assert decoded["report_kind"] == "aggregate"
    assert decoded["session_count"] == 3


def test_aggregate_cli_mode_runs(chat_report, tmp_path, monkeypatch):
    """End-to-end via _run_aggregate_mode with shape=locked uses build_locked_aggregate_report."""
    pack = _three_session_pack(chat_report)
    fixtures = {
        s["session_id"]: [
            ("u", fx.make_user_bubble("u")),
            ("r0", fx.make_tool_call_bubble("r0", tool_name="read_file_v2",
                                            params={"targetFile": "src/a.py"})),
        ]
        for s in pack
    }

    def fake_build_report(chat_id, state_con, track_con, tools, input_id=None, resolved_from=None):
        ordered = fx.make_ordered(*fixtures[chat_id])
        meta = fx.make_meta(chat_id=chat_id)
        return {
            "chatId": chat_id,
            "meta": meta,
            "_orderedRaw": ordered,
            "_perToolRollup": {},
            "summary": {"toolCallsIncluded": 0, "bubbleCount": 0, "userBubbles": 0,
                        "assistantBubbles": 0, "errorCount": 0, "denialCount": 0,
                        "toolCallsByName": {}, "toolCallsByCategory": {},
                        "statusCounts": {}, "thinkingBlockCount": 0},
            "toolTrajectory": [], "optimusCalls": [],
            "denials": [], "errors": [],
            "attribution": {"codeHashesBySource": {}, "deletedFiles": [], "scoredCommits": []},
            "bubbles": [],
        }

    monkeypatch.setattr(chat_report, "build_report", fake_build_report)
    resolved = [(s["session_id"], s["session_id"], None) for s in pack]
    rc = chat_report._run_aggregate_mode(
        resolved, state_con=None, track_con=None,
        tools="all", fmt="json", out_dir=tmp_path,
        shape="locked",
    )
    assert rc == 0
    import json
    matches = list(tmp_path.glob("aggregate-*.json"))
    assert len(matches) == 1
    out = json.loads(matches[0].read_text(encoding="utf-8"))
    assert out["report_kind"] == "aggregate"
    assert out["session_count"] == 3
