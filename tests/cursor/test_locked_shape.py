"""Tests for the locked-shape emitter (Cursor variant).

These tests drive the section-4-conformant report shape defined in
``MISSION-BRIEF.md`` (locked structured-report shape, ``report_version: "1.0"``).
They exercise the per-helper classifiers in isolation and the full
``build_locked_report`` end-to-end against synthetic in-memory bubble fixtures
(no SQLite roundtrip).
"""

from __future__ import annotations

from typing import Any

import pytest

from . import _fixtures as fx


# ---------------------------------------------------------------------------
# Tool-class mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tool_name, params, expected_class",
    [
        ("read_file_v2", {"targetFile": "src/foo.py"}, "broad-sweep-read"),
        ("read_file_v2", {"targetFile": "C:\\repo\\.cursor\\DIRECTORY_INDEX.md"}, "directory-index-read"),
        ("read_file_v2", {"targetFile": "/repo/.cursor/DIRECTORY_INDEX.md"}, "directory-index-read"),
        ("ripgrep_raw_search", {"pattern": "TODO"}, "broad-sweep-grep"),
        ("glob_file_search", {"pattern": "**/*.py"}, "broad-sweep-glob"),
        ("run_terminal_command_v2", {"command": "ls"}, "bash"),
        ("edit_file_v2", {"targetFile": "src/foo.py"}, "edit"),
        ("optimus_search", {"query": "foo"}, "optimus-mcp"),
        ("optimus_grep", {"pattern": "bar"}, "optimus-mcp"),
        ("optimus_resolve", {"path": "foo"}, "optimus-mcp"),
        ("optimus_prune", {}, "optimus-mcp"),
        ("optimus_extract", {}, "optimus-mcp"),
        ("task_v2", {}, "other"),
        ("some_unknown_tool", {}, "other"),
        ("", {}, "other"),
    ],
)
def test_classify_tool_class_cursor(chat_report, tool_name, params, expected_class):
    assert chat_report.classify_tool_class_cursor(tool_name, params) == expected_class


# ---------------------------------------------------------------------------
# Session ID normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "session_id, expected",
    [
        ("f9a48f94-b698-455e-946d-9734c86babe9", "f9a48f94b698455e946d9734c86babe9"),
        ("ABC-DEF_123", "abcdef123"),
        ("d5093750-dd1a-4a15-adae-79334235a2e7", "d5093750dd1a4a15adae79334235a2e7"),
        ("simple", "simple"),
        ("", ""),
    ],
)
def test_normalize_session_id(chat_report, session_id, expected):
    assert chat_report.normalize_session_id(session_id) == expected


# ---------------------------------------------------------------------------
# Informed-precision-read classification
# ---------------------------------------------------------------------------

def test_informed_precision_read_not_applicable_for_non_read(chat_report):
    result = chat_report.classify_informed_precision_read(
        {"tool_name": "run_terminal_command_v2", "tool_class": "bash"},
        prior_in_turn=[],
    )
    assert result["applicable"] is False
    assert result["classification"] == "n/a"


def test_informed_precision_read_uninformed_when_no_prior_dir_index(chat_report):
    tc = {
        "tool_name": "read_file_v2",
        "tool_class": "broad-sweep-read",
        "input_payload": {"targetFile": "src/foo.py"},
    }
    result = chat_report.classify_informed_precision_read(tc, prior_in_turn=[])
    assert result["applicable"] is True
    assert result["classification"] == "uninformed"
    assert "no DIRECTORY_INDEX.md" in result["reason"]


def test_informed_precision_read_informed_when_dir_index_preceded(chat_report):
    tc = {
        "tool_name": "read_file_v2",
        "tool_class": "broad-sweep-read",
        "input_payload": {"targetFile": "src/foo.py"},
    }
    prior = [
        {"tool_name": "read_file_v2", "tool_class": "directory-index-read",
         "input_payload": {"targetFile": ".cursor/DIRECTORY_INDEX.md"}},
    ]
    result = chat_report.classify_informed_precision_read(tc, prior_in_turn=prior)
    assert result["applicable"] is True
    assert result["classification"] == "informed"
    assert "DIRECTORY_INDEX.md" in result["reason"]


def test_informed_precision_read_dir_index_read_itself_is_na(chat_report):
    """The DIRECTORY_INDEX.md read itself is not the kind of read the heuristic classifies."""
    tc = {
        "tool_name": "read_file_v2",
        "tool_class": "directory-index-read",
        "input_payload": {"targetFile": ".cursor/DIRECTORY_INDEX.md"},
    }
    result = chat_report.classify_informed_precision_read(tc, prior_in_turn=[])
    assert result["applicable"] is False
    assert result["classification"] == "n/a"


