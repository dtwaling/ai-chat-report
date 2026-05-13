"""Tests for the locked-shape diff emitter (Cursor variant).

MISSION-BRIEF section 4.4 defines the diff shape: ``before`` (idA) + ``after``
(idB) + ``delta`` block. Deltas are ``after - before`` per aggregate counter and
per Component A/B ratio. Same versioning as the single-chat shape.
"""

from __future__ import annotations

from typing import Any

import pytest

from . import _fixtures as fx


def _build_locked(chat_report, chat_id: str, bubbles: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Convenience: build a locked report from a list of (bubble_id, payload) pairs."""
    meta = fx.make_meta(chat_id=chat_id)
    ordered = fx.make_ordered(*bubbles)
    rows = chat_report.build_bubble_rows(ordered)
    return chat_report.build_locked_report(chat_id, meta, rows, ordered)


def _baseline_pair(chat_report) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a 'before' (broad-sweep-heavy) + 'after' (optimus-heavy) pair.

    before: 3 reads, 1 grep, 1 optimus_search => component_a ratio 1/4 (FAIL)
    after:  1 read, 0 greps, 4 optimus_search => component_a ratio 4/1 (PASS)
    """
    before = _build_locked(chat_report, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", [
        ("u0", fx.make_user_bubble("u0")),
        ("r0", fx.make_tool_call_bubble("r0", tool_name="read_file_v2",
                                        params={"targetFile": "src/a.py"})),
        ("r1", fx.make_tool_call_bubble("r1", tool_name="read_file_v2",
                                        params={"targetFile": "src/b.py"})),
        ("r2", fx.make_tool_call_bubble("r2", tool_name="read_file_v2",
                                        params={"targetFile": "src/c.py"})),
        ("g0", fx.make_tool_call_bubble("g0", tool_name="ripgrep_raw_search",
                                        params={"pattern": "TODO"})),
        ("o0", fx.make_tool_call_bubble("o0", tool_name="optimus_search",
                                        params={"query": "x"})),
    ])
    after = _build_locked(chat_report, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", [
        ("u0", fx.make_user_bubble("u0")),
        ("r0", fx.make_tool_call_bubble("r0", tool_name="read_file_v2",
                                        params={"targetFile": "src/a.py"})),
        ("o0", fx.make_tool_call_bubble("o0", tool_name="optimus_search",
                                        params={"query": "x"})),
        ("o1", fx.make_tool_call_bubble("o1", tool_name="optimus_search",
                                        params={"query": "y"})),
        ("o2", fx.make_tool_call_bubble("o2", tool_name="optimus_grep",
                                        params={"pattern": "z"})),
        ("o3", fx.make_tool_call_bubble("o3", tool_name="optimus_resolve",
                                        params={"path": "w"})),
    ])
    return before, after


def test_diff_top_level_fields(chat_report):
    before, after = _baseline_pair(chat_report)
    diff = chat_report.build_locked_diff_report(before, after)

    assert diff["report_version"] == "1.0"
    assert diff["report_kind"] == "diff"
    assert diff["ide"] == "cursor"
    assert diff["before"] == before
    assert diff["after"] == after
    assert "delta" in diff
    assert "generated_at_iso" in diff


def test_diff_delta_total_tool_calls(chat_report):
    before, after = _baseline_pair(chat_report)
    diff = chat_report.build_locked_diff_report(before, after)
    # before: 5 tools, after: 5 tools => delta 0
    assert diff["delta"]["total_tool_calls"] == 0


def test_diff_delta_by_class_arithmetic(chat_report):
    before, after = _baseline_pair(chat_report)
    diff = chat_report.build_locked_diff_report(before, after)
    bc = diff["delta"]["by_class"]
    # All locked classes must appear (zero-filled).
    assert set(bc.keys()) == {
        "broad-sweep-read", "broad-sweep-grep", "broad-sweep-glob",
        "optimus-mcp", "directory-index-read",
        "edit", "write", "bash", "other",
    }
    assert bc["broad-sweep-read"] == -2  # 3 -> 1
    assert bc["broad-sweep-grep"] == -1  # 1 -> 0
    assert bc["optimus-mcp"] == 3        # 1 -> 4
    assert bc["bash"] == 0


def test_diff_delta_component_a(chat_report):
    before, after = _baseline_pair(chat_report)
    diff = chat_report.build_locked_diff_report(before, after)
    a = diff["delta"]["component_a"]
    assert a["optimus_count_delta"] == 3       # 1 -> 4
    assert a["broad_sweep_count_delta"] == -3  # 4 -> 1
    assert a["pass_before"] is False
    assert a["pass_after"] is True
    # ratio 4/1 - 1/4 = 4 - 0.25 = 3.75
    assert a["ratio_delta"] == pytest.approx(3.75)


def test_diff_delta_component_b(chat_report):
    """Both before and after have zero informed; this baseline pair just
    verifies the delta arithmetic doesn't crash on zero/zero ratios."""
    before, after = _baseline_pair(chat_report)
    diff = chat_report.build_locked_diff_report(before, after)
    b = diff["delta"]["component_b"]
    assert b["informed_count_delta"] == 0
    # before had 3 uninformed reads, after has 1 -> delta -2
    assert b["uninformed_count_delta"] == -2
    assert b["pass_before"] is False
    assert b["pass_after"] is False


def test_diff_handles_inf_ratio_delta(chat_report):
    """When before or after has zero broad-sweep + positive optimus, ratio is inf.
    The delta should not crash and should signal the discontinuity."""
    before = _build_locked(chat_report, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", [
        ("u0", fx.make_user_bubble("u0")),
        ("r0", fx.make_tool_call_bubble("r0", tool_name="read_file_v2",
                                        params={"targetFile": "src/a.py"})),
    ])
    after = _build_locked(chat_report, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", [
        ("u0", fx.make_user_bubble("u0")),
        ("o0", fx.make_tool_call_bubble("o0", tool_name="optimus_search",
                                        params={"query": "x"})),
    ])
    diff = chat_report.build_locked_diff_report(before, after)
    a = diff["delta"]["component_a"]
    # Before: 0 / 1 = 0.0
    # After:  1 / 0 = inf
    # Delta: inf - 0 = inf. Acceptable for the contract.
    assert a["ratio_delta"] == float("inf")


def test_diff_dict_is_json_serializable(chat_report):
    import json
    before, after = _baseline_pair(chat_report)
    diff = chat_report.build_locked_diff_report(before, after)
    encoded = json.dumps(diff, allow_nan=True)
    decoded = json.loads(encoded)
    assert decoded["report_kind"] == "diff"


def test_diff_cli_mode_runs(chat_report, tmp_path, monkeypatch):
    """End-to-end via _run_diff_mode with shape=locked uses build_locked_diff_report."""
    before, after = _baseline_pair(chat_report)

    # Stub out build_report to return a fake "legacy" report that carries the
    # raw bubble payload we need for the locked rebuild. _run_diff_mode will
    # rebuild from build_bubble_rows + build_locked_report itself.
    chat_a = before["session_id"]
    chat_b = after["session_id"]
    fixtures_by_chat = {
        chat_a: ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", [
            ("u0", fx.make_user_bubble("u0")),
            ("r0", fx.make_tool_call_bubble("r0", tool_name="read_file_v2",
                                            params={"targetFile": "src/a.py"})),
        ]),
        chat_b: ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", [
            ("u0", fx.make_user_bubble("u0")),
            ("o0", fx.make_tool_call_bubble("o0", tool_name="optimus_search",
                                            params={"query": "x"})),
        ]),
    }

    def fake_build_report(chat_id, state_con, track_con, tools, input_id=None, resolved_from=None):
        _, bubbles = fixtures_by_chat[chat_id]
        ordered = fx.make_ordered(*bubbles)
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
            "toolTrajectory": [],
            "optimusCalls": [],
            "denials": [],
            "errors": [],
            "attribution": {"codeHashesBySource": {}, "deletedFiles": [], "scoredCommits": []},
            "bubbles": [],
        }

    monkeypatch.setattr(chat_report, "build_report", fake_build_report)

    rc = chat_report._run_diff_mode(
        resolved=[(chat_a, chat_a, None), (chat_b, chat_b, None)],
        state_con=None, track_con=None,
        tools="all", fmt="json", out_dir=tmp_path,
        shape="locked",
    )
    assert rc == 0
    # Diff JSON written with the expected stem.
    matches = list(tmp_path.glob("diff-*.json"))
    assert len(matches) == 1
    import json
    written = json.loads(matches[0].read_text(encoding="utf-8"))
    assert written["report_kind"] == "diff"
    assert written["before"]["session_id"] == chat_a
    assert written["after"]["session_id"] == chat_b