# ---------------------------------------------------------------------------
# Success metric components
# ---------------------------------------------------------------------------

def test_success_metric_component_a_pass_when_optimus_ge_broad_sweep(chat_report):
    by_class = {
        "broad-sweep-read": 2,
        "broad-sweep-grep": 1,
        "broad-sweep-glob": 0,
        "optimus-mcp": 3,
    }
    result = chat_report.compute_success_metric_components(
        by_class, informed_count=0, uninformed_count=0,
    )
    a = result["component_a"]
    assert a["optimus_count"] == 3
    assert a["broad_sweep_count"] == 3
    assert a["ratio"] == 1.0
    assert a["pass"] is True


def test_success_metric_component_a_fail_when_optimus_under_broad_sweep(chat_report):
    by_class = {
        "broad-sweep-read": 5,
        "broad-sweep-grep": 2,
        "broad-sweep-glob": 1,
        "optimus-mcp": 1,
    }
    result = chat_report.compute_success_metric_components(
        by_class, informed_count=0, uninformed_count=0,
    )
    a = result["component_a"]
    assert a["optimus_count"] == 1
    assert a["broad_sweep_count"] == 8
    assert a["ratio"] == pytest.approx(1 / 8)
    assert a["pass"] is False


def test_success_metric_component_a_zero_broad_sweep_treats_ratio_as_infinite(chat_report):
    by_class = {
        "broad-sweep-read": 0,
        "broad-sweep-grep": 0,
        "broad-sweep-glob": 0,
        "optimus-mcp": 2,
    }
    result = chat_report.compute_success_metric_components(
        by_class, informed_count=0, uninformed_count=0,
    )
    a = result["component_a"]
    assert a["pass"] is True
    assert a["ratio"] == float("inf") or a["ratio"] >= 1.0


def test_success_metric_component_b_uninformed_only_fails(chat_report):
    """The realistic pre-spike-1 baseline: 0 informed, N uninformed."""
    by_class = {"broad-sweep-read": 5, "optimus-mcp": 0}
    result = chat_report.compute_success_metric_components(
        by_class, informed_count=0, uninformed_count=5,
    )
    b = result["component_b"]
    assert b["informed_count"] == 0
    assert b["uninformed_count"] == 5
    assert b["ratio"] == 0.0
    assert b["pass"] is False


def test_success_metric_overall_partial_pass(chat_report):
    by_class = {"broad-sweep-read": 1, "optimus-mcp": 5}
    result = chat_report.compute_success_metric_components(
        by_class, informed_count=0, uninformed_count=1,
    )
    assert result["component_a"]["pass"] is True
    assert result["component_b"]["pass"] is False
    assert result["overall"]["pass"] is False
    assert result["overall"]["partial_pass"] is True


def test_success_metric_overall_full_pass(chat_report):
    by_class = {"broad-sweep-read": 1, "optimus-mcp": 5}
    result = chat_report.compute_success_metric_components(
        by_class, informed_count=2, uninformed_count=1,
    )
    assert result["component_a"]["pass"] is True
    assert result["component_b"]["pass"] is True
    assert result["overall"]["pass"] is True
    assert result["overall"]["partial_pass"] is False


# ---------------------------------------------------------------------------
# build_locked_report end-to-end (synthetic)
# ---------------------------------------------------------------------------

def _build_synthetic_inputs(chat_report):
    """Build a 4-bubble synthetic chat: user prompt, assistant text, read_file_v2, optimus_search."""
    chat_id = "f9a48f94-b698-455e-946d-9734c86babe9"
    meta = fx.make_meta(chat_id=chat_id)
    ordered = fx.make_ordered(
        ("b0", fx.make_user_bubble("b0", text="check issue #35")),
        ("b1", fx.make_assistant_text_bubble("b1", text="On it.")),
        ("b2", fx.make_tool_call_bubble(
            "b2",
            tool_name="read_file_v2",
            params={"targetFile": "src/foo.py"},
            result_text="contents of foo.py",
        )),
        ("b3", fx.make_tool_call_bubble(
            "b3",
            tool_name="optimus_search",
            params={"query": "issue 35"},
            result_text="search hits...",
        )),
    )
    rows = chat_report.build_bubble_rows(ordered)
    return chat_id, meta, rows, ordered


def test_locked_report_top_level_fields(chat_report):
    chat_id, meta, rows, ordered = _build_synthetic_inputs(chat_report)
    report = chat_report.build_locked_report(chat_id, meta, rows, ordered)

    assert report["report_version"] == "1.0"
    assert report["report_kind"] == "single-chat"
    assert report["ide"] == "cursor"
    assert report["session_id"] == chat_id
    assert report["session_id_normalized"] == chat_id.replace("-", "")
    assert report["session_start_iso"].startswith("2026-")
    assert report["session_end_iso"].startswith("2026-")
    assert isinstance(report["session_duration_s"], int)
    assert report["session_duration_s"] == 300  # 5 minutes apart in fixture
    assert "ide_version" in report  # may be "unknown" for Cursor; key must exist


def test_locked_report_turns_grouped_by_role(chat_report):
    """Consecutive same-role bubbles collapse into one turn."""
    chat_id, meta, rows, ordered = _build_synthetic_inputs(chat_report)
    report = chat_report.build_locked_report(chat_id, meta, rows, ordered)

    turns = report["turns"]
    assert len(turns) == 2  # 1 user, 1 assistant (3 assistant bubbles collapse)
    assert turns[0]["role"] == "user"
    assert turns[0]["turn_index"] == 0
    assert turns[0]["tool_calls"] == []
    assert turns[1]["role"] == "assistant"
    assert turns[1]["turn_index"] == 1
    assert len(turns[1]["tool_calls"]) == 2  # read_file_v2 + optimus_search


def test_locked_report_tool_calls_have_required_fields(chat_report):
    chat_id, meta, rows, ordered = _build_synthetic_inputs(chat_report)
    report = chat_report.build_locked_report(chat_id, meta, rows, ordered)
    tcs = report["turns"][1]["tool_calls"]

    required = {
        "call_index", "tool_name", "tool_class",
        "input_summary", "input_payload",
        "output_summary", "output_status",
        "denial_reason", "elapsed_ms",
        "informed_precision_read",
    }
    for tc in tcs:
        missing = required - set(tc.keys())
        assert not missing, f"tool_call missing keys: {missing}"
        ipr = tc["informed_precision_read"]
        assert set(ipr.keys()) == {"applicable", "classification", "reason"}


def test_locked_report_tool_class_mapping_correct(chat_report):
    chat_id, meta, rows, ordered = _build_synthetic_inputs(chat_report)
    report = chat_report.build_locked_report(chat_id, meta, rows, ordered)
    tcs = report["turns"][1]["tool_calls"]

    by_name = {tc["tool_name"]: tc for tc in tcs}
    assert by_name["read_file_v2"]["tool_class"] == "broad-sweep-read"
    assert by_name["optimus_search"]["tool_class"] == "optimus-mcp"


def test_locked_report_aggregates_by_class(chat_report):
    chat_id, meta, rows, ordered = _build_synthetic_inputs(chat_report)
    report = chat_report.build_locked_report(chat_id, meta, rows, ordered)
    by_class = report["aggregates"]["by_class"]

    # Every key in the locked enum must be present (zero-fill).
    assert set(by_class.keys()) == {
        "broad-sweep-read", "broad-sweep-grep", "broad-sweep-glob",
        "optimus-mcp", "directory-index-read",
        "edit", "write", "bash", "other",
    }
    assert by_class["broad-sweep-read"] == 1
    assert by_class["optimus-mcp"] == 1
    assert by_class["bash"] == 0
    assert report["aggregates"]["total_tool_calls"] == 2


def test_locked_report_success_metric_computed(chat_report):
    chat_id, meta, rows, ordered = _build_synthetic_inputs(chat_report)
    report = chat_report.build_locked_report(chat_id, meta, rows, ordered)
    smc = report["aggregates"]["success_metric_components"]

    assert smc["component_a"]["optimus_count"] == 1
    assert smc["component_a"]["broad_sweep_count"] == 1
    assert smc["component_a"]["pass"] is True

    # The lone Read call is uninformed (no DIRECTORY_INDEX.md preceded it).
    assert smc["component_b"]["informed_count"] == 0
    assert smc["component_b"]["uninformed_count"] == 1
    assert smc["component_b"]["pass"] is False

    # Component A passes but B fails → partial.
    assert smc["overall"]["pass"] is False
    assert smc["overall"]["partial_pass"] is True


def test_locked_report_directory_index_then_read_marks_informed(chat_report):
    chat_id = "f9a48f94-b698-455e-946d-9734c86babe9"
    meta = fx.make_meta(chat_id=chat_id)
    ordered = fx.make_ordered(
        ("b0", fx.make_user_bubble("b0")),
        ("b1", fx.make_tool_call_bubble(
            "b1", tool_name="read_file_v2",
            params={"targetFile": ".cursor/DIRECTORY_INDEX.md"},
            result_text="index contents",
        )),
        ("b2", fx.make_tool_call_bubble(
            "b2", tool_name="read_file_v2",
            params={"targetFile": "src/foo.py"},
            result_text="foo contents",
        )),
    )
    rows = chat_report.build_bubble_rows(ordered)
    report = chat_report.build_locked_report(chat_id, meta, rows, ordered)

    assistant_turn = report["turns"][1]
    foo_read = next(tc for tc in assistant_turn["tool_calls"]
                    if tc["input_payload"].get("targetFile") == "src/foo.py")
    assert foo_read["informed_precision_read"]["classification"] == "informed"

    smc = report["aggregates"]["success_metric_components"]
    assert smc["component_b"]["informed_count"] == 1
    assert smc["component_b"]["uninformed_count"] == 0


def test_locked_report_denials_and_errors_surfaced(chat_report):
    chat_id = "f9a48f94-b698-455e-946d-9734c86babe9"
    meta = fx.make_meta(chat_id=chat_id)
    # Use a fallback denial marker that's always active regardless of hook config.
    ordered = fx.make_ordered(
        ("b0", fx.make_user_bubble("b0")),
        ("b1", fx.make_tool_call_bubble(
            "b1", tool_name="ripgrep_raw_search",
            params={"pattern": "secret"},
            result_text="hook reply",
            is_denial_marker="This workspace routes all search through Optimus MCP",
        )),
        ("b2", fx.make_tool_call_bubble(
            "b2", tool_name="run_terminal_command_v2",
            params={"command": "false"},
            result_text="exit 1",
            status="failed",
        )),
    )
    rows = chat_report.build_bubble_rows(ordered)
    report = chat_report.build_locked_report(chat_id, meta, rows, ordered)

    aggs = report["aggregates"]
    assert len(aggs["denials"]) == 1
    assert aggs["denials"][0]["tool_name"] == "ripgrep_raw_search"
    assert len(aggs["errors"]) == 1
    assert aggs["errors"][0]["tool_name"] == "run_terminal_command_v2"

    # The tool_call entries reflect the same.
    tcs = report["turns"][1]["tool_calls"]
    statuses = {tc["tool_name"]: tc["output_status"] for tc in tcs}
    assert statuses["ripgrep_raw_search"] == "denied"
    assert statuses["run_terminal_command_v2"] == "error"


def test_locked_report_warnings_for_no_per_turn_timestamps(chat_report):
    """Cursor's bubble store doesn't expose per-bubble timestamps; the locked
    shape requires started_iso/ended_iso per turn. The variant should surface a
    warning so downstream consumers know per-turn times are synthesized from
    session bounds rather than per-event."""
    chat_id, meta, rows, ordered = _build_synthetic_inputs(chat_report)
    report = chat_report.build_locked_report(chat_id, meta, rows, ordered)
    codes = [w["code"] for w in report["warnings"]]
    assert "cursor-per-turn-timestamps-synthesized" in codes


def test_locked_report_round_trip_json_serializable(chat_report):
    """The emitted dict must JSON-serialize without custom encoders."""
    import json

    chat_id, meta, rows, ordered = _build_synthetic_inputs(chat_report)
    report = chat_report.build_locked_report(chat_id, meta, rows, ordered)
    # Allow inf for Component A ratio with zero broad-sweep; otherwise must be
    # strictly serializable. The default encoder accepts inf via allow_nan=True.
    encoded = json.dumps(report, allow_nan=True)
    assert '"report_version": "1.0"' in encoded
    decoded = json.loads(encoded)
    assert decoded["session_id"] == chat_id


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------

def test_cli_shape_flag_defaults_to_legacy(chat_report):
    args = chat_report._build_argparser().parse_args(["some-uuid"])
    assert args.shape == "legacy"


def test_cli_shape_flag_accepts_locked(chat_report):
    args = chat_report._build_argparser().parse_args(["--shape", "locked", "some-uuid"])
    assert args.shape == "locked"


def test_cli_shape_flag_rejects_other_values(chat_report):
    with pytest.raises(SystemExit):
        chat_report._build_argparser().parse_args(["--shape", "bogus", "some-uuid"])


def test_aggregate_mode_with_locked_shape_raises_not_implemented(chat_report, tmp_path):
    """Aggregate mode is deferred to commit 3 -- emit a clear NotImplementedError now."""
    with pytest.raises(NotImplementedError, match="locked-shape aggregate"):
        chat_report._run_aggregate_mode(
            resolved=[("a", "a", None)],
            state_con=None, track_con=None,
            tools="all", fmt="json", out_dir=tmp_path,
            shape="locked",
        )
